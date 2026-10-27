#!/usr/bin/env python3
"""Documentation watch for the update manager: every 14 days, fetch the pages
that describe the protocols and tools this workspace builds on, diff each
against the snapshot from the previous run, and write one report per run.

It reads and reports. It never edits code: adapting the workspace to what a
report shows is engine work, done in a yard on the operator's word. Operator
decision 2026-09-30, translated: "the check automatic, the adaptation on your
word" was HELM's proposal and his answer was "yes". (translated)

The sources are the `doc_sources:` list in `config/update-registry.yaml`, each
with a note saying where its address was read. Snapshots, the run stamp and the
reports live in the data repository under three registry names
(`doc-watch-snapshots`, `doc-watch-state`, `doc-watch-reports`), never at a
path built against the checkout.

What a failed fetch means is said, never implied. A source that could not be
fetched is "not checked: <reason>" in the report and counts toward the `/prime`
line; it is never folded into "no changes". TLS verification is always on,
because this machine sometimes sits behind a network that presents a foreign
certificate, and a page read through an interceptor is not the page.
"""
from __future__ import annotations

import difflib
import json
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import yaml

from scripts.utils import update_sources
from scripts.utils.atomic import atomic_write_text
from scripts.utils.data_path import data_write_dir

INTERVAL = timedelta(days=14)
KINDS = {"markdown", "html", "links", "github_latest_release", "npm_latest"}
#: Diff lines quoted per changed section in the report; the rest is counted.
EXCERPT_LINES = 40
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class DocSourceError(Exception):
    """A `doc_sources:` entry that cannot be used as written."""


@dataclass
class DocSource:
    id: str
    url: str
    kind: str
    provenance: str
    pattern: str = ""
    fields: list[str] = field(default_factory=list)


def load_doc_sources(path: Path) -> list[DocSource]:
    """The `doc_sources:` list of the registry file, validated.

    A malformed entry refuses the whole list rather than being skipped: a
    source dropped in silence reads in the report exactly like a source that
    has not changed.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DocSourceError(f"cannot read {path}: {exc}") from exc
    entries = raw.get("doc_sources") if isinstance(raw, dict) else None
    if not isinstance(entries, list) or not entries:
        raise DocSourceError(f"{path.name} has no `doc_sources:` list")
    out: list[DocSource] = []
    seen: set[str] = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise DocSourceError(f"doc_sources[{i}] is not a mapping")
        sid, url, kind = e.get("id"), e.get("url"), e.get("kind")
        prov = e.get("provenance")
        if not isinstance(sid, str) or not _ID_RE.match(sid) or sid in seen:
            raise DocSourceError(f"doc_sources[{i}]: id {sid!r} missing, malformed or repeated")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise DocSourceError(f"{sid}: url must be https")
        if kind not in KINDS:
            raise DocSourceError(f"{sid}: kind {kind!r} not in {sorted(KINDS)}")
        if not isinstance(prov, str) or not prov.strip():
            raise DocSourceError(f"{sid}: every source says where its address was read (provenance)")
        pattern = e.get("pattern", "")
        if kind == "links":
            try:
                re.compile(pattern)
            except (re.error, TypeError) as exc:
                raise DocSourceError(f"{sid}: bad pattern: {exc}") from exc
            if not pattern:
                raise DocSourceError(f"{sid}: kind links needs a pattern")
        fields = e.get("fields") or []
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            raise DocSourceError(f"{sid}: fields must be a list of strings")
        seen.add(sid)
        out.append(DocSource(sid, url, kind, prov.strip(), pattern, fields))
    return out


# ---------------------------------------------------------------- normalising

class _TextExtractor(HTMLParser):
    """Visible text of a page, headings marked `#`, chrome left out."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "template", "iframe"}
    BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "pre", "table",
             "ul", "ol", "dd", "dt", "blockquote", "main", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._heading = 0

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text:
            self.lines.append(("#" * self._heading + " " + text) if self._heading else text)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif not self._skip and re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self._heading = int(tag[1])
        elif not self._skip and tag in self.BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif not self._skip and re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self._heading = 0
        elif not self._skip and tag in self.BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._buf.append(data)


def html_to_text(body: str) -> str:
    parser = _TextExtractor()
    parser.feed(body)
    parser.close()
    parser._flush()
    return "\n".join(parser.lines) + "\n"


#: Invisible characters a page may carry (Docusaurus puts U+200B after every
#: heading, MEASURED 2026-09-30 on the Hindsight docs). Removed so no snapshot
#: or report carries one; `.claude/rules/hidden-chars.md`.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad"))
_INVISIBLE[0xA0] = " "


def normalise(src: DocSource, body: str) -> str:
    """The text a snapshot holds: stable across fetches of an unchanged page."""
    return _normalise(src, body).translate(_INVISIBLE)


def _normalise(src: DocSource, body: str) -> str:
    if src.kind == "markdown":
        return "\n".join(line.rstrip() for line in body.strip().splitlines()) + "\n"
    if src.kind == "html":
        return html_to_text(body)
    if src.kind == "links":
        found = sorted(set(re.findall(src.pattern, body)))
        found = [m if isinstance(m, str) else "/".join(m) for m in found]
        return "# Matches\n" + "".join(f"{m}\n" for m in found)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    if src.kind == "github_latest_release":
        head = f"# Release {data.get('tag_name', '?')}\npublished: {data.get('published_at', '?')}\n"
        return head + (data.get("body") or "").replace("\r\n", "\n").strip() + "\n"
    # npm_latest: the chosen fields, one section each, keys sorted.
    out = []
    for name in src.fields or ["version", "description"]:
        value = data.get(name)
        rendered = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) \
            if isinstance(value, (dict, list)) else str(value)
        out.append(f"# {name}\n{rendered}\n")
    return "".join(out)


# ------------------------------------------------------------------- diffing

def sections(text: str) -> dict[str, str]:
    """Body per heading; a repeated heading gets ` (2)`, ` (3)` appended."""
    out: dict[str, list[str]] = {}
    key = "(preamble)"
    counts: dict[str, int] = {}
    out[key] = []
    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            title = m.group(2)
            counts[title] = counts.get(title, 0) + 1
            key = title if counts[title] == 1 else f"{title} ({counts[title]})"
            out[key] = []
        else:
            out[key].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items() if k != "(preamble)" or "\n".join(v).strip()}


@dataclass
class SourceDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: dict[str, list[str]] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


def diff_texts(old: str, new: str) -> SourceDiff:
    a, b = sections(old), sections(new)
    d = SourceDiff()
    d.added = [k for k in b if k not in a]
    d.removed = [k for k in a if k not in b]
    for k in b:
        if k in a and a[k] != b[k]:
            d.changed[k] = list(difflib.unified_diff(
                a[k].splitlines(), b[k].splitlines(), lineterm="", n=1))[2:]
    return d


# ------------------------------------------------------------------- running

class FetchFailed(Exception):
    """A source that could not be read; its message is the report's reason."""


def fetch(url: str) -> str:
    """The body at `url`, TLS verified, the workspace User-Agent.

    Through `update_sources.fetch_text`, the adapter every version probe uses,
    which enforces https and never builds an SSL context of its own, so the
    default context with verification ON is the only one in play. A
    certificate the system does not trust is named as such, because on this
    machine it usually means somebody in the path is reading the traffic.
    """
    try:
        return update_sources.fetch_text(url)
    except update_sources.SourceError as exc:
        cause = getattr(exc.__cause__, "reason", exc.__cause__)
        if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
            raise FetchFailed("TLS certificate not trusted (possible interception); "
                              f"not bypassed: {exc}") from exc
        raise FetchFailed(str(exc)) from exc


@dataclass
class SourceResult:
    source: DocSource
    status: str                 # baseline | unchanged | changed | not-checked
    reason: str = ""
    diff: SourceDiff | None = None


@dataclass
class Paths:
    state: Path
    snapshots: Path
    reports: Path


def read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # Unreadable is not "never run": say so, then treat it as due, which
        # costs one extra run and loses nothing.
        print(f"[doc-watch] state unreadable ({exc}); treating the watch as due")
        return {}
    return data if isinstance(data, dict) else {}


def is_due(state: dict[str, Any], now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(state["last_run"])
    except (KeyError, TypeError, ValueError):
        return True
    return now - last >= INTERVAL


def check_sources(sources: list[DocSource], snapshots: Path,
                  fetcher: Callable[[str], str] = fetch) -> list[SourceResult]:
    results = []
    for src in sources:
        try:
            text = normalise(src, fetcher(src.url))
        except FetchFailed as exc:
            results.append(SourceResult(src, "not-checked", str(exc)))
            continue
        except (ValueError, re.error) as exc:
            results.append(SourceResult(src, "not-checked", f"could not normalise the body: {exc}"))
            continue
        if not text.strip():
            results.append(SourceResult(src, "not-checked", "the page normalised to no text"))
            continue
        snap = snapshots / f"{src.id}.txt"
        try:
            old = snap.read_text(encoding="utf-8")
        except FileNotFoundError:
            old = None
        if old is None:
            results.append(SourceResult(src, "baseline"))
        else:
            d = diff_texts(old, text)
            results.append(SourceResult(src, "changed" if d.count else "unchanged", diff=d))
        atomic_write_text(snap, text)
    return results


def render_report(results: list[SourceResult], now: datetime) -> str:
    changed = sum(r.diff.count for r in results if r.status == "changed" and r.diff)
    failed = sum(r.status == "not-checked" for r in results)
    out = [f"# Documentation watch, {now.strftime('%Y-%m-%d %H:%M UTC')}", "",
           f"{len(results)} sources; {changed} changed items; {failed} not checked.", "",
           "Nothing here was applied. Adapting the workspace to a change is done in a "
           "yard on the operator's word.", ""]
    for r in results:
        out += [f"## {r.source.id}", "", f"URL: {r.source.url}", ""]
        if r.status == "not-checked":
            out += [f"not checked: {r.reason}", ""]
            continue
        if r.status == "baseline":
            out += ["baseline recorded: first snapshot, nothing to compare against", ""]
            continue
        if r.status == "unchanged" or r.diff is None:
            out += ["no changes since the previous snapshot", ""]
            continue
        d = r.diff
        for label, items in (("Added", d.added), ("Removed", d.removed)):
            if items:
                out += [f"{label} sections:", *[f"- {s}" for s in items], ""]
        for title, lines in d.changed.items():
            out += [f"Changed: {title}", "", "```diff"]
            out += lines[:EXCERPT_LINES]
            if len(lines) > EXCERPT_LINES:
                out.append(f"... {len(lines) - EXCERPT_LINES} more diff lines")
            out += ["```", ""]
    return "\n".join(out)


def run(sources: list[DocSource], paths: Paths, *, force: bool = False,
        now: datetime | None = None, fetcher: Callable[[str], str] = fetch) -> dict[str, Any] | None:
    """One watch run, or None when it is not due. Returns the new state.

    A run in which EVERY source failed does not move `last_run`, so an offline
    morning is retried the next day instead of waiting fourteen; it still writes
    its report and records the attempt, because a silent retry is a failure
    nobody sees.
    """
    now = now or datetime.now(timezone.utc)
    state = read_state(paths.state)
    if not force and not is_due(state, now):
        return None
    results = check_sources(sources, paths.snapshots, fetcher)
    report = paths.reports / f"{now.strftime('%Y-%m-%d_%H%M%S')}_doc-watch.md"
    atomic_write_text(report, render_report(results, now))
    changed = sum(r.diff.count for r in results if r.status == "changed" and r.diff)
    failed = [r.source.id for r in results if r.status == "not-checked"]
    new_state = {
        "last_attempt": now.isoformat(),
        "last_run": state.get("last_run") if failed and len(failed) == len(results) else now.isoformat(),
        "report": str(report),
        "changed": changed,
        "not_checked": failed,
        "sources": {r.source.id: r.status for r in results},
    }
    atomic_write_text(paths.state, json.dumps(new_state, indent=1))
    return new_state


def prime_line(state: dict[str, Any]) -> str:
    """The one `/prime` line, or "" when the last report holds nothing to act on."""
    changed = state.get("changed") or 0
    failed = state.get("not_checked") or []
    if not changed and not failed:
        return ""
    parts = [f"{changed} changed item{'s' if changed != 1 else ''}"]
    if failed:
        parts.append(f"{len(failed)} not checked ({', '.join(failed)})")
    return f"MCP docs watch: {'; '.join(parts)}; report: {state.get('report', '?')}"


# ----------------------------------------------------------------- self-test

def self_test() -> int:
    """Every scenario on invented fixtures, in a temporary tree, no network."""
    import tempfile
    import urllib.error
    import urllib.request
    from unittest import mock

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    src = DocSource("example-spec", "https://docs.example.invalid/changelog.md", "markdown", "invented")
    v1 = "# Changelog\n\n## Tools\nlist\ncall\n\n## Sampling\nold rule\n"
    v2 = "# Changelog\n\n## Tools\nlist\ncall\nstream\n\n## Tasks\nnew\n"
    cases: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory(dir=data_write_dir("runtime:scratch/tmp")) as tmp:
        root = Path(tmp)
        paths = Paths(root / "state.json", root / "snap", root / "reports")
        body = {"text": v1}

        def fake(_url: str) -> str:
            return body["text"]

        first = run([src], paths, now=t0, fetcher=fake)
        cases.append(("first run records a baseline, reports no change",
                      bool(first) and first["sources"] == {"example-spec": "baseline"}
                      and prime_line(first) == ""))
        cases.append(("not due within 14 days returns at once",
                      run([src], paths, now=t0 + timedelta(days=13), fetcher=fake) is None))
        s = run([src], paths, now=t0 + timedelta(days=14), fetcher=fake)
        cases.append(("due after 14 days runs; unchanged snapshot reports nothing",
                      bool(s) and s["sources"]["example-spec"] == "unchanged" and prime_line(s) == ""))
        body["text"] = v2
        s = run([src], paths, now=t0 + timedelta(days=15), force=True, fetcher=fake)
        rep = Path(s["report"]).read_text(encoding="utf-8") if s else ""
        cases.append(("--force runs when not due", bool(s)))
        cases.append(("changed snapshot reports added, removed and changed",
                      bool(s) and s["changed"] == 3 and "- Tasks" in rep and "- Sampling" in rep
                      and "Changed: Tools" in rep and "+stream" in rep))
        cases.append(("/prime line appears on change",
                      bool(s) and prime_line(s).startswith("MCP docs watch: 3 changed items")))

        def boom(_url: str) -> str:
            raise FetchFailed("HTTP 503 for https://docs.example.invalid/changelog.md")

        s = run([src], paths, now=t0 + timedelta(days=40), fetcher=boom)
        rep = Path(s["report"]).read_text(encoding="utf-8") if s else ""
        cases.append(("fetch failure reported as not checked, never as no changes",
                      "not checked: HTTP 503" in rep and "no changes" not in rep))
        cases.append(("/prime line appears on failure",
                      bool(s) and "1 not checked (example-spec)" in prime_line(s)))
        cases.append(("an all-failed run keeps last_run, so it retries the next day",
                      bool(s) and s["last_run"] == (t0 + timedelta(days=15)).isoformat()))

        # TLS: the real fetch path, with urlopen made to fail the way an
        # intercepting network makes it fail. It must surface, not be retried
        # around, and nothing may hand urlopen a context of its own.
        calls: list[dict] = []

        def refuse(req, **kw):
            calls.append(kw)
            raise urllib.error.URLError(ssl.SSLCertVerificationError(
                1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))

        with mock.patch.object(urllib.request, "urlopen", refuse):
            try:
                fetch(src.url)
                tls_ok = False
            except FetchFailed as exc:
                tls_ok = "not bypassed" in str(exc)
        cases.append(("TLS failure surfaces as not checked and is never bypassed",
                      tls_ok and len(calls) == 1 and "context" not in calls[0]))

    sample_html = ("<html><head><title>x</title><script>var a=1</script></head><body><nav>menu</nav>"
                   "<h2>Tools\u200b</h2><p>call &amp;\u00a0list</p><footer>foot</footer></body></html>")
    page = DocSource("h", "https://x.invalid", "html", "invented")
    cases.append(("HTML normalises to headings and text, chrome and invisible characters dropped",
                  normalise(page, sample_html) == "## Tools\ncall & list\n"))
    links = DocSource("l", "https://x.invalid", "links", "invented", pattern=r"/spec/(\d{4})/log")
    cases.append(("links kind lists the distinct matches sorted",
                  normalise(links, "/spec/2025/log /spec/2024/log /spec/2025/log") == "# Matches\n2024\n2025\n"))

    failures = 0
    for label, ok in cases:
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    print(f"{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0
