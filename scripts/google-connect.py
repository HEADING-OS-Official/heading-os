#!/usr/bin/env python3
"""Connect Gmail and Google Calendar through the executive's OWN Google sign-in app (HOS-324).

The one owner of the Google OAuth client file and of the Gmail and Calendar
consent. The installer (scripts/utils/install_google.py), the doctor
(scripts/doctor.py, through `status()`) and the executive call it; nothing else
places the client file or runs this consent.

The executive creates a Google Cloud project and a "Desktop app" OAuth client in
their own Google console (Claude walks them through it: reference/install-agent.md,
"Google mail and calendar"), downloads its JSON, and `place` puts it where the
engine reads it: `.sessions/google/credentials.json` (scripts/utils/gmail_auth.py
`creds_path`), mode 600, directory 700. `consent` opens Google's sign-in in the
browser once per service and proves each token with one read call. The tokens
are `gmail_token.json` (gmail.readonly and gmail.modify, the engine's existing
Gmail token) and `calendar_token.json` (calendar.events), both beside the client
file at mode 600.

No value from the client file or a token is ever printed: only paths, service
names and exception class names.

Usage:
    .venv/bin/python scripts/google-connect.py place ~/Downloads/client_secret_XXXX.json
    .venv/bin/python scripts/google-connect.py place <path> --replace
    .venv/bin/python scripts/google-connect.py consent          # at a terminal
    .venv/bin/python scripts/google-connect.py consent --fresh  # sign in again
    .venv/bin/python scripts/google-connect.py check [--json]   # never opens a browser
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.gmail_auth import (  # noqa: E402
    CALENDAR_SCOPES, SCOPES, ConsentRequired, calendar_token_path, creds_path, google_credentials, google_dir,
    saved_credentials, token_path,
)
from scripts.utils.paths import read_env_value  # noqa: E402

MAX_CLIENT_BYTES = 64 * 1024
OVERRIDES = ("GOOGLE_GMAIL_CREDENTIALS_PATH", "GOOGLE_CONTACTS_CREDENTIALS_PATH")
AUDIENCE_ENV = "HEADING_OS_GOOGLE_AUDIENCE"
AUDIENCES = ("production", "internal")
CLIENT_SUFFIX = ".apps.googleusercontent.com"
RERUN = ".venv/bin/python scripts/heading-install.py --mail google"

#: (part, title, scopes, token path for an engine root)
SERVICES = (
    ("gmail", "Gmail", SCOPES, token_path),
    ("calendar", "Google Calendar", CALENDAR_SCOPES, calendar_token_path),
)


def default_client_path(root: Path) -> Path:
    return google_dir(root) / "credentials.json"


def client_problem(data: object) -> str | None:
    """Why `data` is not a Desktop app client, or None when it is. Never quotes a value."""
    if not isinstance(data, dict):
        return "the file is not a JSON object"
    if "web" in data:
        return ("this is a Web application client; a Desktop app client is needed, because a Web "
                "application client breaks the sign-in on this computer. Create a client of type "
                "\"Desktop app\" in the Google console and download that one")
    if set(data) != {"installed"} or not isinstance(data["installed"], dict):
        return "the file is not a Desktop app client (its only top-level key must be \"installed\")"
    inner = data["installed"]
    client_id = inner.get("client_id")
    if not isinstance(client_id, str) or not client_id.endswith(CLIENT_SUFFIX):
        return f"the client id is missing or does not end in {CLIENT_SUFFIX}"
    secret = inner.get("client_secret")
    if not isinstance(secret, str) or not secret:
        return "the client secret is missing"
    return None


def _override_conflict(root: Path) -> str | None:
    """The override variable that would hide a placed file from the readers, or None."""
    default = default_client_path(root).resolve()
    for name in OVERRIDES:
        value = os.environ.get(name) or ""
        if value and Path(value).expanduser().resolve() != default:
            return name
    return None


# ============================================================
# place
# ============================================================

def cmd_place(root: Path, source: str, replace: bool) -> int:
    conflict = _override_conflict(root)
    if conflict:
        print(f"{conflict} is set to another file, so Gmail and Calendar would not read the file placed "
              f"here. Remove {conflict} from .env (or the environment), then run this again.")
        return 2
    src = Path(source).expanduser().resolve()
    if not src.is_file():
        print(f"not a file: {src}")
        return 1
    try:
        size = src.stat().st_size
        raw = src.read_bytes() if size <= MAX_CLIENT_BYTES else b""
    except OSError as exc:
        print(f"{src} could not be read ({type(exc).__name__})")
        return 1
    if size > MAX_CLIENT_BYTES:
        print(f"{src} is larger than {MAX_CLIENT_BYTES // 1024} KiB; a Google client file is far smaller")
        return 1
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        print(f"{src} is not a readable JSON file ({type(exc).__name__})")
        return 1
    problem = client_problem(data)
    if problem:
        print(f"{src}: {problem}")
        return 1
    dest = default_client_path(root)
    same = dest.is_file() and dest.read_bytes() == raw
    if dest.exists() and not same and not replace:
        print(f"a different client file is already at {dest}; to replace it, add --replace")
        return 1
    # An identical file is written again too, so a copy left at another mode (or a
    # link in its place) is repaired by the same rerun the doctor names.
    for d in (dest.parent.parent, dest.parent):
        d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(dest.parent, 0o700)
    atomic_write_text(dest, text, mode=0o600)
    if same:
        print(f"already in place: {dest} (mode 600)")
        return 0
    print(f"placed: {dest} (mode 600)")
    if replace:
        print("The client changed, so both Google sign-ins must be given again: "
              ".venv/bin/python scripts/google-connect.py consent --fresh")
    print(f"You can now delete the downloaded copy: {src}")
    return 0


# ============================================================
# The read call that proves a token
# ============================================================

def _prove(part: str, creds) -> None:
    from googleapiclient.discovery import build

    from scripts.utils.google_user_agent import google_http

    if part == "gmail":
        build("gmail", "v1", http=google_http(creds)).users().getProfile(userId="me").execute()
    else:
        build("calendar", "v3", http=google_http(creds)).events().list(
            calendarId="primary", maxResults=1).execute()


def _google_failures() -> tuple:
    """The exception classes a Google sign-in or read call ends in when it fails."""
    import httplib2
    from google.auth.exceptions import GoogleAuthError
    from googleapiclient.errors import Error as GoogleApiError
    from oauthlib.oauth2.rfc6749.errors import OAuth2Error  # a declined or mismatched browser sign-in

    return (ConsentRequired, GoogleAuthError, GoogleApiError, OAuth2Error, httplib2.HttpLib2Error, OSError,
            ValueError)


def _libraries_present() -> bool:
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None
                   for m in ("google.oauth2", "google_auth_oauthlib", "googleapiclient"))
    except ImportError:
        return False


# ============================================================
# consent
# ============================================================

def cmd_consent(root: Path, fresh: bool) -> int:
    if not sys.stdin.isatty():
        print("The Google sign-in needs you at a terminal, with a browser on this computer. In a second "
              f"terminal, run: cd {root} && .venv/bin/python scripts/google-connect.py consent")
        return 2
    client = _client_item(root)  # the same reading `check` gives, so a hand-edited file stops here
    if not client["ok"]:
        print(f"The Google client file is not usable: {client['reason']}. Place it first: "
              f"{RERUN} --google-client <path to the downloaded file>")
        return 1
    if not _libraries_present():
        print("The Google libraries are not installed in this engine: uv sync --all-extras")
        return 1
    failures = _google_failures()
    failed = 0
    for part, title, scopes, path_for in SERVICES:
        print(f"{title}: opening Google's sign-in in your browser")
        try:
            creds = google_credentials(scopes, path_for(root), interactive=True, force=fresh, root=root)
            _prove(part, creds)
        except failures as exc:
            print(f"{title}: sign-in failed ({type(exc).__name__})")
            failed += 1
            continue
        print(f"{title}: signed in")
    return 1 if failed else 0


# ============================================================
# check / status
# ============================================================

def _item(part: str, ok: bool, state: str, reason: str) -> dict:
    return {"part": part, "ok": ok, "state": state, "reason": reason}


def _client_item(root: Path) -> dict:
    path = Path(creds_path(root))
    if not path.exists():
        return _item("client", False, "missing", "no Google client file has been placed yet")
    try:
        mode = path.stat().st_mode & 0o777
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _item("client", False, "unusable", f"the client file cannot be read ({type(exc).__name__})")
    if mode != 0o600:
        return _item("client", False, "unusable", f"the client file has mode {mode:o}, not 600")
    if client_problem(data):
        return _item("client", False, "unusable", "the client file is not a Desktop app client")
    return _item("client", True, "ok", "a Desktop app client file is in place, mode 600")


def _audience_item(root: Path) -> dict:
    value = os.environ.get(AUDIENCE_ENV) or read_env_value(root / ".env", AUDIENCE_ENV, default="") or ""
    if value in AUDIENCES:
        return _item("audience", True, "ok", f"recorded as {value}, as read in the Google console")
    return _item("audience", False, "not-set", "the app's publishing status has not been recorded")


def _service_item(root: Path, part: str, title: str, scopes: list[str], path_for) -> dict:
    token = path_for(root)
    if not os.path.exists(token):
        return _item(part, False, "consent-needed", f"{title} has not been signed in yet")
    if not _libraries_present():
        return _item(part, False, "unusable", "the Google libraries are not installed in this engine")
    try:
        saved_credentials(token, scopes)
    except (OSError, ValueError) as exc:  # unreadable, a directory, not a JSON object, or missing fields
        return _item(part, False, "unusable", f"the saved {title} token cannot be read ({type(exc).__name__})")
    failures = _google_failures()
    try:
        creds = google_credentials(scopes, token, interactive=False, root=root)
    except ConsentRequired:
        return _item(part, False, "consent-needed", f"{title} needs to be signed in again")
    except failures as exc:
        return _item(part, False, "unusable", f"the {title} token could not be refreshed ({type(exc).__name__})")
    try:
        _prove(part, creds)
    except failures as exc:
        return _item(part, False, "unusable", f"a {title} read call failed ({type(exc).__name__})")
    return _item(part, True, "ok", f"{title} signed in; one read call answered")


def status(engine_root: Path) -> list[dict]:
    """The client file, the recorded audience and both sign-ins, for `engine_root` only.

    Never opens a browser. Each item has exactly the keys part, ok, state, reason.
    """
    root = Path(engine_root)
    return [_client_item(root), _audience_item(root),
            *(_service_item(root, *svc) for svc in SERVICES)]


def cmd_check(root: Path, as_json: bool) -> int:
    items = status(root)
    if as_json:
        print(json.dumps(items, indent=1))
    else:
        for it in items:
            print(f"{'OK ' if it['ok'] else 'RED'}  {it['part']:<9} {it['state']:<15} {it['reason']}")
    return 0 if all(it["ok"] for it in items) else 1


def main(argv: list[str] | None = None) -> int:
    from scripts.utils.workspace import load_env

    load_env(ROOT)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("place", help="put the downloaded Desktop app client file where the engine reads it")
    p.add_argument("path")
    p.add_argument("--replace", action="store_true", help="replace a different client file already in place")
    c = sub.add_parser("consent", help="sign in to Gmail and Google Calendar in the browser (needs a terminal)")
    c.add_argument("--fresh", action="store_true", help="sign in again even when a token is valid")
    k = sub.add_parser("check", help="client file, audience and both sign-ins; never opens a browser")
    k.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "place":
        return cmd_place(ROOT, args.path, args.replace)
    if args.cmd == "consent":
        return cmd_consent(ROOT, args.fresh)
    return cmd_check(ROOT, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
