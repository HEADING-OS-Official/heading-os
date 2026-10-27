#!/usr/bin/env python3
"""Is the operator in a Zoom meeting right now? One read of the Zoom presence API.

Usage:
    .venv/bin/python scripts/zoom-presence.py           # raw status + verdict
    .venv/bin/python scripts/zoom-presence.py --json    # {"status": ..., "verdict": ...}

Read-only and on demand: one token request, one presence read, nothing stored,
no daemon, no webhook, no timer. Stdlib only.

CREDENTIALS. A Server-to-Server OAuth app in the Zoom Marketplace with the one
scope `user:read:presence_status:admin`. Four names, read through
`load_api_key` from the environment or the engine `.env`:
ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_USER_EMAIL. An S2S
app cannot address the user as `me`, so the user is addressed by email. No
value, the token or the secret is ever printed or logged; an error names the
variable, never its content.

THE API, read 2026-10-18 from the official pages (developers.zoom.us
/docs/internal-apps/s2s-oauth/ and /docs/api/users/):
- token: POST https://zoom.us/oauth/token, form body
  grant_type=account_credentials&account_id=<id>, header
  `Authorization: Basic base64(client_id:client_secret)`; the answer carries
  `access_token`, valid one hour, no refresh token.
- presence: GET https://api.zoom.us/v2/users/{userId}/presence_status, Bearer
  token; the answer carries `status`. Errors: 400 (codes 5301, 5411 no
  permission), 404 (1001 no such user), 429 rate limit.
The documented values disagree with themselves on that page: the enum lists
Away, Do_Not_Disturb, Available, In_Calendar_Event, Presenting,
In_A_Zoom_Meeting, On_A_Call, Out_of_Office, Busy; the prose beside it says
In_A_Meeting, In_A_Call, In_A_Calendar_Event and adds Offline; the DND variant
is spelled "Do_No_Disturb" once. Both spellings are mapped below. The value
Zoom actually returns during a meeting is NOT verified yet: the first real call
is HELM's, after the merge.

THE VERDICT. "in a meeting" only for a meeting or presenting value; "not in a
meeting" only for Available, Away and Out_of_Office. Everything else reads
"cannot tell", never "not": a user who set Do Not Disturb by hand stays
Do_Not_Disturb through a meeting; Offline has been reported for a user
available on mobile (Zoom developer forum, not the official docs); a calendar
event, a phone call and Busy say nothing certain about a meeting; and an
unknown value is unknown.

Exit codes: 0 answered (any verdict), 2 a credential is missing, 3 the API
failed (network, HTTP error, or an answer with no status).
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402

TOKEN_URL = "https://zoom.us/oauth/token"  # noqa: S105 - a URL, not a password
API_BASE = "https://api.zoom.us/v2"
TIMEOUT_S = 20
CREDENTIALS = ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_USER_EMAIL")

IN_MEETING = {"In_A_Zoom_Meeting", "In_A_Meeting", "Presenting"}
NOT_IN_MEETING = {"Available", "Away", "Out_of_Office"}

VERDICT_IN, VERDICT_NOT, VERDICT_UNKNOWN = "in a meeting", "not in a meeting", "cannot tell"


class ZoomError(RuntimeError):
    """The token or presence call failed; the message never carries a credential."""


def verdict(status: str) -> str:
    if status in IN_MEETING:
        return VERDICT_IN
    if status in NOT_IN_MEETING:
        return VERDICT_NOT
    return VERDICT_UNKNOWN


def _check_url(url: str) -> str:
    # The credentials go to this address, so it is https, or plain http only to
    # loopback (a local stub server).
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")):
        return url
    raise ZoomError(f"refusing {parts.scheme}://{parts.hostname}: https only, or http to loopback")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib follows a 302/307 and carries the Authorization header to the new
    # address unchecked (MEASURED 2026-10-18 against loopback stubs: the Basic
    # credentials and the Bearer token both reached the redirect target). Zoom's
    # two endpoints do not redirect, so a redirect is an error, never followed.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(req: urllib.request.Request, what: str) -> dict:
    req.add_header("User-Agent", user_agent())
    req.add_header("Accept", "application/json")
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:  # noqa: S310 - scheme checked by _check_url
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        try:
            data = json.loads(detail)
            message = data.get("message") or data.get("reason") or data.get("error") or ""
            detail = f"code {data['code']}: {message}" if "code" in data else message
        except ValueError:
            pass
        raise ZoomError(f"{what}: HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ZoomError(f"{what}: {type(exc).__name__}: {getattr(exc, 'reason', exc)}") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ZoomError(f"{what}: answer is not JSON") from exc
    if not isinstance(data, dict):
        raise ZoomError(f"{what}: answer is not a JSON object")
    return data


def get_token(token_url: str, account_id: str, client_id: str, client_secret: str) -> str:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "account_credentials", "account_id": account_id}).encode()
    req = urllib.request.Request(token_url, data=body, method="POST")  # noqa: S310 - scheme checked by _check_url
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    token = _request(req, "token").get("access_token")
    if not token:
        raise ZoomError("token: answer has no access_token")
    return token


def get_presence(api_base: str, token: str, user: str) -> str:
    url = f"{api_base.rstrip('/')}/users/{urllib.parse.quote(user, safe='')}/presence_status"
    req = urllib.request.Request(url, method="GET")  # noqa: S310 - scheme checked by _check_url
    req.add_header("Authorization", f"Bearer {token}")
    status = _request(req, "presence").get("status")
    if not status or not isinstance(status, str):
        raise ZoomError("presence: answer has no status")
    return status


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Whether the configured Zoom user is in a meeting now (read-only).")
    p.add_argument("--json", action="store_true", help="print one JSON object")
    # For a local stub server only; real use never passes these.
    p.add_argument("--token-url", default=TOKEN_URL, help=argparse.SUPPRESS)
    p.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    creds = {name: load_api_key(name, required=False) for name in CREDENTIALS}
    missing = [name for name, value in creds.items() if not value]
    if missing:
        msg = f"missing in .env or environment: {', '.join(missing)}"
        if args.json:
            print(json.dumps({"error": "credentials", "missing": missing}))
        print(f"zoom-presence: {msg}", file=sys.stderr)
        return 2

    try:
        token = get_token(_check_url(args.token_url), creds["ZOOM_ACCOUNT_ID"],
                          creds["ZOOM_CLIENT_ID"], creds["ZOOM_CLIENT_SECRET"])
        status = get_presence(_check_url(args.api_base), token, creds["ZOOM_USER_EMAIL"])
    except ZoomError as exc:
        # Zoom echoes the user id in a 404 ("User does not exist: <email>"), so
        # every credential value is masked by name before anything is printed.
        detail = str(exc)
        for name, value in creds.items():
            if len(value) >= 6:  # a shorter one would mask ordinary words
                detail = detail.replace(value, f"<{name}>")
        if args.json:
            print(json.dumps({"error": "api", "detail": detail}))
        print(f"zoom-presence: {detail}", file=sys.stderr)
        return 3

    v = verdict(status)
    if args.json:
        print(json.dumps({"status": status, "verdict": v}))
    else:
        print(f"status:  {status}")
        print(f"verdict: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
