#!/usr/bin/env python3
"""Google mail and calendar for `scripts/heading-install.py` (HOS-324).

With `--mail google` the executive connects Gmail and Google Calendar through
their OWN Google Cloud project and "Desktop app" OAuth client, which they create
in their Google console while Claude walks them through it (reference/install-agent.md,
"Google mail and calendar"). The installer's HOS-309 section registers three
steps, after `mail-choice` and before `mail-account`, each ready and silent
unless `--mail google`:

  google-client    the downloaded client file placed by
                   `scripts/google-connect.py place`, from `--google-client PATH`
  google-audience  HEADING_OS_GOOGLE_AUDIENCE=production|internal in .env, from
                   `--google-audience`. The value is the executive's own reading
                   of the console's Audience page, relayed by Claude: nothing here
                   measures it, and the doctor row says so.
  google-consent   Gmail and Google Calendar signed in by
                   `scripts/google-connect.py consent`, at a terminal. It comes
                   after google-audience on purpose: a token issued while the app
                   was still in Testing may need one more consent after publishing.

Every check asks `scripts/google-connect.py check --json`, which never opens a
browser. A step that cannot finish stops with the exact command to type, and
every rerun form carries `--mail google`, because without it the installer's
`--mail` default (none) switches mail off.

Standard library only, like install_mail.py; every child goes through
`host(ctx)`, `_env` and `_run` of install_herdr_cliproxy.py, so a proof run
replaces the machine. No value from the client file or a token is read here.
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.env_file import upsert_env_value  # noqa: E402 - the one .env writer
from scripts.utils.install_herdr_cliproxy import InstallError, _env, _run, host  # noqa: E402
from scripts.utils.paths import read_env_value  # noqa: E402

AUDIENCE_ENV = "HEADING_OS_GOOGLE_AUDIENCE"  # scripts/google-connect.py AUDIENCE_ENV
AUDIENCES = ("production", "internal")
RERUN = ".venv/bin/python scripts/heading-install.py --mail google"


def _google(ctx) -> bool:
    return ctx.mail == "google"


def _connect(ctx, *args: str, capture: bool = True):
    """`scripts/google-connect.py <args>` in the installed engine, on the injected runner."""
    return _run([str(ctx.python), str(ctx.engine / "scripts" / "google-connect.py"), *args],
                env=_env(ctx, host(ctx)), cwd=ctx.engine, capture=capture)


def _last_line(out) -> str:
    lines = [ln.strip() for ln in ((out.stdout or "") + "\n" + (out.stderr or "")).splitlines() if ln.strip()]
    return lines[-1] if lines else f"exit {out.returncode}"


def _items(ctx) -> dict[str, dict]:
    """part -> item from `check --json`; empty when the engine cannot answer yet."""
    if not ctx.python.exists():
        return {}
    out = _connect(ctx, "check", "--json")
    try:
        items = json.loads(out.stdout or "")
    except ValueError:
        return {}
    return {it.get("part"): it for it in items if isinstance(it, dict)} if isinstance(items, list) else {}


# ============================================================
# google-client
# ============================================================

def google_client_ready(ctx) -> bool:
    if not _google(ctx):
        return True
    return bool(_items(ctx).get("client", {}).get("ok"))


def install_google_client(ctx) -> None:
    if not ctx.google_client:
        raise InstallError(
            "your Google sign-in app's client file is not placed yet. Claude walks you through the "
            "Google console (reference/install-agent.md, \"Google mail and calendar\"); then run: "
            f"{RERUN} --google-client <path to the downloaded file>")
    path = Path(ctx.google_client).expanduser().resolve()
    out = _connect(ctx, "place", str(path))
    if out.returncode != 0:
        raise InstallError(f"{_last_line(out)}. Then run: {RERUN} --google-client <path to the downloaded file>")
    for line in (out.stdout or "").splitlines():
        print(f"  {line}")


# ============================================================
# google-audience
# ============================================================

def google_audience_ready(ctx) -> bool:
    if not _google(ctx):
        return True
    return read_env_value(ctx.engine / ".env", AUDIENCE_ENV, default="") in AUDIENCES


def install_google_audience(ctx) -> None:
    if ctx.google_audience not in AUDIENCES:
        raise InstallError(
            "your Google app must be published before the sign-in. On the Google console's Audience "
            "page, check that the status reads \"In production\" (or that the user type is Internal, "
            f"for a company Google Workspace app), then run: {RERUN} --google-audience production "
            "(or --google-audience internal for a Workspace app)")
    upsert_env_value(ctx.engine / ".env", AUDIENCE_ENV, ctx.google_audience)
    print(f"  Google app audience recorded as {ctx.google_audience}, as you read it in the Google console")


# ============================================================
# google-consent
# ============================================================

def google_consent_ready(ctx) -> bool:
    if not _google(ctx):
        return True
    items = _items(ctx)
    return all(items.get(part, {}).get("ok") for part in ("gmail", "calendar"))


def install_google_consent(ctx) -> None:
    command = f"cd {shlex.quote(str(ctx.engine))} && .venv/bin/python scripts/google-connect.py consent"
    if not sys.stdin.isatty():
        raise InstallError(
            "the Google sign-in needs you at a terminal with a browser on this computer. In a second "
            f"terminal, run: {command}; then run this installer again with --mail google")
    out = _connect(ctx, "consent", capture=False)
    if out.returncode != 0:
        raise InstallError(f"the Google sign-in did not finish (exit {out.returncode}); see the lines "
                           f"above. Run it again: {command}; then: {RERUN}")
