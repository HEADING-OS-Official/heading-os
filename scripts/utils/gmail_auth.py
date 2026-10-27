#!/usr/bin/env python3
"""Shared Gmail API authentication.

One definition of the OAuth dance, so a second Gmail caller does not fork a
second copy of the token handling. Consumed by `scripts/gmail-reader.py` and
`scripts/gmail-send.py`.

Credentials live outside the repo, in the engine clone's gitignored
`.sessions/google/`, and are reused, never re-minted here. The `gmail.modify`
scope covers both reading and `drafts.send`, which is why one token serves both
callers.

Usage:
    from scripts.utils.gmail_auth import get_service
    service = get_service()

`google_credentials(scopes, token, interactive=...)` is the token handling itself, for any
caller of the same OAuth client that needs a different scope in a token file of its own
(`scripts/hindsight-snapshot-offsite.py`, scope `drive.file`).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.google_user_agent import google_flow_user_agent, google_http, google_refresh_request  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def creds_path() -> str:
    return os.getenv(
        "GOOGLE_GMAIL_CREDENTIALS_PATH",
        os.getenv(
            "GOOGLE_CONTACTS_CREDENTIALS_PATH",
            str(PROJECT_ROOT / ".sessions" / "google" / "credentials.json"),
        ),
    )


def token_path() -> str:
    return str(PROJECT_ROOT / ".sessions" / "google" / "gmail_token.json")


def get_service():
    """Return an authorized Gmail API service, refreshing the token if needed."""
    from googleapiclient.discovery import build

    creds = google_credentials(SCOPES, token_path())
    return build("gmail", "v1", http=google_http(creds))


class ConsentRequired(RuntimeError):
    """No usable token, and the caller forbade the browser consent flow."""


def google_credentials(scopes: list[str], token: str, *, interactive: bool = True):
    """Credentials for `scopes` from the token file at `token`, refreshed when needed.

    One OAuth client (`creds_path()`), any number of tokens: a caller that needs a
    different scope keeps its own token file and passes it here, so the dance below
    exists once. With `interactive=False` a missing, unreadable or revoked token raises
    `ConsentRequired` instead of opening a browser, which is what an unattended run
    must do: on a headless machine the consent flow waits for a click that never comes.
    """
    from scripts.utils.optdeps import require

    require("google", extra="ai-extra")
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(token):
        try:
            creds = Credentials.from_authorized_user_file(token, scopes)
        except (ValueError, json.JSONDecodeError) as exc:
            # A token file that exists but cannot be read is the normal outcome of
            # an interrupted write, a revoked grant, or a hand edit: the library
            # raises JSONDecodeError on a truncated file and ValueError on valid
            # JSON missing `refresh_token`. Neither was caught, so `get_service`
            # raised out of a helper whose only caller-side handler
            # (`scripts/gmail-reader.py`) catches FileNotFoundError - the operator
            # got a traceback instead of a re-authorisation.
            #
            # A None here is not a silent default: it falls through to the same
            # consent flow a first run takes, and the reason is stated first.
            print(f"gmail_auth: the saved token at {token} is unusable "
                  f"({type(exc).__name__}: {exc}); re-authorising.", file=sys.stderr)
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(google_refresh_request())
            except RefreshError as exc:
                # A REVOKED grant does not surface where the comment above says
                # it does. `from_authorized_user_file` succeeds on one - the
                # file is valid JSON and carries a refresh_token - and the
                # revocation appears only when this call reaches Google, as
                # `google.auth.exceptions.RefreshError`. That is a
                # `GoogleAuthError`, not a `ValueError`, and it sat outside
                # every handler: the operator of a revoked grant got exactly
                # the outcome the block above says was fixed, a traceback
                # instead of a re-authorisation, on a headless machine where
                # the traceback is the whole failure. MEASURED 2026-08-30 with
                # a credentials object whose `refresh` raises RefreshError - it
                # propagated straight out of `get_service`.
                print(f"gmail_auth: refreshing the saved token at {token} "
                      f"failed ({type(exc).__name__}: {exc}); re-authorising.",
                      file=sys.stderr)
                creds = None
        if not creds or not creds.valid:
            if not interactive:
                raise ConsentRequired(
                    f"no usable Google token at {token}; run the consent step once, by hand")
            secrets = creds_path()
            if not os.path.exists(secrets):
                raise FileNotFoundError(
                    f"Google OAuth client secrets not found at {secrets}. "
                    "Place them there or set GOOGLE_GMAIL_CREDENTIALS_PATH in .env."
                )
            flow = google_flow_user_agent(InstalledAppFlow.from_client_secrets_file(secrets, scopes))
            creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(token), mode=0o700, exist_ok=True)
        # tmp + os.replace, not open("w"). The refresh token is the whole Gmail
        # path's credential: a plain open truncates in place, so a crash or a
        # concurrent reader between truncate and write leaves an empty or
        # half-written token, and every caller then re-runs the browser OAuth
        # dance on a headless machine. atomic_write_text sets the mode on the
        # tempfile before the rename, so the file is never briefly world-readable.
        atomic_write_text(Path(token), creds.to_json(), mode=0o600)
    return creds
