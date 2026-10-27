#!/usr/bin/env python3
"""Source adapters for the update manager: resolve the latest version of a
component from GitHub releases, PyPI, the npm registry, the Node.js release
index, or a local command that reads a package manager's own cache. Reads only.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from typing import Any
from urllib.error import HTTPError, URLError

from scripts.utils.user_agent import user_agent


class SourceError(Exception):
    """Raised when a source cannot be reached or parsed."""


def _get_json(url: str) -> dict[str, Any]:
    body = _fetch(url)
    # A 200 whose body is an array or scalar must be a SourceError, so the
    # update manager marks ONE component unresolved rather than losing the check.
    if not isinstance(body, dict):
        raise SourceError(f"{url} returned {type(body).__name__}, not a JSON object")
    return body


def _fetch(url: str, as_json: bool = True) -> Any:
    """The body of an https GET: decoded JSON of any top-level type, or text.

    Split out of `_get_json` because the Node.js release index is a JSON ARRAY,
    which `_get_json` rightly refuses for every source that promises an object,
    and because one vendor publishes its latest version only as a text file.
    """
    # The scheme is CHECKED, not asserted. Both suppressions below used to be
    # justified with "https literal", and `url` is a caller-supplied parameter:
    # nothing enforced the scheme, so an `http://` or `file:///etc/passwd` URL
    # would have been opened under an annotation claiming it was a vetted
    # literal. The guard is what makes the justification true.
    if not url.startswith("https://"):
        raise SourceError(f"refusing a non-https source URL: {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})  # noqa: S310 - https enforced above
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - https enforced above
            text = resp.read().decode("utf-8")
        return json.loads(text) if as_json else text
    except HTTPError as exc:
        raise SourceError(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise SourceError(f"network error for {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"bad JSON from {url}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # A non-UTF-8 body is a sibling of JSONDecodeError, not an instance,
        # and escaped every clause here (MEASURED 2026-09-01).
        raise SourceError(f"undecodable body from {url}: {exc}") from exc
    except (TimeoutError, OSError) as exc:
        # A read timeout is a TimeoutError (an OSError), not a URLError
        # (measured 2026-08-26). Last, so the clauses above keep their messages.
        raise SourceError(f"network error for {url}: {exc}") from exc


def fetch_text(url: str) -> str:
    """The text body of an https GET, for a reader that wants the page itself.

    The documentation watch (`update_docwatch`) snapshots pages rather than
    reading a version out of them; it goes through `_fetch` so the https
    check, the User-Agent and the error mapping above are the same ones every
    version probe gets, and so no second urllib call site exists to drift.
    """
    return _fetch(url, as_json=False)


def _strip_v(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def run_probe(cmd: str, regex: str | None = None, timeout: int = 60) -> str:
    """Run one local read-only command and return the version it prints.

    With `regex`, the first capture group of its first match; without, the
    first line of output. "" whenever the command could not run, exited
    non-zero, printed nothing, or the regex did not match, because "" is this module's word for
    "not answered" and a line of noise in a version cell is a guess.

    Shared by the `current` side (`update_common.resolve_current`) and the
    `latest: {via: shell}` side below, so the two cannot drift. Output is
    decoded with `errors="replace"`: a stray non-UTF-8 byte in a vendor banner
    is never part of the digits a regex captures, and a strict decode raised
    `UnicodeDecodeError` past every handler here (MEASURED 2026-09-01 with
    `printf 'v1.2.3 \\xe9dition\\n'`, recorded at `resolve_current`).
    """
    try:
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                             errors="replace", timeout=timeout, check=False)
    except (subprocess.SubprocessError, OSError):
        return ""
    # A command that failed has not measured anything, whatever it printed
    # first: partial or cached output before a non-zero exit is not a version.
    # Every probe in the registry exited 0 when this was added (MEASURED
    # 2026-09-26, 25 commands), so no live probe loses its answer to it.
    if res.returncode != 0:
        return ""
    out = res.stdout.strip()
    if not out:
        return ""
    if not regex:
        return out.splitlines()[0]
    try:
        m = re.search(regex, out, re.MULTILINE)
    except re.error:
        return ""
    return m.group(1) if (m and m.groups()) else ""


def _node_lts(spec: dict[str, Any], current: str) -> str:
    """Newest LTS release of the installed Node.js major, from the index nvm reads.

    Same major as `current` because that is the line nvm keeps the globals
    under; a new major is a migration, not an update. With no current version
    there is no major to ask about, and the answer is "not answered".
    """
    m = re.match(r"v?(\d+)\.", current or "")
    if not m:
        raise SourceError("no installed Node.js version, so no major to look up")
    major = m.group(1)
    body = _fetch(spec.get("index", "https://nodejs.org/dist/index.json"))
    if not isinstance(body, list):
        raise SourceError(f"Node.js index returned {type(body).__name__}, not a list")
    for entry in body:          # the index is newest first
        if not isinstance(entry, dict) or not entry.get("lts"):
            continue
        version = str(entry.get("version", ""))
        if version.startswith(f"v{major}."):
            return _strip_v(version)
    raise SourceError(f"no LTS release of Node.js {major} in the index")


def _https_version(spec: dict[str, Any]) -> str:
    """A vendor's own "latest" document: a JSON `field`, or a `regex` over text.

    For tools that publish no release API: a JSON manifest the tool's own
    updater reads, or a release note with a fixed `version:` line. A document
    that does not carry the version is SourceError, never a guess.
    """
    url = spec.get("url", "")
    if spec.get("field"):
        body = _get_json(url)
        value = body.get(spec["field"])
        if not isinstance(value, str) or not value.strip():
            raise SourceError(f"{url} carries no `{spec['field']}` string")
        return _strip_v(value.strip())
    regex = spec.get("regex")
    if not regex:
        raise SourceError("an https source needs `field` or `regex`")
    try:
        m = re.search(regex, _fetch(url, as_json=False), re.MULTILINE)
    except re.error as exc:
        raise SourceError(f"bad regex {regex!r}: {exc}") from exc
    if not (m and m.groups()):
        raise SourceError(f"{url} did not match {regex!r}")
    return m.group(1)


def latest_version(spec: dict[str, Any], current: str = "") -> str:
    """The latest version `spec` names, or SourceError.

    `current` is consulted only by a source whose answer depends on what is
    installed (`node_lts`, and `zero_outdated`, which answers the target of a
    count rather than a version).
    """
    via = spec.get("via")
    if via == "github_release":
        data = _get_json(f"https://api.github.com/repos/{spec['repo']}/releases/latest")
        return _strip_v(data.get("tag_name", ""))
    if via == "pypi":
        data = _get_json(f"https://pypi.org/pypi/{spec['package']}/json")
        return data.get("info", {}).get("version", "")
    if via == "npm":
        data = _get_json(f"https://registry.npmjs.org/{spec['package']}/latest")
        return data.get("version", "")
    if via == "https":
        return _https_version(spec)
    if via == "node_lts":
        return _node_lts(spec, current)
    if via == "shell":
        # A package manager's own cache (`apt-cache policy`, `snap info`) or a
        # tool's own dry run (`uv self update --dry-run`). Read-only by the
        # registry's contract; a command that answers nothing is SourceError,
        # never a guessed version.
        found = run_probe(spec.get("cmd", ""), spec.get("regex"))
        if not found:
            raise SourceError(f"`{spec.get('cmd', '')}` printed no version")
        return found
    if via == "zero_outdated":
        # The target state of a component measured as a count of outdated
        # parts: zero. Only meaningful beside a `current` that counts.
        if not current:
            raise SourceError("the count it compares against was not answered")
        return ZERO_OUTDATED
    raise SourceError(f"unknown source via={via!r}")


ZERO_OUTDATED = "0 outdated"


def github_asset_url(spec: dict[str, Any], arch: str = "amd64") -> str | None:
    """URL of the latest-release linux/<arch> plugin tarball, or None."""
    data = _get_json(f"https://api.github.com/repos/{spec['repo']}/releases/latest")
    for asset in data.get("assets") or []:
        # `.get`, not `asset["name"]`: an asset entry missing `name` (or not a
        # mapping at all) raised KeyError/TypeError out of a function whose only
        # documented failure is SourceError. An asset this loop cannot read is
        # an asset that does not match.
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).lower()
        if "linux" in name and arch in name and name.endswith(".tar.gz") \
                and "no-plugin" not in name:
            return asset.get("browser_download_url")
    return None
