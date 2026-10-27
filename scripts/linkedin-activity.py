#!/usr/bin/env python3
"""Scrape a LinkedIn profile's recent activity feed authenticated via Brave cookies.

Launches Brave through Playwright's chromium driver (`executable_path`, resolved
by `scripts/browser.py`), on the route the operator chose, in a fresh context. It never
opens the Brave profile directory itself, which may be in use by a running
Brave. The li_at + JSESSIONID cookies are read from that profile's cookie store
read-only by `scripts/utils/chromium_cookies.py` and injected into the fresh
context. It then navigates to /in/<slug>/recent-activity/all/, scrolls to load
the requested number of posts, and extracts structured post data from the
rendered DOM. The user agent sent, by the browser and by the pre-flight probe,
is the one the launched browser reports, with the headless marker removed.

The route has NO default (HOS-140, operator request 2026-09-26):
`--proxy-slot direct|1|2|3`, with `0` still meaning direct. Omitted on a
terminal, the four routes are shown with their exit IP and country, measured
through each route at that moment, and the operator picks one; omitted off a
terminal, the run refuses with exit code 7 before any cookie, browser or
LinkedIn work and prints the same menu. The mechanism:
`scripts/utils/proxy_route.py`.

Playwright's bundled Chromium (`--engine chromium`) is opt-in.

A slot may be `http://` or `socks5h://` (`socks5://` also accepted). An http slot
goes to Playwright as it always did. A socks slot cannot: Playwright 1.61.0
refuses SOCKS5 with a username and password at launch, in Chromium and Brave
alike. So for a socks slot the run starts `SocksRelay`, a thread inside this
process listening on 127.0.0.1 on a port the OS picks. The browser talks
unauthenticated SOCKS5 to it, and it opens each upstream connection through
PySocks with the slot's credentials and with DNS resolved at the proxy. It stops
when the run ends. The `requests` pre-flight hands the socks URL to PySocks
directly. A proxy that refuses the connection, wrong credentials included, ends
the run with exit code 6 and the proxy's own error.

Usage:
  python scripts/linkedin-activity.py --slug example-profile --limit 3 --proxy-slot direct
  python scripts/linkedin-activity.py --slug example-profile --limit 5 --headed --proxy-slot direct
  python scripts/linkedin-activity.py --slug example-profile --proxy-slot 2
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sqlite3
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW
from scripts.browser import _browser_paths
from scripts.utils.chromium_cookies import _read_cookies
from scripts.utils.socks_relay import SOCKS_SCHEMES, SocksRelay, proxy_scheme
from scripts.utils.data_path import data_write_dir
from scripts.utils.proxy_route import EXIT_ROUTE_NOT_CHOSEN, add_route_argument, resolve_route
from scripts.utils.workspace import get_default_tz_name, load_env


EXIT_PROXY_REFUSED = 6


class ProxyRefused(RuntimeError):
    """The proxy slot itself refused: wrong credentials, or no route through it."""


def socks_refusal(exc: BaseException) -> str | None:
    """The PySocks error behind a `requests` failure, or None when there is none.

    urllib3 wraps the PySocks error in `NewConnectionError`, which `MaxRetryError`
    carries as `.reason`, which `requests` wraps in `ConnectionError`; the chain is
    walked through `__cause__`, `__context__` and `.reason` rather than matched as
    text.
    """
    try:
        import socks
    except ImportError:
        return None
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, socks.ProxyError):
            return str(cur)
        todo.extend(c for c in (cur.__cause__, cur.__context__, getattr(cur, "reason", None))
                     if isinstance(c, BaseException))
    return None


def parse_proxy(url: str) -> dict:
    """Convert user:pass@host:port URL into Playwright proxy dict."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    try:
        port = parsed.port
    except ValueError:
        port = None  # its message quotes a piece of the URL, which may be the password
    if not parsed.hostname or not port:
        # Never the URL itself: it carries the slot's password.
        raise ValueError("Proxy URL missing host or port (percent-encode any / ? # "
                         "in the password)")
    return {
        "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        "username": parsed.username or "",
        "password": parsed.password or "",
    }


BRAVE_LOGIN_HINT = ("Open Brave, go to https://www.linkedin.com/ and log in "
                    "(complete any challenge), then rerun this script.")


def brave_executable() -> Path:
    """The Brave binary `scripts/browser.py` launches on this platform, checked to exist."""
    exe = _browser_paths("brave")["exe"]
    if not exe.is_file():
        raise FileNotFoundError(f"Brave not found at {exe}. Install Brave, or pass "
                                "--engine chromium for Playwright's bundled Chromium.")
    return exe


def brave_cookies_for_playwright(profile_name: str = "Default") -> list[dict]:
    """Read a Brave profile's LinkedIn cookies and reshape them for context.add_cookies().

    The store is read read-only through an SQLite backup snapshot, so a running
    Brave holding the profile is not disturbed. A cookie that matched and could
    not be decrypted is reported as that, not as a missing login: the two need
    opposite repairs.
    """
    raw, failures = _read_cookies("linkedin.com", profile_name=profile_name, browser="brave")
    if "li_at" not in raw:
        undecryptable = [reason for name, _host, reason in failures if name == "li_at"]
        if undecryptable:
            raise RuntimeError(f"li_at cookie in Brave profile {profile_name} could not be "
                               f"decrypted: {undecryptable[0]}")
        raise RuntimeError(f"li_at cookie missing from Brave profile {profile_name}: "
                           f"not logged in to LinkedIn there. {BRAVE_LOGIN_HINT}")
    cookies: list[dict] = []
    for name, (_host, value, _attrs) in raw.items():
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".linkedin.com",
            "path": "/",
            "secure": True,
            "httpOnly": name in {"li_at", "JSESSIONID", "bcookie", "bscookie"},
            "sameSite": "None",
        })
    return cookies


def browser_user_agent(browser) -> str:
    """The user agent the launched Chromium-family browser itself reports.

    Read over the browser-level CDP session Playwright already holds, so it
    names the real build and platform rather than a string written here. A
    headless build says `HeadlessChrome`, which is a bot marker; that one word
    is replaced, nothing else. Brave reports the same token as Chrome.
    """
    cdp = browser.new_browser_cdp_session()
    try:
        ua = cdp.send("Browser.getVersion")["userAgent"]
    finally:
        cdp.detach()
    return ua.replace("HeadlessChrome/", "Chrome/")


def check_session_alive(cookies: dict[str, str], slug: str,
                        proxy_url: str | None = None,
                        user_agent: str = "") -> tuple[bool, str]:
    """Probe a strictly auth-gated endpoint to confirm the li_at session is live.

    Uses `/in/{slug}/` which requires auth for a real response. `/feed/` is a false
    positive: it returns 200 for everyone. LinkedIn signals session termination by
    setting `li_at=delete me; Max-Age=0` in the response.
    Returns (is_alive, reason).

    `proxy_url` is the SAME slot the browser launches with, so one li_at never
    travels between two source IPs in one run (a rotating slot can still change
    exit IP per request; matching slots is the closest guarantee available).

    Raises `ProxyRefused` when the failure came from the SOCKS proxy itself,
    because "proceeding anyway" through a slot that refuses its own credentials
    only moves the same failure into the browser, where it reads as a network
    error.
    """
    try:
        import requests
    except ImportError:
        return True, "requests not available - skipping pre-flight"
    from requests.cookies import RequestsCookieJar
    jar = RequestsCookieJar()
    for n, v in cookies.items():
        jar.set(n, v, domain=".linkedin.com", path="/")
    if proxy_url and "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        r = requests.get(
            f"https://www.linkedin.com/in/{slug}/",
            cookies=jar,
            headers={"User-Agent": user_agent} if user_agent else None,
            timeout=10,
            allow_redirects=False,
            proxies=proxies,
        )
    except requests.RequestException as exc:
        refusal = socks_refusal(exc) if proxy_url else None
        if refusal:
            raise ProxyRefused(refusal) from exc
        return True, f"pre-flight probe failed ({exc}) - proceeding anyway"
    set_cookie = r.headers.get("Set-Cookie", "")
    if "li_at=delete me" in set_cookie or "li_at=\"delete me\"" in set_cookie:
        return False, "server set `li_at=delete me` - session terminated"
    if r.status_code == 429:
        return False, "429 Too Many Requests - back off and retry in 10+ minutes"
    if r.status_code == 302:
        # Only the walls count: a redirect to `/in/{slug}` is LinkedIn's own
        # canonicalisation of a valid session (2026-09-02).
        loc = r.headers.get("Location", "")
        if "/authwall" in loc or "/login" in loc:
            return False, f"bounced to {loc[:80]} - session invalidated"
    return True, f"status={r.status_code}"


def scroll_until(page, min_posts: int, max_scrolls: int = 10) -> int:
    """Scroll the feed until at least min_posts activity cards are present."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    selector = '[data-urn*="urn:li:activity:"], div.feed-shared-update-v2'
    for i in range(max_scrolls):
        count = page.locator(selector).count()
        if count >= min_posts:
            return count
        page.evaluate("window.scrollBy(0, document.body.scrollHeight);")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            time.sleep(1.5)  # feed may have already rendered; proceed
    return page.locator(selector).count()


def _activity_id(urn: str) -> int:
    """The numeric id inside `urn:li:activity:<id>`, 0 when there is none."""
    m = re.search(r"urn:li:activity:(\d+)", urn)
    return int(m.group(1)) if m else 0


def extract_posts(html: str, limit: int) -> list[dict]:
    """Parse rendered HTML for post data.

    Strategy: walk bpr-guid JSON blobs (post-JS-execution those are populated with
    Voyager UpdateV2 objects for each visible post). Fall back to JSON-LD and
    regex-on-DOM if bpr is empty.
    """
    posts: dict[str, dict] = {}  # urn -> record

    # Strategy 1: bpr-guid Voyager blobs
    bpr = re.findall(r'<code[^>]*id="bpr-guid-\d+"[^>]*>(.*?)</code>', html, re.DOTALL)
    for blob in bpr:
        try:
            data = json.loads(unescape(blob))
        except json.JSONDecodeError:
            continue
        included = data.get("included") or []
        for obj in included:
            t = obj.get("$type", "")
            urn = obj.get("updateMetadata", {}).get("urn") or obj.get("urn", "")
            if "UpdateV2" not in t and "FeedUpdate" not in t:
                continue
            if not urn or "urn:li:activity:" not in urn:
                continue
            rec = posts.setdefault(urn, {"urn": urn})
            # Text commentary
            commentary = obj.get("commentary") or {}
            text = commentary.get("text", {}).get("text") if isinstance(commentary.get("text"), dict) else commentary.get("text")
            if text and "text" not in rec:
                rec["text"] = text
            # Engagement counts
            social = obj.get("socialDetail") or {}
            counts = social.get("totalSocialActivityCounts") or {}
            for k in ("numLikes", "numComments", "numShares", "numImpressions"):
                if k in counts and k not in rec:
                    rec[k] = counts[k]

    # Strategy 2: JSON-LD (always available, but only exposes likes + text)
    jsonld = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    if jsonld:
        try:
            g = json.loads(jsonld.group(1)).get("@graph", [])
            for node in g:
                if node.get("@type") != "DiscussionForumPosting":
                    continue
                url = node.get("mainEntityOfPage") or node.get("url") or ""
                m = re.search(r"activity-(\d+)", url)
                if not m:
                    continue
                urn = f"urn:li:activity:{m.group(1)}"
                rec = posts.setdefault(urn, {"urn": urn})
                rec.setdefault("text", node.get("text") or node.get("headline") or "")
                rec.setdefault("date", node.get("datePublished", ""))
                rec.setdefault("url", url)
                likes = node.get("interactionStatistic", {}).get("userInteractionCount")
                if likes is not None:
                    rec.setdefault("numLikes", likes)
        except (json.JSONDecodeError, AttributeError):
            pass

    # Backfill URL from urn when absent
    for urn, rec in posts.items():
        rec.setdefault("url", f"https://www.linkedin.com/feed/update/{urn}/")

    # Sort by date desc, then by the activity id NUMERICALLY (its high bits are
    # a creation time; as text a 19-digit id would sort below an 18-digit one).
    ordered = sorted(
        posts.values(),
        key=lambda r: (r.get("date", ""), _activity_id(r.get("urn", ""))),
        reverse=True,
    )
    return ordered[:limit]


def render_markdown(posts: list[dict], slug: str, egress: str) -> str:
    """`egress` is stated by the caller and has no default on purpose, so the
    artifact can never record a route the traffic did not take (HOS-140)."""
    lines = [f"# LinkedIn Recent Activity - {slug}", "",
             f"_Scraped via Playwright + Brave auth, {egress}_", ""]
    for i, p in enumerate(posts, 1):
        metrics = []
        for label, key in [("likes", "numLikes"), ("comments", "numComments"), ("shares", "numShares"), ("views", "numImpressions")]:
            if key in p:
                metrics.append(f"{p[key]} {label}")
        header = f"## [{i}] {p.get('date', '')[:10] or 'unknown date'}"
        if metrics:
            header += f" - {', '.join(metrics)}"
        lines.append(header)
        lines.append("")
        lines.append(f"URL: {p.get('url', '')}")
        lines.append("")
        text = p.get("text", "").strip()
        lines.append(text if text else "_[no text extracted]_")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--slug", required=True, help="LinkedIn profile slug, e.g. example-profile")
    parser.add_argument("--limit", type=int, default=3, help="Posts to extract (default 3)")
    parser.add_argument("--profile", default="Default",
                        help="Brave profile the LinkedIn cookies are read from, by display "
                             "name or folder name (default Default)")
    add_route_argument(parser, "Authenticated LinkedIn sessions typically fail through "
                               "proxies due to IP-travel detection; choose direct unless you "
                               "know the Brave session was established via that Decodo slot.")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--timeout", type=int, default=45000, help="Per-action timeout (ms)")
    parser.add_argument("--engine", default="brave", choices=["brave", "chromium"],
                        help="Browser to launch (default brave, the installed Brave through "
                             "Playwright's chromium driver, the same browser the cookies come "
                             "from; chromium = Playwright's bundled Chromium).")
    args = parser.parse_args()

    load_env()
    # The route is settled first: no cookie read, relay or browser before it.
    args.proxy_slot = resolve_route(args.proxy_slot, tool="linkedin-activity.py")
    if args.proxy_slot is None:
        return EXIT_ROUTE_NOT_CHOSEN

    import os
    proxy_cfg = None
    proxy_url = None
    relay = None
    if args.proxy_slot:
        proxy_url = os.environ.get(f"DECODO_PROXY_{args.proxy_slot}")
        if not proxy_url:
            print(f"{RED}ERROR: DECODO_PROXY_{args.proxy_slot} not in .env{RESET}", file=sys.stderr)
            return 1
        if proxy_scheme(proxy_url) in SOCKS_SCHEMES:
            try:
                relay = SocksRelay(proxy_url)
            except ValueError as exc:
                print(f"{RED}ERROR: DECODO_PROXY_{args.proxy_slot}: {exc}{RESET}", file=sys.stderr)
                return 1
            except ImportError:
                print(f"{RED}ERROR: DECODO_PROXY_{args.proxy_slot} is a SOCKS5 slot and PySocks "
                      f"is not installed{RESET}", file=sys.stderr)
                print(f"{YELLOW}FIX: uv sync --extra browser{RESET}", file=sys.stderr)
                return 1
        else:
            try:
                proxy_cfg = parse_proxy(proxy_url)
            except ValueError as exc:
                print(f"{RED}ERROR: DECODO_PROXY_{args.proxy_slot}: {exc}{RESET}", file=sys.stderr)
                return 1

    try:
        cookies = brave_cookies_for_playwright(args.profile)
        executable = brave_executable() if args.engine == "brave" else None
    except (FileNotFoundError, RuntimeError, ValueError, ImportError, sqlite3.Error) as exc:
        print(f"{RED}ERROR: {exc}{RESET}", file=sys.stderr)
        return 2
    print(f"{GRAY}Loaded {len(cookies)} cookies from Brave profile {args.profile}{RESET}")
    raw_cookies = {c["name"]: c["value"] for c in cookies}

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    url = f"https://www.linkedin.com/in/{args.slug}/recent-activity/all/"
    # Page dumps are runtime junk (re-scraped on the next run), so they go to
    # the browser service's tmp kind and never under `outputs/`.
    out_dir = data_write_dir("runtime:browser/tmp")

    # The relay is entered first so it outlives the browser, and leaves with it.
    with relay or contextlib.nullcontext(), sync_playwright() as pw:
        launch_kwargs = {"headless": not args.headed}
        if relay:
            launch_kwargs["proxy"] = {"server": relay.url}
        elif proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        if executable:
            launch_kwargs["executable_path"] = str(executable)
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            ua = browser_user_agent(browser)
            # The pre-flight runs after the launch so it can send the browser's
            # own user agent, and through the same slot the browser uses, so
            # both requests leave from one egress rather than travelling
            # between two IPs on one cookie.
            try:
                alive, reason = check_session_alive(raw_cookies, args.slug, proxy_url, ua)
            except ProxyRefused as exc:
                print(f"{RED}ERROR: Decodo proxy slot {args.proxy_slot} refused the "
                      f"connection: {exc}{RESET}", file=sys.stderr)
                return EXIT_PROXY_REFUSED
            if not alive:
                print(f"{RED}ERROR: LinkedIn session invalid - {reason}{RESET}", file=sys.stderr)
                print(f"{YELLOW}FIX: {BRAVE_LOGIN_HINT}{RESET}", file=sys.stderr)
                return 3
            print(f"{GRAY}Pre-flight: session alive ({reason}){RESET}")
            context = browser.new_context(
                user_agent=ua,
                locale="en-US",
                timezone_id=get_default_tz_name(),
                viewport={"width": 1440, "height": 900},
            )
            context.add_cookies(cookies)
            page = context.new_page()
            page.set_default_timeout(args.timeout)
            print(f"{CYAN}Navigating to {url}{RESET}")
            try:
                response = page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError:
                if relay and relay.last_error:
                    print(f"{RED}ERROR: Decodo proxy slot {args.proxy_slot} refused the "
                          f"connection: {relay.last_error}{RESET}", file=sys.stderr)
                    return EXIT_PROXY_REFUSED
                raise
            status = response.status if response else None
            try:
                page.wait_for_load_state("networkidle", timeout=args.timeout)
            except PlaywrightTimeout:
                # Continue on the page as loaded: the feed's long-poll sockets
                # keep networkidle from settling, and the checks below work on
                # a partial page.
                print(f"{YELLOW}networkidle did not settle within {args.timeout}ms; "
                      f"continuing with the page as loaded{RESET}")

            if status == 429 or "/hp?" in page.url or "Too Many Requests" in page.content()[:2000]:
                print(f"{RED}ERROR: LinkedIn returned 429 (rate limited on this IP).{RESET}", file=sys.stderr)
                print(f"{YELLOW}FIX: wait 10-15 minutes, or switch Mullvad exit for a fresh IP, then rerun.{RESET}", file=sys.stderr)
                return 5
            if "/login" in page.url or "/authwall" in page.url:
                print(f"{RED}ERROR: hit auth wall at {page.url}. li_at cookie may be expired.{RESET}", file=sys.stderr)
                return 3

            count = scroll_until(page, min_posts=args.limit + 1, max_scrolls=8)
            print(f"{GRAY}Activity cards visible after scroll: {count}{RESET}")

            html = page.content()
            (out_dir / "linkedin-activity-rendered.html").write_text(html, encoding="utf-8")
        finally:
            browser.close()

    posts = extract_posts(html, limit=args.limit)
    if not posts:
        print(f"{YELLOW}WARNING: no posts extracted. See linkedin-activity-rendered.html{RESET}")
        return 4

    egress = (f"Decodo proxy slot {args.proxy_slot}" if args.proxy_slot
              else "direct connection, no proxy")
    md = render_markdown(posts, args.slug, egress)
    md_path = out_dir / "linkedin-activity-auth.md"
    json_path = out_dir / "linkedin-activity-auth.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{BOLD}{GREEN}Extracted {len(posts)} post(s){RESET}")
    print(f"  Markdown: {md_path.resolve()}")
    print(f"  JSON:     {json_path.resolve()}")
    print(f"  HTML:     {(out_dir / 'linkedin-activity-rendered.html').resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
