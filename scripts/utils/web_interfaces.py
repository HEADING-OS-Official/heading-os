#!/usr/bin/env python3
"""The web interfaces an install puts on this computer, and whether each one answers (HOS-312).

Usage:
    from scripts.utils.web_interfaces import web_interfaces, answers
    for wi in web_interfaces(engine):
        print(wi.id, wi.name, wi.url, wi.url and answers(wi.url), wi.sign_in)

The one list: `scripts/heading-install.py` prints it at the end of an install,
`scripts/doctor.py` checks the Hindsight console from it and
`scripts/heading-console.py` builds the admin console page from it (HOS-333);
no other copy. A new system is one new entry. `FORMAT` and the `section` field
are what a later local server reads; `section` is "systems" for every entry
today.
Each address is on 127.0.0.1, so each page opens in a browser on this computer
only. The CLIProxyAPI management panel is the route GET /management.html of
the proxy's own server (internal/api/server_routes.go line 57 of the 8.0.48
source, commit dbb70621, read 2026-11-02).

Standard library only at import time, like the installer.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

HINDSIGHT_CONSOLE_URL = "http://127.0.0.1:9999"   # scripts/utils/install_hindsight_console.py
AGENTSVIEW_URL = "http://127.0.0.1:8080"           # scripts/utils/install_agentview.py URL

#: The shape of this list; raised when a field changes meaning (HOS-333).
FORMAT = 2


@dataclass(frozen=True)
class WebInterface:
    id: str
    name: str
    purpose: str
    url: str | None
    sign_in: str | None
    section: str = "systems"
    #: The variable that holds the sign-in key and the file that holds it, or
    #: None; `scripts/heading-secrets.py reveal` reads it (HOS-337, FORMAT 2).
    key: tuple[str, Path] | None = None


def web_interfaces(engine: Path | None = None) -> list[WebInterface]:
    """Every web interface: id, name, one-line purpose, address or None, how to sign in or None.

    The address is None when the system is not set up here. The CLIProxyAPI
    sign-in names the engine's .env, so it and its `key` are None when
    `engine` is None.

    Lific serves its browser page at the root of its server address: the
    lific 2.11.1 binary embeds an index.html titled "Lific" whose assets are
    root paths (/favicon.png, /manifest.webmanifest), read 2026-11-02.
    """
    from scripts.utils.install_herdr_cliproxy import MGMT_KEY_ENV, PROXY_HOST, PROXY_PORT
    from scripts.utils.install_hindsight import ENV_CONSOLE_ACCESS, console_env_path
    from scripts.utils.install_lific import _base_url

    console_env = "~" / console_env_path(Path("/")).relative_to("/")  # the file as the executive's home names it
    return [WebInterface("hindsight", "Hindsight",
                         "Memory: what HEADING OS remembers, and the knowledge pages built from it.",
                         HINDSIGHT_CONSOLE_URL, f"sign in with {ENV_CONSOLE_ACCESS} from {console_env}",
                         key=(ENV_CONSOLE_ACCESS, console_env_path(Path.home()))),
            WebInterface("lific", "Lific", "The task tracker: your projects and issues.",
                         _base_url(Path.home()), None),
            WebInterface("agentview", "AgentView",
                         "Every Claude session on this computer, and how much each model is used.",
                         AGENTSVIEW_URL, None),
            WebInterface("cliproxyapi", "CLIProxyAPI",
                         "The model proxy's panel: its sign-ins and the models it offers.",
                         f"http://{PROXY_HOST}:{PROXY_PORT}/management.html",
                         f"sign in with {MGMT_KEY_ENV} from {engine / '.env'}" if engine else None,
                         key=(MGMT_KEY_ENV, engine / ".env") if engine else None)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a 3xx comes back as an HTTPError carrying its status


def answers(url: str, timeout: float = 3) -> bool:
    """A GET to `url` answers 200 or any 3xx; redirects are not followed."""
    from scripts.utils.user_agent import user_agent  # stdlib only

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})  # noqa: S310 - http to a loopback address from this list
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200 or 300 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        return 300 <= exc.code < 400
    except (OSError, ValueError):
        return False
