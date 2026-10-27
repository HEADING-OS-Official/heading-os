#!/usr/bin/env python3
"""Report "what just happened" so /next can recommend the logical next step.

Aggregates four signal sources, newest-first and handoff-weighted, into a compact
recent-actions list:

  1. HANDOFF:  THIS checkout's pointer, `summary.md` under
                 `checkpoint_paths.latest_root()`, which checkpoint-save.py writes
                 (the explicit "where were we" pointer; weighted highest). Never
                 another checkout's (HOS-158).
  2. OUTPUTS:  the newest files under the registry group `output-dirs` (noise
                 excluded), each mapped back to its producing skill via
                 reference/skill-graph.csv (produces_in).
  3. GIT:      the last N `git log --oneline` subjects (the feat/fix/chore prefixes name
                 the recent work area).
  4. THREADS:  active business threads (`threads_lib.business_thread_dirs`) by mtime.

Read-only. No daemon, no browser, no writes. Console-first: run it directly from the
terminal or chat.

The handoff pointer is runtime state, not a record: it is read only where its
writer writes it (`checkpoint_paths`), in this checkout's own directory, never a
migrated copy and never the retired machine-wide `.latest/summary.md`.

Usage:
  python scripts/next-signal.py            # lean text
  python scripts/next-signal.py --json     # structured signal for /next
  python scripts/next-signal.py --limit 12 # widen the outputs scan

Consumed by: /next.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import skill_graph  # noqa: E402
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import DataPathError, data_path  # noqa: E402
from scripts.utils.data_homes import homes as data_homes  # noqa: E402
from scripts.utils.paths import get_data_root  # noqa: E402
from scripts.utils.workspace import (  # noqa: E402
    get_default_tz,
    get_workspace_root,
)

# ============================================================
# Configuration
# ============================================================

# Directory names that are noise for a "what did I just do" signal, matched as
# PATH COMPONENTS (never prefixes) relative to the data root.
EXCLUDE_DIRS = {"_sync", "_tmp", "browser", "clipboard", "handoff-archive"}
# Specific noise files to skip wherever they appear.
EXCLUDE_FILES = {"_latest-fetch.json"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=get_default_tz()).strftime("%Y-%m-%d %H:%M")


# ============================================================
# Signal sources
# ============================================================

def handoff_latest_summary() -> Path | None:
    """THIS checkout's pointer, `.latest/.checkout/<checkout>/summary.md` under
    the writer's runtime state root. The checkout is the tree this file sits in, so a YARD never reads HELM's
    handoff (HOS-158); there is deliberately no fallback to the retired
    machine-wide `.latest/summary.md`.

    Asked of the WRITER's own resolver, `checkpoint_paths.runtime_state_root`
    with `create=False`, so this reader cannot spell the store differently from
    the hook that fills it; nothing is created on the way.

    None when the writer has no store to use (no engine tree, no overlay, or a
    resolver error it has already reported on stderr). The writer then keeps
    the pointers beside the handoff archive, which this reader does not follow,
    so `read_handoff` answers "no handoff" exactly as for a missing file."""
    from scripts.utils import checkpoint_paths as CP

    state = CP.runtime_state_root(create=False)
    if state is None:
        return None
    checkout = CP.checkout_key(Path(__file__).resolve().parent.parent)
    return state / CP.LATEST_DIRNAME / CP.CHECKOUT_DIRNAME / checkout / "summary.md"


def read_handoff() -> dict | None:
    """Parse the .latest handoff summary: source, generated, objective, next steps.

    The pointer is runtime state -> resolved under the data root's runtime store."""
    f = handoff_latest_summary()
    if f is None or not f.is_file():
        return None
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # An unreadable pointer reads as "no handoff", never a traceback.
        return None
    out = {"source": "", "generated": "", "objective": "", "next_steps": []}
    section = None
    for line in text.splitlines():
        s = line.strip()
        # Source/Generated live only in the header region (above the first `## `);
        # gating on section keeps a body line that happens to start "Source:" from
        # hijacking the field or stealing the objective's first line.
        if section is None and s.startswith("Source:"):
            out["source"] = s.split(":", 1)[1].strip()
        elif section is None and s.startswith("Generated:"):
            out["generated"] = s.split(":", 1)[1].strip()
        elif s.startswith("## "):
            section = s[3:].strip().lower()
            # `## Summary` is the last field-bearing heading the checkpoint hook
            # writes, and everything below it is the model's own prose, which
            # routinely carries its own `## Next steps`. Without this latch those
            # bullets APPEND to the list above, so /next renders the previous
            # session's steps as if they were this handoff's. Measured: a summary
            # containing that heading produced four steps, two of them the body's.
            if section == "summary":
                break
        elif s and section == "objective" and not out["objective"]:
            out["objective"] = s
        elif s and section == "next steps" and (s[0].isdigit() or s.startswith(("-", "*"))):
            # Strip a real ordinal marker (1. / 2) / - / *) only — not every leading
            # digit/dash, which would mangle steps like "3D ..." or "2026-report due".
            out["next_steps"].append(re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", s).strip())
    return out


def recent_outputs(limit: int, graph_rows: list[dict]) -> list[dict]:
    """Newest files under the registry group `output-dirs`, noise excluded,
    mapped to producing skill(s).

    Exclusions are PATH COMPONENTS relative to the data root, plus the
    `handoff-archive` and `linkedin-archive` trees by registry name. An empty
    tree answers an empty list; it is not a broken one."""
    data_root = get_data_root()
    seen: dict[Path, None] = {}
    # The handoff archive and the LinkedIn archive (published posts, nested
    # inside `linkedin-drafts`, which this walk descends) are not recent work.
    noise = []
    try:
        noise.append(data_path("handoff-archive", data_root=data_root))
    except DataPathError as exc:
        print(f"next-signal: 'handoff-archive' unresolved ({exc}); its files are "
              f"not excluded by name", file=sys.stderr)
    try:
        noise.append(data_path("linkedin-archive", data_root=data_root))
    except DataPathError as exc:
        print(f"next-signal: 'linkedin-archive' unresolved ({exc}); its files are "
              f"not excluded by name", file=sys.stderr)
    # An umbrella and its children are both named; walk each tree once.
    for home in data_homes("output-dirs", data_root=data_root, outermost=True):
        if not home.is_dir():
            continue
        for p in home.rglob("*"):
            if p.is_file() and not any(p.is_relative_to(n) for n in noise):
                seen[p] = None
    found: list[tuple[float, str]] = []
    for p in seen:
        # Exclusions are tested on the path RELATIVE to the data root, never on
        # the absolute one: a scratch tree whose own parent directory is called
        # `browser` must not silently empty the signal.
        rel = _relative(p, data_root)
        if any(part in EXCLUDE_DIRS for part in rel.split("/")):
            continue
        if p.name in EXCLUDE_FILES or p.name.startswith("."):
            continue
        try:
            found.append((p.stat().st_mtime, rel))
        except OSError:
            continue
    found.sort(key=lambda t: -t[0])
    out = []
    for mtime, relpath in found[:limit]:
        out.append({
            "path": relpath,
            "mtime": _iso(mtime),
            "skills": producing_skills(graph_rows, relpath),
        })
    return out


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def producing_skills(graph_rows: list[dict], relpath: str) -> list[str]:
    """Join a record's path onto the catalog's `produces_in` column.

    Cells written `@<name>` are registry names, which `skill_graph.by_output_dir`
    resolves against the data root (the longest match answers). A legacy cell
    is also tried against the path with its first two segments dropped, for a
    record still filed under a `<family>/<leaf>/` head.
    """
    skills = skill_graph.by_output_dir(graph_rows, relpath)
    if skills:
        return skills
    parts = relpath.split("/")
    if len(parts) > 2:
        return skill_graph.by_output_dir(graph_rows, "/".join(parts[2:]))
    return skills


def recent_commits(root: Path, n: int) -> list[str]:
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "-n", str(n)],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def active_threads(limit: int) -> list[dict]:
    """Active (non-archive) business threads by mtime. Business only -- personal threads
    carry a strict CEO-only regime and are not skill-lifecycle signals.

    The directories are `threads_lib.business_thread_dirs`, by registry name
    (the group `business-threads`, which walks each account's `account-threads`
    as well as `threads-business`), one level each (`*.md`), so nothing
    archived below one is read and no ancestor directory name can admit a
    private thread."""
    from scripts.utils.threads_lib import business_thread_dirs
    records = [p for d in business_thread_dirs()
               for p in sorted(d.glob("*.md"))]
    # Stat ONCE per file, tolerating a thread archived between glob and stat.
    stamped = []
    for p in records:
        try:
            stamped.append((p.stat().st_mtime, p))
        except OSError:
            continue
    stamped.sort(key=lambda pair: -pair[0])
    items = []
    for mtime, p in stamped:
        items.append({"slug": p.stem, "mtime": _iso(mtime)})
        if len(items) >= limit:
            break
    return items


# ============================================================
# Aggregation + render
# ============================================================

def load_graph_rows() -> tuple[list[dict], str]:
    """The skill catalog, or an empty catalog and a sentence saying why.

    Returns ``(rows, degraded_reason)``; the reason is `""` when the catalog
    loaded. It is ONE of the four signal sources' enrichment, not a source in
    its own right: without it `recent_outputs` reports the same files with no
    skill attribution, and the handoff, the commits and the threads are
    untouched. Losing all four to it is out of proportion, and losing them
    silently is worse.

    Absent or undecodable (MEASURED 2026-09-01), it must not discard the
    other three sources.
    """
    path = skill_graph.default_file()
    try:
        return skill_graph.load(path), ""
    except (OSError, UnicodeDecodeError) as exc:
        # NAMED, never dropped in silence: the reason travels in the signal and
        # is rendered in both output modes, so a reader sees a signal that is
        # missing its skill attribution rather than one that looks complete.
        return [], f"skill catalog unreadable ({path}): {exc}"


def gather(root: Path, limit: int) -> dict:
    """`root` is the ENGINE root, used only for the git log (recent_commits). The
    data sources (handoff, outputs, threads) resolve under the DATA root via their
    own get_*_dir() helpers and ignore `root`."""
    graph_rows, degraded = load_graph_rows()
    return {
        "handoff": read_handoff(),
        "recent_outputs": recent_outputs(limit, graph_rows),
        "recent_commits": recent_commits(root, 8),
        "active_threads": active_threads(5),
        # Always present, `[]` when nothing degraded, so a JSON consumer reads
        # the key rather than inferring its absence.
        "degraded": [degraded] if degraded else [],
    }


def render_text(sig: dict) -> str:
    lines = []
    # First, so a reader cannot take the rest of the page for a complete one.
    for why in sig.get("degraded") or []:
        lines.append(f"{YELLOW}degraded:{RESET} {why}")
    if sig.get("degraded"):
        lines.append("")
    h = sig.get("handoff")
    if h:
        lines.append(f"{BOLD}Handoff (strongest signal){RESET}")
        if h.get("objective"):
            lines.append(f"  {GREEN}objective:{RESET} {h['objective']}")
        for step in h.get("next_steps", [])[:3]:
            lines.append(f"  {CYAN}next:{RESET} {step}")
        lines.append("")
    if sig.get("recent_outputs"):
        lines.append(f"{BOLD}Recent outputs{RESET}")
        for o in sig["recent_outputs"]:
            who = f" {GRAY}[{'/'.join(o['skills'])}]{RESET}" if o["skills"] else ""
            lines.append(f"  {o['mtime']}  {o['path']}{who}")
        lines.append("")
    if sig.get("recent_commits"):
        lines.append(f"{BOLD}Recent commits{RESET}")
        for c in sig["recent_commits"][:5]:
            lines.append(f"  {GRAY}{c}{RESET}")
        lines.append("")
    if sig.get("active_threads"):
        lines.append(f"{BOLD}Open business threads{RESET}")
        for t in sig["active_threads"]:
            lines.append(f"  {YELLOW}{t['mtime']}{RESET}  {t['slug']}")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--json", action="store_true", help="emit the structured signal as JSON")
    p.add_argument("--limit", type=int, default=8, help="how many recent outputs to scan (default 8)")
    p.add_argument("--root", type=Path, default=None, help="workspace root (default: auto-detect)")
    args = p.parse_args(argv)

    root = args.root or get_workspace_root()
    try:
        sig = gather(root, max(1, args.limit))
    except OSError as e:
        # Any OSError: on CPython 3.11 `Path.rglob` suppresses only
        # PermissionError, so a vanished directory or a symlink loop still
        # escapes `gather`. The message is the exception's own.
        print(f"next-signal: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(sig, indent=2))
    else:
        print(render_text(sig))
    return 0


if __name__ == "__main__":
    sys.exit(main())
