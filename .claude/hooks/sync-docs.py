#!/usr/bin/env python3
"""PostToolUse hook: auto-sync templates/ -> docs/ when documentation files change.

templates/ is the source of truth for shared documentation.
docs/ is the distribution directory.
This hook auto-copies changed files to keep them in sync.

Syncs the 2 shared documentation files in SYNC_FILES (MD + HTML versions).

When a templates/*.md file is edited, the matching HTML is regenerated via
scripts/regenerate-docs-html.py (non-blocking: failures produce warnings,
never abort the write).
"""
import sys
import json
import shutil
import subprocess
from pathlib import Path

# Walk to the tree that owns scripts/: .claude/hooks/ in the monorepo, hooks/
# in a plugin bundle.
for _root in Path(__file__).resolve().parents:
    if (_root / "scripts" / "utils" / "hook_common.py").is_file():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        break
try:
    from scripts.utils.hook_common import post_tool_advice as advise
except ImportError as _exc:
    # Exit 2 is the PostToolUse channel that shows stderr to Claude.
    print(f"[sync-docs] shared hook helpers unavailable, sync skipped: {_exc}",
          file=sys.stderr)
    sys.exit(2)


# Files that should be synced from templates/ to docs/. Must match SYNCED_FILES
# in `.claude/hooks/_dispatch.py`, which walls the destinations off from a
# direct edit.
SYNC_FILES = {
    "GETTING-STARTED.md",
    "GETTING-STARTED.html",
}

# The engine clone, anchored to this file rather than to the session's cwd, so a
# hook that fires while the cwd is the data overlay still finds the right tree.
ENGINE_ROOT = Path(__file__).resolve().parents[2]


def is_real_template(file_path: Path) -> bool:
    """True only for a file directly inside a real `templates/` source directory.

    The path is resolved first, so a relative `templates/GETTING-STARTED.md`
    matches (an unanchored substring test missed it, 2026-08-23 audit). Two
    structural rules keep a scratch copy from overwriting the distributed docs:

      1. the parent directory must BE named `templates`;
      2. a `templates/` nested inside a known workspace root counts only when it
         is that root's own templates home (`<root>/templates` or the registry
         name `templates`), so `<root>/<anything>/templates/` is refused.

    Rule 2 is shape-based, not an identity check, so a synthetic root in a test
    tree still exercises the sync.
    """
    try:
        resolved = file_path.resolve()
    except OSError:
        return False
    if resolved.parent.name != "templates":
        return False
    root = resolved.parent.parent

    # The engine root and the data overlay; its absence is "engine-only".
    known = known_roots()

    # A root's own templates home: `<root>/templates`, or the address the
    # registry gives it, which sits deeper than one level below the data root
    # (a nested shape rule 2 would otherwise refuse; MEASURED 2026-09-18, the
    # sync was dead for every shared document until this set existed).
    homes = set()
    for k in known:
        homes.add(k / "templates")
        try:
            from scripts.utils.data_path import data_path  # noqa: PLC0415
            homes.add(data_path("templates", data_root=k))
        except Exception as exc:  # noqa: BLE001 — a lookup never breaks a write
            print(f"[sync-docs] templates-home lookup skipped for {k} ({exc})",
                  file=sys.stderr)

    if any(k in root.parents for k in known) and resolved.parent not in homes:
        print(f"[sync-docs] refusing {resolved}: a templates/ directory nested "
              "inside a workspace root is not a template source", file=sys.stderr)
        return False

    return True


def known_roots(engine_root: Path | None = None) -> set:
    """The engine root and, when it resolves, the data root. Resolved paths.

    One helper because `is_real_template` and `sync_targets` both need the same
    two roots, and the second used to derive its root from the template's own
    `parent.parent` instead. That was the same answer until the move put the
    templates two levels deeper, and then it was silently a different one.
    """
    roots = {Path(ENGINE_ROOT if engine_root is None else engine_root).resolve()}
    # Restored in `finally`, and inserted only when absent. The bare
    # `sys.path.insert(0, ...)` this replaces ran on every call, and once
    # `sync_targets` started calling this helper too, one hook invocation
    # prepended the same directory several times. pytest's own teardown check
    # caught it: "left sys.path changed (added=[..., ..., ...])". A module
    # search path that grows per call is a slow leak in a process the harness
    # keeps alive across tool calls.
    added = str(ENGINE_ROOT) not in sys.path
    if added:
        sys.path.insert(0, str(ENGINE_ROOT))
    try:
        from scripts.utils.workspace import get_data_root  # noqa: PLC0415
        roots.add(Path(get_data_root()).resolve())
    except Exception as exc:  # noqa: BLE001 — never let path resolution break a write
        print(f"[sync-docs] data-root lookup skipped ({exc})", file=sys.stderr)
    finally:
        if added and str(ENGINE_ROOT) in sys.path:
            sys.path.remove(str(ENGINE_ROOT))
    return roots


def workspace_root_of(path: Path, engine_root: Path | None = None) -> Path:
    """The workspace root `path` belongs to, or its own `parent.parent`.

    A template can sit deeper than `<root>/templates/`, so `parent.parent` is
    not always a workspace root. The fallback keeps a synthetic tree in a test,
    which belongs to neither known root, resolving against itself.
    """
    resolved = Path(path).resolve()
    for root in sorted(known_roots(engine_root), key=lambda p: len(p.parts),
                       reverse=True):
        if root == resolved or root in resolved.parents:
            return root
    return resolved.parent.parent


def sync_targets(file_path: Path, engine_root: Path | None = None) -> list:
    """Every docs/ path a template must be copied to.

    The copy's address is asked of the registry (`spec-docs`) for the template's
    own workspace root; the sibling `<root>/docs/<name>` is computed FIRST and
    kept whenever the registry cannot answer (a public clone, a test tree).
    Building `parent.parent / "docs"` instead once pointed at a directory that
    does not exist and would have materialised a third home for the documents
    (MEASURED 2026-09-18). Kept a pure function so a test can assert the
    destination without running the hook.
    """
    engine_root = ENGINE_ROOT if engine_root is None else engine_root
    resolved = file_path.resolve()
    base = workspace_root_of(resolved, engine_root)
    sibling = base / "docs" / resolved.name
    try:
        from scripts.utils.data_path import data_path  # noqa: PLC0415
        # `data_root=base`, never the resolver: this function is pure and must
        # answer for the tree it was handed, so a synthetic test root resolves
        # against itself rather than against the operator's overlay.
        sibling = data_path("spec-docs", data_root=base) / resolved.name
    except Exception as exc:  # noqa: BLE001 — a lookup never breaks a write
        print(f"[sync-docs] docs-home lookup skipped ({exc}); using the sibling "
              f"docs/ directory", file=sys.stderr)
    return [sibling]


# Load-bearing substrings that MUST survive in a synced file. The sync blindly
# copies templates/ -> docs/, so an edit that silently drops a section would
# faithfully propagate the deletion into the distributed docs (this has
# recurred — e.g. the uv-dependency docs vanishing from GETTING-STARTED). If a
# template is missing any anchor for its name, the hook REFUSES to propagate and
# shouts, leaving the last-good docs/ copy intact for a human to reconcile.
# Keyed by filename; add anchors as load-bearing sections are identified.
REQUIRED_ANCHORS = {
    "GETTING-STARTED.md": ["uv sync", "DEPENDENCY-POLICY"],
}

def _missing_anchors(file_path: Path) -> list:
    """Return required anchors absent from the file's text (empty list = OK)."""
    anchors = REQUIRED_ANCHORS.get(file_path.name)
    if not anchors:
        return []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []  # unreadable source — let the normal copy path surface the error
    return [a for a in anchors if a not in text]


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception as e:
        print(f"[sync-docs] failed to parse input: {e}", file=sys.stderr)
        sys.exit(0)

    # A payload that is valid JSON but not an object still reaches `.get`.
    # `[]`, `"x"`, `3` and `null` all parse, then raise an uncaught
    # AttributeError. Swept 2026-08-23 across every stdin hook: six crashed on
    # all four shapes. Same defect checkpoint-inject.py fixed on 2026-08-20;
    # the sweep is how the rest were found.
    if not isinstance(input_data, dict):
        sys.exit(0)

    # `.get("tool_input", {})` returns the STORED value when the key is present,
    # so `null`, a list or a string reached `.get` and raised an uncaught
    # AttributeError one line below the guard that had just handled the same
    # shape at the top level. `_dispatch.py` and `data-path-redirect.py` both
    # already guard the nested value.
    tool_input = input_data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        print(f"[sync-docs] tool_input was {type(tool_input).__name__}, "
              "not an object", file=sys.stderr)
        sys.exit(0)
    file_path_str = tool_input.get("file_path", "")

    # The TYPE of the path INSIDE tool_input, one line below the guard that
    # checks the container. `file_path: 3` reached `Path()` and raised an
    # uncaught TypeError. Measured 2026-08-31 driving the real hook: an int, a
    # bool, a list, a dict and a float each exited 1 with a traceback. Coerce to
    # the empty string, which the next line already handles, and say so on
    # stderr: a silent coercion is a sync nobody knows was skipped.
    if not isinstance(file_path_str, str):
        print(f"[sync-docs] file_path was {type(file_path_str).__name__}, "
              "not a string", file=sys.stderr)
        file_path_str = ""

    if not file_path_str:
        sys.exit(0)

    file_path = Path(file_path_str)

    if file_path.name not in SYNC_FILES:
        sys.exit(0)

    if not is_real_template(file_path):
        sys.exit(0)

    # The renderer resolves from ENGINE_ROOT and the targets from the
    # template's own tree, never from the payload cwd: resolving from cwd once
    # wrote the data-overlay guide's copy into the engine tree.
    targets = sync_targets(file_path)

    # Anchor guard: refuse to propagate a template that lost a load-bearing
    # section, rather than faithfully copying the deletion into docs/.
    # The `mkdir` below comes AFTER this guard: a refused sync must not leave a
    # target directory behind (fixed 2026-09-18).
    missing = _missing_anchors(file_path)
    if missing:
        warn = (
            f"BLOCKED sync of {file_path.name}: required content missing "
            f"({', '.join(missing)}). The docs/ copy was left unchanged. Restore "
            f"the dropped section in templates/{file_path.name} and re-save."
        )
        advise(f"Warning: {warn}")
        sys.exit(0)

    for t in targets:
        t.parent.mkdir(parents=True, exist_ok=True)

    # Copy the file
    sync_msg = ""
    try:
        for t in targets:
            shutil.copy2(file_path, t)
        sync_msg = f"Auto-synced templates/{file_path.name} -> {len(targets)} docs/ copy(ies)"
    except Exception as e:
        advise(f"Warning: Failed to sync {file_path.name} to docs/: {e}")
        sys.exit(0)

    # If MD was edited, regenerate the matching HTML in both templates/ and docs/.
    # Non-blocking: regen failure produces a warning but never aborts.
    regen_msg = ""
    if file_path.suffix.lower() == ".md":
        # ENGINE_ROOT, not the payload cwd. The comment above already said the
        # renderer "lives in the engine clone" while the code looked for it under
        # the session's cwd, so a session started in any engine subdirectory - or
        # in the data overlay, the case ENGINE_ROOT was introduced for - found no
        # script, skipped regeneration, and still returned "Auto-synced ... ->
        # N docs/ copy(ies)". The `.html` twin is itself in SYNC_FILES and is
        # distributed, so it drifted silently, which is the exact staleness the
        # note at the top of this file records happening once already.
        regen_script = ENGINE_ROOT / "scripts" / "regenerate-docs-html.py"
        if regen_script.exists():
            try:
                # The result used to be discarded, so a renderer exiting 1
                # (a missing pygments, a template error) left stale HTML while
                # this hook appended "+ regenerated HTML" — a success claim on a
                # distribution pipeline that had not run. Found by the
                # 2026-08-23 audit. Non-zero stays non-blocking, per the module
                # docstring's promise of a warning, but it must now be SAID.
                failures = []
                for md_target in [file_path, *targets]:
                    proc = subprocess.run(
                        [sys.executable, str(regen_script), "--quiet", str(md_target)],
                        cwd=ENGINE_ROOT,
                        timeout=30,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if proc.returncode != 0:
                        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                        failures.append(f"{md_target.name} (exit {proc.returncode}"
                                        + (f": {detail[-1]}" if detail else "") + ")")
                if failures:
                    print(f"[sync-docs] HTML regen FAILED: {'; '.join(failures)}",
                          file=sys.stderr)
                    regen_msg = (f" (HTML regen FAILED for {len(failures)} target(s): "
                                 f"{'; '.join(failures)}. The HTML is STALE.)")
                else:
                    regen_msg = f" + regenerated HTML for {file_path.name}"
            except Exception as e:
                print(f"[sync-docs] HTML regen warning for {file_path.name}: {e}", file=sys.stderr)
                regen_msg = f" (HTML regen warning: {e})"
        else:
            # The missing-script branch had no `else` at all: nothing on stderr,
            # nothing in the message, and a success line claiming a complete
            # sync. A renderer that is not there is the same "the HTML is STALE"
            # outcome as one that exits non-zero, so it is reported the same way.
            print(f"[sync-docs] HTML NOT regenerated: no renderer at "
                  f"{regen_script}", file=sys.stderr)
            regen_msg = (f" (HTML NOT regenerated: no renderer at "
                         f"{regen_script}. The HTML is STALE.)")

    advise(sync_msg + regen_msg)
    sys.exit(0)


if __name__ == "__main__":
    main()
