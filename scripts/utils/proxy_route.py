#!/usr/bin/env python3
"""The route a browser run leaves by: direct, or a `DECODO_PROXY_N` slot.

Shared by `scripts/browser.py launch` and `scripts/linkedin-activity.py`. The
operator asked on 2026-09-26 to be asked, on every launch, whether the run goes
direct or through proxy slot 1, 2 or 3, and to see which country each slot
exits in. So `--proxy-slot` has NO default:

  * given (`direct`, `1`, `2`, `3`; `0` still means direct, so nothing that
    already passes it breaks), the run proceeds on that route;
  * omitted on a terminal (stdin and stdout both TTYs), the four routes are
    probed and shown as a menu, and the operator types one of `0` to `3`.
    Empty input or anything else picks NOTHING and the run exits
    `EXIT_ROUTE_NOT_CHOSEN`; there is no default to fall back to;
  * omitted off a terminal (Claude running it), the run refuses with
    `EXIT_ROUTE_NOT_CHOSEN`, prints the same menu and one line naming the flag,
    so the operator can be asked and the command re-run. The refusal comes
    before any browser, relay or LinkedIn work.

`python scripts/browser.py routes` prints the menu alone and exits 0.

The country is MEASURED per route, never derived from a proxy hostname or
username: one request to `GEO_CHECK_URL` made THROUGH that route, so the service
sees only that route's exit address, and it answers with that address and its
country in the same reply, so the two cannot disagree. Direct is measured the
same way. The four probes run in parallel under ONE wall-clock deadline of
`PROBE_TIMEOUT` seconds: a probe still out at the deadline is shown as
unavailable and abandoned on a daemon thread, because a `requests` timeout does
not bound DNS resolution or a reply that trickles in. Every probe ignores the
environment's own proxy variables, so "direct" means direct. A slot is reached
with `requests` and the slot URL itself (PySocks for `socks5h://`, as the
LinkedIn pre-flight already does), so a probe starts no relay and nothing
outlives the command. The reply is accepted only as an IP address and a
two-letter country; anything else is reported as unavailable, never echoed.
What the menu shows is what that route answered at that moment: a rotating
residential slot may leave by another address, and another country, on its
next connection.

A failed probe prints `unavailable: <short error>`, never a guessed country.
The error is scrubbed of the slot's URL, its username and its password, raw and
percent-decoded, and when a credential is too short to scrub safely only the
exception's class name is printed. A slot URL that does not even parse yields a
fixed sentence and no exception text at all, because the parser's own error
quotes the netloc, credentials included, before anything knows what to scrub
(reproduced 2026-09-26 with an NFKC-invalid password). No part of a slot's
userinfo is ever shown.

Usage:
  from scripts.utils.proxy_route import add_route_argument, resolve_route
  slot = resolve_route(args.proxy_slot, tool="browser.py launch")
  if slot is None:
      return EXIT_ROUTE_NOT_CHOSEN
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, TextIO
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.utils.user_agent import user_agent  # noqa: E402

# The exit-IP service this repository already uses (`reference/vpn-preflight.md`).
IP_CHECK_URL = "https://api.ipify.org?format=json"
# The exit address AND its country, for whoever asks. ipinfo.io because it is
# the geolocation service this workspace already names (`reference/vpn-preflight.md`,
# the `/yt-pulse` skill), it is HTTPS, it needs no key for this use, and one
# unauthenticated request to `/json` answers with the caller's own `ip` and a
# two-letter `country`, so the country shown belongs to the address shown.
GEO_CHECK_URL = "https://ipinfo.io/json"
PROBE_TIMEOUT = 8.0

# `0` is kept as a spelling of direct so a caller that already passes it works.
ROUTE_CHOICES = ("direct", "0", "1", "2", "3")
SLOTS = (1, 2, 3)
# Distinct from every exit code either tool already uses (browser.py 0-2,
# linkedin-activity.py 0-6).
EXIT_ROUTE_NOT_CHOSEN = 7
_ERROR_MAX = 120
_COUNTRY = re.compile(r"[A-Z]{2}")


@dataclass
class RouteProbe:
    slot: int
    ip: str = ""
    country: str = ""
    error: str = ""


def route_label(slot: int) -> str:
    return "direct" if slot == 0 else f"proxy slot {slot}"


def parse_route(value: str) -> int:
    """`direct` or `0` is 0; `1`, `2`, `3` are that slot. Anything else raises."""
    text = str(value).strip().lower()
    if text in ("direct", "0"):
        return 0
    if text in ("1", "2", "3"):
        return int(text)
    raise ValueError(f"not a route: {value!r} (expected direct, 1, 2 or 3)")


def add_route_argument(parser: argparse.ArgumentParser, extra_help: str = "") -> None:
    """`--proxy-slot`, required in effect: omitted means ask, never a default."""
    parser.add_argument(
        "--proxy-slot", default=None, choices=ROUTE_CHOICES, metavar="{direct,1,2,3}",
        help="Route: direct, or the DECODO_PROXY_N slot 1, 2 or 3 (0 also means direct). "
             "No default. Omitted on a terminal, the four routes are shown with their "
             "measured exit country and you pick one; omitted off a terminal, the run "
             f"refuses with exit code {EXIT_ROUTE_NOT_CHOSEN} and prints the same menu. "
             + extra_help)


def _secrets_of(url: str) -> List[str]:
    """The strings that must never be printed for this slot URL."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    found = [url]
    for part in (parsed.username, parsed.password):
        if part:
            found += [part, unquote(part)]
    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        found += [userinfo, unquote(userinfo)]
    return [s for s in found if s]


def scrub_error(exc: BaseException, secrets: List[str]) -> str:
    """A short, printable error with every credential removed.

    Any secret under four characters cannot be removed from arbitrary text
    without either missing it or shredding the message, so then the message is
    dropped and only the class name remains.
    """
    name = type(exc).__name__
    if any(len(s) < 4 for s in secrets):
        return name
    text = str(exc)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    # Any other userinfo-bearing URL the library chose to quote.
    text = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@'\"]*@", "<proxy>://***@", text)
    text = " ".join(text.split())
    if len(text) > _ERROR_MAX:
        text = text[:_ERROR_MAX - 3] + "..."
    return f"{name}: {text}" if text else name


def probe_route(slot: int, timeout: float = PROBE_TIMEOUT) -> RouteProbe:
    """Exit IP and country of one route, measured through that route. Never raises."""
    result = RouteProbe(slot)
    secrets: List[str] = []
    try:
        proxies = None
        if slot:
            url = os.environ.get(f"DECODO_PROXY_{slot}", "").strip()
            if not url:
                result.error = f"DECODO_PROXY_{slot} not in .env"
                return result
            try:
                secrets = _secrets_of(url)
            except ValueError:
                result.error = f"DECODO_PROXY_{slot} is not a parseable proxy URL"
                return result
            if "://" not in url:
                url = f"http://{url}"
            proxies = {"http": url, "https": url}
        import requests
        with requests.Session() as session:
            session.trust_env = False  # the environment's proxy is not this route
            session.headers["User-Agent"] = user_agent()
            reply = session.get(GEO_CHECK_URL, proxies=proxies, timeout=timeout)
            reply.raise_for_status()
            body = reply.json()
        ip = body.get("ip") if isinstance(body, dict) else None
        country = body.get("country") if isinstance(body, dict) else None
        try:
            ip = str(ipaddress.ip_address(ip)) if isinstance(ip, str) else None
        except ValueError:
            ip = None
        if ip and isinstance(country, str) and _COUNTRY.fullmatch(country):
            result.ip, result.country = ip, country
        else:
            result.error = "the geolocation reply was not an IP address and a country code"
    except Exception as exc:  # reported on the menu line, never raised
        result.error = scrub_error(exc, secrets)
    return result


def probe_all() -> List[RouteProbe]:
    """All four routes, in parallel, in the order direct, 1, 2, 3.

    Daemon threads rather than an executor: a pool's shutdown, and the
    interpreter's exit, both wait for a worker stuck in DNS, so the deadline
    would hold for the menu and not for the command.
    """
    from scripts.utils.workspace import load_env
    load_env()
    routes = (0, *SLOTS)
    found: dict = {}

    def _run(slot: int) -> None:
        found[slot] = probe_route(slot, PROBE_TIMEOUT)

    workers = [threading.Thread(target=_run, args=(s,), daemon=True) for s in routes]
    for w in workers:
        w.start()
    deadline = time.monotonic() + PROBE_TIMEOUT
    for w in workers:
        w.join(max(0.0, deadline - time.monotonic()))
    late = f"no answer within {PROBE_TIMEOUT:.0f}s"
    return [found.get(s) or RouteProbe(s, error=late) for s in routes]


def format_menu(probes: List[RouteProbe]) -> List[str]:
    lines = [f"Routes (measured now through each route, {PROBE_TIMEOUT:.0f}s deadline):"]
    for p in probes:
        detail = f"unavailable: {p.error}" if p.error else f"exit {p.ip}, country {p.country}"
        lines.append(f"  {p.slot}  {route_label(p.slot):<12}  {detail}")
    return lines


def _is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def resolve_route(
    value: Optional[str],
    *,
    tool: str,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    probe: Optional[Callable[[], List[RouteProbe]]] = None,
) -> Optional[int]:
    """The chosen slot (0 = direct), or None when no route was chosen.

    None is the caller's cue to return `EXIT_ROUTE_NOT_CHOSEN` at once, before
    doing any work of its own.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if value is not None:
        return parse_route(value)
    menu = format_menu((probe or probe_all)())
    if _is_terminal(stdin) and _is_terminal(stdout):
        print("\n".join(menu), file=stdout, flush=True)
        stdout.write("Route (0 direct, 1, 2 or 3; Enter cancels): ")
        stdout.flush()
        try:
            answer = stdin.readline().strip()
        except (OSError, ValueError) as exc:  # a hung-up terminal: nothing chosen
            print(f"Could not read a route ({type(exc).__name__}); nothing started.", file=stderr)
            return None
        if not answer:
            print("No route chosen; nothing started.", file=stderr)
            return None
        try:
            return parse_route(answer)
        except ValueError:
            print(f"{answer!r} is not a route; nothing chosen, nothing started.", file=stderr)
            return None
    print("\n".join(menu), file=stdout, flush=True)
    # Four separate flags and no `|`: a line pasted into a shell must not
    # become a pipeline that runs the first choice.
    print(f"REFUSED: no route chosen and no terminal to ask on. Re-run the same {tool} "
          "command adding one of: --proxy-slot direct, --proxy-slot 1, --proxy-slot 2, "
          "--proxy-slot 3", file=stderr, flush=True)
    return None
