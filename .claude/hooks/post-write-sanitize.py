#!/usr/bin/env python3
"""PostToolUse hook: scan written/edited files for hidden Unicode characters.

Triggers after Write or Edit tool calls. If hidden characters are detected,
returns feedback to Claude so it can fix the contamination immediately.
"""
import json
import os
import sys
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
    # Exit 2 is the PostToolUse channel that shows stderr to Claude: a scan
    # that cannot run must say so, never pass for a clean file.
    print(f"[post-write-sanitize] HIDDEN CHARACTER SCAN DID NOT RUN: shared hook "
          f"helpers unavailable ({_exc})", file=sys.stderr)
    sys.exit(2)


# Binary/non-text extensions to skip
SKIP_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".pdf", ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv", ".webm",
    ".pyc", ".pyo", ".class", ".o", ".a", ".lib",
    ".bin", ".dat", ".db", ".sqlite",
    ".pptx", ".docx", ".xlsx", ".dotx", ".potx",
    ".pen",
}

# Files that legitimately embed invisible characters as data (the sanitizer
# itself). Scanning them produces a false-positive contamination warning on
# every edit. Match on suffix to be path-agnostic.
SKIP_BASENAMES = {
    "sanitize_text.py",  # scripts/utils/sanitize_text.py
}


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception as e:
        print(f"[post-write-sanitize] failed to parse input: {e}", file=sys.stderr)
        sys.exit(0)

    # A payload that is valid JSON but not an object still has `.get` called on
    # it. `[]`, `"x"` and `3` all parse, then raise an uncaught AttributeError.
    # Measured 2026-08-23 with `echo '[]' | python <hook>`: exit 1, traceback.
    # `.claude/hooks/checkpoint-inject.py` fixed this shape on 2026-08-20 and
    # these were missed. Degrade to the empty dict, which every path below
    # already handles, rather than dropping the hook's whole job.
    if not isinstance(input_data, dict):
        print(f"[post-write-sanitize] payload was {type(input_data).__name__}, "
              "not an object", file=sys.stderr)
        sys.exit(0)

    # `.get("tool_input", {})` returns the STORED value when the key is present,
    # so a `null`, a list or a string reached `.get` and raised an uncaught
    # AttributeError one level below the guard above. `_dispatch.py` and
    # `data-path-redirect.py` both already guard this nested shape.
    tool_input = input_data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        print(f"[post-write-sanitize] tool_input was {type(tool_input).__name__}, "
              "not an object", file=sys.stderr)
        sys.exit(0)
    # Write/Edit/MultiEdit carry file_path; NotebookEdit carries notebook_path.
    # We scan the on-disk result, so the same post-write scan covers all four.
    file_path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")

    # The TYPE of the path INSIDE tool_input, one level below the guard above.
    # `tool_input` was checked and the field in it was not, so `file_path: 3`
    # reached `os.path.isfile`, which accepts an int as a FILE DESCRIPTOR rather
    # than rejecting it, and `os.path.splitext` then raised TypeError. Measured
    # 2026-08-31 driving the real hook: a list, a dict and a float each exited 1
    # with a traceback on both fields, and the int did so whenever fd 3 happened
    # to be open on a regular file. Coerce to the empty string, which the next
    # line already handles, and SAY it, because a silent coercion is a scan
    # nobody knows was skipped.
    if not isinstance(file_path, str):
        print(f"[post-write-sanitize] path field was "
              f"{type(file_path).__name__}, not a string", file=sys.stderr)
        file_path = ""

    if not file_path:
        sys.exit(0)

    ext = os.path.splitext(file_path)[1].lower()
    if ext in SKIP_EXTENSIONS:
        sys.exit(0)

    if os.path.basename(file_path) in SKIP_BASENAMES:
        sys.exit(0)

    # WHICH directory a RELATIVE file_path is resolved from. The scanner import
    # below was anchored to `__file__` on 2026-08-25 and this existence gate, one
    # step above it, was left on the hook PROCESS cwd, so the two halves of the
    # same hook disagreed about the same payload field. Measured 2026-08-31 by
    # driving the real hook with a relative path and the payload cwd at the
    # engine root: from the root the contamination was reported, and with the
    # process parked in `scripts/` or `.claude/hooks/` the hook printed nothing
    # at all, on either stream, and exited 0 having scanned nothing.
    # `prompt-guard.py`, on this same PostToolUse matcher, already resolves
    # against the payload's own cwd; the two hooks answered differently about
    # one field.
    session_cwd = input_data.get("cwd")
    if not isinstance(session_cwd, str) or not session_cwd:
        session_cwd = os.getcwd()
    scan_path = (file_path if os.path.isabs(file_path)
                 else os.path.join(session_cwd, file_path))

    if not os.path.isfile(scan_path):
        # Say it, rather than exiting 0 the way a clean file does. A control
        # whose failure is indistinguishable from its success gets read as a
        # pass (`.claude/rules/scope-claims.md`, obligation 3), and this gate is
        # exactly where the silent non-scan lived. Same wording as the
        # import-failure branch below, because it is the same outcome: the file
        # is unchecked, and the only thing that changed is the reason.
        advise(
            f"HIDDEN CHARACTER SCAN DID NOT RUN on "
            f"{os.path.basename(file_path)}: no file was found at "
            f"{scan_path}. The file is UNVERIFIED, not clean. Scan it with "
            f"the workspace sanitizer before treating it as checked."
        )
        sys.exit(0)

    # In-process import (Phase 2.1). The engine root is derived from THIS FILE,
    # not from the harness cwd.
    #
    # "Workspace root is the harness-provided cwd" was asserted by the old
    # comment and established by nothing: the Bash tool's cwd drifts, and a
    # session started in any engine subdirectory made the scanner import fail,
    # print one stderr line nobody reads, and exit 0 having scanned NOTHING.
    # Reproduced 2026-08-25: the same file carrying U+200B reported "HIDDEN
    # CHARACTER CONTAMINATION" from the engine root and nothing at all from a
    # subdirectory. This hook is the mechanical half of the always-on
    # `.claude/rules/hidden-chars.md` policy, so it was switching itself off.
    # The docs-sync hook beside it already anchors this way; it is not named by
    # path here, because the plugin bundler's completeness gate reads a path
    # token as a bundled dependency.
    engine_root = Path(__file__).resolve().parents[2]
    # `session_cwd` above is the same field, resolved once. Reading `cwd` twice
    # is how the file gate and the import gate came to disagree about it.
    for candidate in (str(engine_root), session_cwd):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    try:
        from scripts.utils.sanitize_text import scan_file
    except Exception as e:
        # Say it IN CONTEXT, not only on a stream nothing reads. A guard that
        # cannot run must report that it did not run, or its silence is read as
        # a clean file (`.claude/rules/scope-claims.md`, obligation 3).
        print(f"[post-write-sanitize] could not import scan_file: {e}", file=sys.stderr)
        # The workspace sanitizer is deliberately not named by path here: the
        # plugin bundler's completeness gate reads a path token as a bundled
        # dependency, and this hook ships in bundles that do not carry it.
        advise(
            f"HIDDEN CHARACTER SCAN DID NOT RUN on "
            f"{os.path.basename(file_path)}: the scanner could not be "
            f"imported ({type(e).__name__}: {e}). The file is UNVERIFIED, "
            f"not clean. Scan it with the workspace sanitizer before "
            f"treating it as checked."
        )
        sys.exit(0)

    try:
        count, report = scan_file(scan_path)
        if count > 0:
            basename = os.path.basename(file_path)
            advise(
                f"HIDDEN CHARACTER CONTAMINATION in {basename}. "
                f"{report}. "
                f"The file has already been written with hidden characters. "
                f"Fix immediately: re-edit the file to remove the hidden characters."
            )
    except Exception as e:
        # The path actually scanned, not the payload's spelling of it: naming a
        # path nothing opened is what made the gate above unreadable.
        print(f"[post-write-sanitize] Error scanning {scan_path}: {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
