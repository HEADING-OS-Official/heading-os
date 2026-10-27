#!/usr/bin/env python3
"""Mattermost client acting as the operator's own account, over REST API v4.

Standard library only. Credentials come from the environment or the engine's
gitignored `.env`: `MATTERMOST_URL` (the server's base URL) and
`MATTERMOST_TOKEN` (a personal access token). The token is sent as a Bearer
header and never printed.

Reads see exactly what the account sees: its teams, channels, group and direct
messages. Nothing here reads the server as an administrator.

`send` and `reply` post as the operator. They are the only commands that reach
another person, and the release gate in `.claude/hooks/_dispatch.py`
(`mattermost_send_action`) refuses them unless the operator's own most recent
prompt asks for a send. `mark-read` and the direct-channel lookup inside `send`
change only the operator's own state.

Exit codes: 0 ok, 1 request or usage error, 2 missing configuration.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(WORKSPACE_ROOT))

from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402
from scripts.utils.workspace import get_default_tz  # noqa: E402

TIMEOUT_S = 20
ID_RE = re.compile(r"^[a-z0-9]{26}$")


class MMError(Exception):
    """A request the server refused or that could not be made."""


class Client:
    def __init__(self, url: str, token: str):
        if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
            raise MMError(f"MATTERMOST_URL must be an http(s) URL, got {url!r}")
        self.base = url.rstrip("/") + "/api/v4"
        self._token = token
        self._me: dict | None = None
        self._users: dict[str, dict] = {}

    def call(self, method: str, path: str, body=None, query: dict | None = None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - scheme checked in __init__
        req.add_header("User-Agent", user_agent())
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - scheme checked in __init__
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if detail.startswith("{") and detail.rstrip().endswith("}"):
                detail = json.loads(detail).get("message", detail)
            raise MMError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MMError(f"{method} {path}: {exc}") from exc
        return json.loads(raw) if raw else None

    # -- identity and users ------------------------------------------------

    def me(self) -> dict:
        if self._me is None:
            self._me = self.call("GET", "/users/me")
            self._users[self._me["id"]] = self._me
        return self._me

    def users_by_ids(self, ids) -> dict[str, dict]:
        missing = [i for i in set(ids) if i and i not in self._users]
        for k in range(0, len(missing), 200):
            # home-path: ok (Mattermost REST API route, not a home directory)
            for u in self.call("POST", "/users/ids", missing[k:k + 200]) or []:
                self._users[u["id"]] = u
        return self._users

    def username(self, user_id: str) -> str:
        self.users_by_ids([user_id])
        u = self._users.get(user_id)
        return u["username"] if u else user_id

    def user_by_username(self, name: str) -> dict:
        # home-path: ok (Mattermost REST API route, not a home directory)
        u = self.call("GET", f"/users/username/{urllib.parse.quote(name.lstrip('@'))}")
        self._users[u["id"]] = u
        return u

    # -- channels ----------------------------------------------------------

    def teams(self) -> list[dict]:
        return self.call("GET", "/users/me/teams") or []

    def my_channels(self) -> list[dict]:
        """Every channel, group and direct message of the account, each with its
        membership merged in as `_member` and a readable `_label`."""
        seen: dict[str, dict] = {}
        members: dict[str, dict] = {}
        for t in self.teams():
            for c in self.call("GET", f"/users/me/teams/{t['id']}/channels") or []:
                c.setdefault("_team", t.get("name", ""))
                seen.setdefault(c["id"], c)
            for m in self.call("GET", f"/users/me/teams/{t['id']}/channels/members") or []:
                members[m["channel_id"]] = m
        me = self.me()["id"]
        dm_peers = {c["id"]: self._dm_peer(c, me) for c in seen.values() if c.get("type") == "D"}
        self.users_by_ids(dm_peers.values())
        for c in seen.values():
            c["_member"] = members.get(c["id"], {})
            if c.get("type") == "D":
                c["_label"] = "@" + self.username(dm_peers[c["id"]])
            elif c.get("type") == "G":
                c["_label"] = "group: " + (c.get("display_name") or c.get("name", ""))
            else:
                c["_label"] = "~" + c.get("name", "") + (f" ({c['display_name']})" if c.get("display_name") else "")
        return list(seen.values())

    @staticmethod
    def _dm_peer(channel: dict, me: str) -> str:
        a, _, b = channel.get("name", "").partition("__")
        return b if a == me else a

    @staticmethod
    def unread_of(c: dict) -> tuple[int, int]:
        m = c.get("_member") or {}
        unread = max(0, int(c.get("total_msg_count") or 0) - int(m.get("msg_count") or 0))
        return unread, int(m.get("mention_count") or 0)

    def resolve(self, target: str, *, create_dm: bool = False) -> dict:
        """A channel by id, `@username` (direct message), `~name`, or display name."""
        target = target.strip()
        if ID_RE.match(target):
            return self.call("GET", f"/channels/{target}")
        if target.startswith("@"):
            user = self.user_by_username(target)
            me = self.me()["id"]
            name = "__".join(sorted((me, user["id"])))
            for c in self.my_channels():
                if c.get("type") == "D" and c.get("name") == name:
                    return c
            if create_dm:
                return self.call("POST", "/channels/direct", [me, user["id"]])
            raise MMError(f"no direct message with {target} yet")
        chans = self.my_channels()
        key = target.lstrip("~").lower()
        for field in ("name", "display_name"):
            hits = [c for c in chans if (c.get(field) or "").lower() == key]
            if len(hits) == 1:
                return hits[0]
        hits = [c for c in chans if key in (c.get("display_name") or "").lower()
                or key in (c.get("name") or "").lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise MMError(f"no channel matches {target!r}")
        names = ", ".join(c.get("_label", c["id"]) for c in hits[:10])
        raise MMError(f"{target!r} is ambiguous: {names}")

    # -- posts -------------------------------------------------------------

    def posts(self, channel_id: str, limit: int) -> list[dict]:
        data = self.call("GET", f"/channels/{channel_id}/posts", query={"per_page": limit})
        return ordered(data)

    def thread(self, post_id: str) -> list[dict]:
        return list(reversed(ordered(self.call("GET", f"/posts/{post_id}/thread"))))

    def search(self, terms: str, limit: int) -> list[dict]:
        out: list[dict] = []
        for t in self.teams():
            data = self.call("POST", f"/teams/{t['id']}/posts/search",
                             {"terms": terms, "is_or_search": False, "per_page": limit, "page": 0})
            out.extend(ordered(data))
        out.sort(key=lambda p: p.get("create_at", 0), reverse=True)
        return out[:limit]

    def post(self, channel_id: str, message: str, root_id: str = "") -> dict:
        body = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        return self.call("POST", "/posts", body)

    def view(self, channel_id: str) -> None:
        self.call("POST", "/channels/members/me/view", {"channel_id": channel_id})


def ordered(data) -> list[dict]:
    """Posts of a post list, newest first, as the server orders them."""
    if not data:
        return []
    posts = data.get("posts") or {}
    return [posts[i] for i in data.get("order") or [] if i in posts]


# -- output ------------------------------------------------------------------

def when(ms: int) -> str:
    dt = datetime.fromtimestamp((ms or 0) / 1000, tz=timezone.utc).astimezone(get_default_tz())
    return dt.strftime("%Y-%m-%d %H:%M")


def post_line(cl: Client, p: dict, channel_label: str = "") -> str:
    who = cl.username(p.get("user_id", ""))
    where = f"[{channel_label}] " if channel_label else ""
    reply = " (reply)" if p.get("root_id") else ""
    files = f" [+{len(p.get('file_ids') or [])} file(s)]" if p.get("file_ids") else ""
    text = (p.get("message") or "").strip()
    return f"{when(p.get('create_at'))}  {where}{who}{reply}{files}  id={p['id']}\n    " + text.replace("\n", "\n    ")


def emit(args, payload, text: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else text)


def slim(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "channel_id", "user_id", "root_id", "create_at", "message", "file_ids")}


# -- commands ----------------------------------------------------------------

def cmd_whoami(cl: Client, args) -> int:
    me = cl.me()
    emit(args, {k: me.get(k) for k in ("id", "username", "email", "roles")},
         f"{me['username']} <{me.get('email', '')}> roles: {me.get('roles', '')}")
    return 0


def cmd_channels(cl: Client, args) -> int:
    chans = cl.my_channels()
    if args.unread:
        chans = [c for c in chans if cl.unread_of(c)[0] or cl.unread_of(c)[1]]
    chans.sort(key=lambda c: c.get("last_post_at", 0), reverse=True)
    chans = chans[: args.limit]
    rows = []
    for c in chans:
        unread, mentions = cl.unread_of(c)
        rows.append({"id": c["id"], "type": c.get("type"), "label": c["_label"], "team": c.get("_team"),
                     "last_post_at": c.get("last_post_at"), "unread": unread, "mentions": mentions})
    lines = [f"{when(r['last_post_at'])}  {r['label']}"
             + (f"  unread={r['unread']}" if r["unread"] else "")
             + (f" mentions={r['mentions']}" if r["mentions"] else "")
             + f"  id={r['id']}" for r in rows]
    emit(args, rows, "\n".join(lines) or "nothing")
    return 0


def cmd_read(cl: Client, args) -> int:
    c = cl.resolve(args.target)
    ps = list(reversed(cl.posts(c["id"], args.limit)))
    cl.users_by_ids(p.get("user_id") for p in ps)
    emit(args, [slim(p) for p in ps], "\n".join(post_line(cl, p) for p in ps) or "no messages")
    return 0


def cmd_thread(cl: Client, args) -> int:
    ps = cl.thread(args.post_id)
    cl.users_by_ids(p.get("user_id") for p in ps)
    emit(args, [slim(p) for p in ps], "\n".join(post_line(cl, p) for p in ps) or "no messages")
    return 0


def cmd_search(cl: Client, args) -> int:
    ps = cl.search(args.terms, args.limit)
    cl.users_by_ids(p.get("user_id") for p in ps)
    labels = {c["id"]: c["_label"] for c in cl.my_channels()} if ps else {}
    emit(args, [slim(p) for p in ps],
         "\n".join(post_line(cl, p, labels.get(p.get("channel_id"), p.get("channel_id", ""))) for p in ps)
         or "no matches")
    return 0


def cmd_users(cl: Client, args) -> int:
    # home-path: ok (Mattermost REST API route, not a home directory)
    found = cl.call("POST", "/users/search", {"term": args.term, "limit": args.limit}) or []
    rows = [{k: u.get(k) for k in ("id", "username", "first_name", "last_name", "email", "position")} for u in found]
    emit(args, rows, "\n".join(f"@{r['username']}  {r['first_name'] or ''} {r['last_name'] or ''}".rstrip()
                               + (f"  <{r['email']}>" if r["email"] else "") for r in rows) or "nobody")
    return 0


def message_text(args) -> str:
    if args.body_file:
        text = Path(args.body_file).read_text(encoding="utf-8")
    elif args.message == "-":
        text = sys.stdin.read()
    else:
        text = args.message or ""
    if not text.strip():
        raise MMError("empty message, nothing sent")
    return text


def cmd_send(cl: Client, args) -> int:
    text = message_text(args)
    c = cl.resolve(args.target, create_dm=True)
    p = cl.post(c["id"], text)
    emit(args, slim(p), f"sent to {c.get('_label') or args.target}  id={p['id']}")
    return 0


def cmd_reply(cl: Client, args) -> int:
    text = message_text(args)
    parent = cl.call("GET", f"/posts/{args.post_id}")
    root = parent.get("root_id") or parent["id"]
    p = cl.post(parent["channel_id"], text, root_id=root)
    emit(args, slim(p), f"replied in thread {root}  id={p['id']}")
    return 0


def cmd_mark_read(cl: Client, args) -> int:
    c = cl.resolve(args.target)
    cl.view(c["id"])
    emit(args, {"channel_id": c["id"]}, f"marked read: {c.get('_label') or args.target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Mattermost client acting as the operator's account.")
    ap.add_argument("--json", action="store_true", help="structured output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami", help="the account the token belongs to")

    p = sub.add_parser("channels", help="channels, groups and direct messages, most recent first")
    p.add_argument("--unread", "-u", action="store_true", help="only those with unread messages")
    p.add_argument("--limit", "-n", type=int, default=40)

    sub.add_parser("unread", help="shorthand for `channels --unread`")

    p = sub.add_parser("read", help="recent messages of a channel or direct message")
    p.add_argument("target", help="@username, ~channel-name, display name, or channel id")
    p.add_argument("--limit", "-n", type=int, default=20)

    p = sub.add_parser("thread", help="a whole thread, from any post in it")
    p.add_argument("post_id")

    p = sub.add_parser("search", help="search message history (Mattermost search syntax)")
    p.add_argument("terms")
    p.add_argument("--limit", "-n", type=int, default=20)

    p = sub.add_parser("users", help="find people by name, username or email")
    p.add_argument("term")
    p.add_argument("--limit", "-n", type=int, default=20)

    for name, target, help_ in (("send", "target", "post a message (gated: needs the operator's word)"),
                                ("reply", "post_id", "reply in a thread (gated: needs the operator's word)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument(target)
        p.add_argument("message", nargs="?", help="the text, or - for stdin")
        p.add_argument("--body-file", help="read the text from this file")

    p = sub.add_parser("mark-read", help="mark a channel or direct message read")
    p.add_argument("target")
    return ap


COMMANDS = {
    "whoami": cmd_whoami, "channels": cmd_channels, "read": cmd_read, "thread": cmd_thread,
    "search": cmd_search, "users": cmd_users, "send": cmd_send, "reply": cmd_reply,
    "mark-read": cmd_mark_read,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "unread":
        args.cmd, args.unread, args.limit = "channels", True, 200
    url = load_api_key("MATTERMOST_URL", required=False)
    token = load_api_key("MATTERMOST_TOKEN", required=False)
    if not url or not token:
        print("MATTERMOST_URL and MATTERMOST_TOKEN must be set in .env", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.cmd](Client(url, token), args)
    except (MMError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
