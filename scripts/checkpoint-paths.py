#!/usr/bin/env python3
"""Print this session's checkpoint paths, write a handoff, flip the session switches.

`/handover` (named `/checkpoint` until 2026-09-27) and `/clear-handover` are
model-driven and get no hook payload, so this script is their one source of
paths: every path is keyed by session id, and a guessed key defeats the keying.
The session id comes from CLAUDE_CODE_SESSION_ID, which Claude Code exports to
child processes (verified on 2.1.228).

Usage:
  python scripts/checkpoint-paths.py                # key=value lines
  python scripts/checkpoint-paths.py --json         # the same, as JSON
  python scripts/checkpoint-paths.py --kind clear   # paths for /clear-handover
  python scripts/checkpoint-paths.py --kind second-wind   # paths for /second-wind
  python scripts/checkpoint-paths.py --write [--kind clear|second-wind] <<'HANDOFF'
  ...the handoff text...
  HANDOFF                                           # archive + all four pointers
  python scripts/checkpoint-paths.py --seal <archive>     # re-stamp an archive
  python scripts/checkpoint-paths.py --unattended on|off|status
  python scripts/checkpoint-paths.py --done "plan X: 7 of 7"  # the plan is finished
  python scripts/checkpoint-paths.py --compact-history    # where compaction fired

Archive paths are DATA-root-relative, the form the @-reference and the inject
hook resolve. The state path is project-relative.

The four pointers are this session's pair and THIS CHECKOUT's pair
(`checkout_summary_pointer`, `checkout_prompt_pointer`), the one /prime and
/next read. The checkout is the tree this script runs from, so HELM and each
YARD have their own pair (HOS-158). The machine-wide `.latest/{summary,prompt}.md`
it replaced is retired: see `CHECKOUT_DIRNAME` in `scripts/utils/checkpoint_paths.py`.

Task dossier (HOS-256): the archive carries a `Task:` header line naming the
session's Lific issues, lists the prompts of those issues saved by earlier
sessions, and once it is on disk posts one `[dossier]` record of event
`handoff` to each issue (`scripts/utils/dossier.py`; Lific failing queues it).

Tests: tests/test_yard_containment.py
"""

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import checkpoint_autonomy as AU  # noqa: E402
from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils import dossier  # noqa: E402

# The archive sections this script writes itself; the model's text never needs them.
SCRIPT_SECTIONS = ("## Continuation prompt", CP.PROMPTS_HEADING, CP.TASK_PROMPTS_HEADING)
POINTER_CORE = ("Objective", "Current state", "Next steps")


def _transcript_path(project: Path, sid: str) -> Path | None:
    """This session's transcript: the one the status line recorded, else derived."""
    recorded = CP.read_json(CP.state_path(project, CP.safe_slug(sid))).get("transcript_path")
    if isinstance(recorded, str) and recorded:
        return Path(recorded)
    directory = CP.transcript_dir(project)
    return directory / f"{sid}.jsonl" if directory is not None else None


def compacted(project: Path, sid: str) -> tuple[str, int]:
    """("yes"|"no"|"unknown", boundaries) for THIS session, from its transcript.

    "unknown" when the transcript cannot be read, which is not "no".
    """
    digest = CP.transcript_digest(_transcript_path(project, sid))
    if digest is None:
        return "unknown", 0
    count = len(digest["boundaries"])
    return ("yes" if count else "no"), count


def collect(kind: str = "manual", master: str | None = None) -> dict:
    """Every path one save needs. `kind` becomes the archive's kind segment.

    The kind tells an operator-typed `/handover` (`manual`) from a
    `/clear-handover` (`clear`) and a `/second-wind` (`second-wind`); the
    compaction probe and the Stop hook read it from the filename.
    """
    project = CP.project_root()
    handoff = CP.handoff_dir(project, CP.engine_root())
    sid = CP.session_id()
    slug = CP.safe_slug(sid)
    # local_now: this stamp is the archive FILENAME, a calendar day the operator reads.
    stamp = CP.local_now().strftime("%Y-%m-%d-%H%M%S")

    root = CP.engine_root()
    if CP.is_engine_tree(root):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from scripts.utils.workspace import get_data_root

        base = get_data_root()
    else:
        base = project

    def ref(path: Path) -> str:
        """Relative when it can be, absolute when it cannot. Total."""
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            return path.as_posix()

    latest = CP.latest_dir(handoff, slug, root)
    checkout = CP.checkout_latest_dir(handoff, root)
    state = CP.state_path(project, slug)
    was, count = compacted(project, sid)
    if master is None:
        master = CP.master_instruction(sid if CP.session_id_is_known() else "",
                                       project, root, CP.read_json(state))
    return {
        "session_id": sid,
        "session_slug": slug,
        "stamp": stamp,
        "project_root": str(project),
        "data_root": str(base),
        "archive": ref(handoff / f"{stamp}_handoff_{kind}_{slug}.md"),
        "summary_pointer": ref(latest / "summary.md"),
        "prompt_pointer": ref(latest / "prompt.md"),
        "checkout_summary_pointer": ref(checkout / "summary.md"),
        "checkout_prompt_pointer": ref(checkout / "prompt.md"),
        "state": (
            state.relative_to(project).as_posix()
            if project in state.parents
            else state.as_posix()
        ),
        "master_instruction": master,
        "compacted": was if was != "yes" else f"yes ({count} time(s))",
    }


def request_reset(kind: str) -> None:
    """Record that /clear-handover or /second-wind started; the Stop hook accepts only a handoff written after it."""
    path = CP.state_path(CP.project_root(), CP.safe_slug(CP.session_id()))
    with CP.locked_state(path) as state:
        state["reset_requested_at"] = CP.utc_now().isoformat()
        state["reset_requested_kind"] = kind


def _sealed(text: str, paths: dict) -> tuple[str, str, str, list[str]]:
    """`text` with the script-owned header lines and the pins section. Idempotent.

    `Master instruction:` and `Compacted before this handoff:` go under the
    title and the session's pins become `## Pinned items`, all by script so the
    model can neither forget nor reword them (operator decision 2026-09-27).
    """
    root = CP.engine_root()
    pins = CP.read_pins(CP.pins_path(CP.handoff_dir(CP.project_root(), root),
                                     paths["session_slug"], root))
    master_line = f"{CP.MASTER_PREFIX} {paths['master_instruction']}"
    compacted_line = f"{CP.COMPACTED_PREFIX} {paths['compacted']}"
    state = CP.read_json(CP.state_path(CP.project_root(), paths["session_slug"]))
    unattended_line = CP.unattended_line(state)
    task_line = dossier.task_header(dossier.session_tasks(state))
    text = CP.set_header_lines(text, [master_line, compacted_line, unattended_line])
    # Not through `set_header_lines`, which drops a matching line ANYWHERE in the
    # body: a handoff may well say "Task: ..." in its own prose.
    lines = text.splitlines()
    at = lines.index(unattended_line) + 1
    if at < len(lines) and lines[at].startswith("Task:"):
        lines[at] = task_line
    else:
        lines.insert(at, task_line)
    text = "\n".join(lines) + "\n"
    return CP.set_pins_section(text, pins), master_line, compacted_line, pins


def _resolve(paths: dict, key: str) -> Path:
    path = Path(paths[key])
    return path if path.is_absolute() else Path(paths["data_root"]) / path


def seal(archive: str) -> int:
    """Re-stamp an archive written by hand: header lines, pins, master line first in the four pointers."""
    paths = collect()
    target = Path(archive)
    if not target.is_absolute():
        target = Path(paths["data_root"]) / target
    if not target.is_file():
        print(f"checkpoint-paths: refused. {target} does not exist; write the "
              f"archive first, then seal it.", file=sys.stderr)
        return 1
    text, master_line, compacted_line, pins = _sealed(
        target.read_text(encoding="utf-8"), paths)
    CP.write_text_atomic(target, text)
    missing = []
    for key in ("summary_pointer", "prompt_pointer",
                "checkout_summary_pointer", "checkout_prompt_pointer"):
        pointer = _resolve(paths, key)
        if not pointer.is_file():
            missing.append(paths[key])
            continue
        CP.write_text_atomic(pointer, CP.set_first_line(
            pointer.read_text(encoding="utf-8"), master_line))
    print(f"sealed {target}")
    print(master_line)
    print(compacted_line)
    print(f"Pinned items: {len(pins)}")
    if missing:
        print("checkpoint-paths: these pointers do not exist yet, so they carry "
              "no master line: " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


def _header_value(text: str, prefix: str) -> str | None:
    for line in text.splitlines()[:20]:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def write(kind: str, text: str) -> int:
    """Write a handoff the model hands over on stdin: the archive and all four pointers.

    The model supplies the template's sections; this adds the continuation
    prompt, the prompt-archive list, the header lines and the pins, derives both
    pointer texts from the archive, and writes this session's pair and this
    checkout's pair. One command, so a pointer can never disagree with its archive.
    """
    if not text.strip():
        print("checkpoint-paths: refused. Nothing arrived on stdin; pass the "
              "handoff text in, for example through a quoted heredoc.", file=sys.stderr)
        return 1
    project = CP.project_root()
    root = CP.engine_root()
    sid = CP.session_id()
    state = CP.read_json(CP.state_path(project, CP.safe_slug(sid)))
    prompts, refusal = CP.archived_prompts(sid if CP.session_id_is_known() else "",
                                           project, CP.prompt_archive_tool(root))
    paths = collect(kind, CP.master_value(prompts, refusal, CP.carried_master(state)))
    archive_ref = paths["archive"]

    body = text
    for heading in SCRIPT_SECTIONS:
        body = CP.drop_section(body, heading)
    tasks = dossier.session_tasks(state)
    owned = (f"## Continuation prompt\n\n{CP.continuation_prompt(archive_ref)}\n"
             f"{CP.prompts_section(prompts, refusal)}")
    others = CP.task_prompts_section(tasks, prompts, project, CP.prompt_archive_tool(root))
    if others:
        owned += f"\n{others}"
    lines = body.rstrip("\n").splitlines()
    note = next((i for i, ln in enumerate(lines) if ln.strip() == "## User note"), None)
    if note is None:
        body = "\n".join(lines) + "\n\n" + owned
    else:
        body = "\n".join(lines[:note]).rstrip("\n") + "\n\n" + owned + "\n" + "\n".join(lines[note:]) + "\n"
    body, master_line, compacted_line, pins = _sealed(body, paths)

    archive = _resolve(paths, "archive")
    CP.write_text_atomic(archive, body)
    record = dossier.post(tasks, "handoff", kind, sid, CP.safe_resume_lines(body), archive_ref)

    summary = (f"# Latest handoff summary\n\nSource: {archive_ref}\n"
               f"Generated: {_header_value(body, 'Generated:') or CP.utc_now().isoformat()}\n"
               f"Trigger: {_header_value(body, 'Trigger:') or kind}\n\n"
               f"{CP.bound_summary(CP.sections(body, POINTER_CORE), archive_ref)}\n")
    pointers = {
        "summary": CP.set_first_line(summary, master_line),
        "prompt": CP.set_first_line(CP.continuation_prompt(archive_ref), master_line),
    }
    CP.runtime_state_root(root, create=True)
    for key in ("summary_pointer", "prompt_pointer"):
        CP.write_text_atomic(_resolve(paths, key), pointers[key.split("_")[0]])
    checkout_summary = _resolve(paths, "checkout_summary_pointer")
    # This checkout's pair under one lock, as the post-compact hook writes it.
    with CP.file_lock(checkout_summary.parent / ".pointers.lock"):
        CP.write_text_atomic(checkout_summary, pointers["summary"])
        CP.write_text_atomic(_resolve(paths, "checkout_prompt_pointer"), pointers["prompt"])

    print(f"wrote {archive}")
    print("pointers: this session's pair and this checkout's pair")
    print(master_line)
    print(compacted_line)
    print(f"Pinned items: {len(pins)}")
    print(dossier.task_header(tasks)
          + (f" (dossier: posted {len(record['posted'])}, queued {len(record['queued'])})"
             if tasks else ""))
    if kind in ("clear", "second-wind"):
        gaps = CP.safe_resume_missing(body)
        if gaps:
            print("checkpoint-paths: the Stop hook will refuse to reset: missing "
                  + "; ".join(gaps) + ". Add the block and run --write again.",
                  file=sys.stderr)
        # The lines the SessionStart hook shows on screen right after /clear.
        resume = CP.resume_block(body)
        if resume:
            print(f"Resume block: {len(resume.splitlines())} line(s)")
        else:
            print(f"checkpoint-paths: no {CP.RESUME_HEADING} section, so the screen "
                  f"after /clear shows the first line of Objective, Current state, "
                  f"Next steps and Open issues instead", file=sys.stderr)
    # The hidden-character advisory the Write tool's PostToolUse hook gave when
    # the model wrote these files itself.
    try:
        from scripts.utils.sanitize_text import scan
        report = io.StringIO()
        if scan(body, str(archive), out=report):
            print(report.getvalue().strip(), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - an advisory, never a refusal
        print(f"checkpoint-paths: hidden-character scan unavailable: {exc}", file=sys.stderr)
    return 0


def compact_history() -> int:
    """Print where compaction actually fired, per session, newest last.

    Every state file is read, since the question is usually asked in a fresh
    session about the previous one. Ordered by each session's newest UTC `at`
    stamp, which sorts chronologically as a string; filename order is arbitrary.
    """
    state_dir = CP.state_path(CP.project_root(), "x").parent
    files = state_dir.glob("checkpoint-*.json") if state_dir.is_dir() else []

    sessions = []
    for path in files:
        history = CP.read_json(path).get("compact_history")
        if not isinstance(history, list) or not history:
            continue
        newest = max(
            (str(entry.get("at") or "") for entry in history
             if isinstance(entry, dict)),
            default="",
        )
        sessions.append((newest, path.stem.removeprefix("checkpoint-"), history))
    sessions.sort(key=lambda item: (item[0], item[1]))

    point = CP.compact_point()
    print(f"configured: {point[0]}={point[1]}" if point
          else "configured: nothing in the environment sets a compaction point")

    rows = 0
    for _newest, name, history in sessions:
        print(f"\n{name}")
        for entry in history:
            if not isinstance(entry, dict):
                continue
            pct = entry.get("used_pct_at_or_above")
            print(
                f"  {entry.get('at')}  trigger={entry.get('trigger')}  "
                f"fired at >= {pct}% used  (configured {entry.get('configured')})"
            )
            rows += 1

    if rows == 0:
        # "Nothing recorded yet" is a different claim from "it never fires".
        print("\nNo compaction has been recorded yet on this tree.")
        print("The record is written at the next compact, by checkpoint-save.py.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Print this session's checkpoint paths.")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of key=value")
    ap.add_argument(
        "--unattended",
        choices=("on", "off", "status"),
        help="continue at a pause after a silent grace period, THIS session only "
             "(overrides CLAUDE_HANDOFF_UNATTENDED and the yard default)",
    )
    ap.add_argument(
        "--done",
        metavar="NOTE",
        help="declare the plan finished: the unattended stretch stops at the next "
             "pause, and the switch is left alone for the operator to read",
    )
    ap.add_argument(
        "--kind",
        choices=("manual", "clear", "second-wind"),
        default=None,
        help="archive kind segment: manual for an operator-typed /handover, "
             "clear for /clear-handover, "
             "second-wind for /second-wind (the paths dump for both also records when it started, for the "
             "Stop hook's check)",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="write the handoff text read from stdin: the archive, its "
             "script-owned sections and all four pointer files",
    )
    ap.add_argument(
        "--seal",
        metavar="ARCHIVE",
        help="stamp the master instruction, the compaction warning and the pins "
             "into an existing archive, and the master line into the pointers",
    )
    ap.add_argument(
        "--compact-history",
        action="store_true",
        help="print where compaction fired on this tree, per session",
    )
    args = ap.parse_args(argv)

    if args.write:
        return write(args.kind or "manual", sys.stdin.read())

    # Run EVERY action asked for, in declaration order; the first refusal is the
    # exit code and the rest still run.
    actions = [
        (args.compact_history, lambda: compact_history()),
        (args.unattended, lambda: AU.unattended_switch(args.unattended)),
        (args.done is not None, lambda: AU.done_marker(args.done)),
        (args.seal is not None, lambda: seal(args.seal)),
    ]
    requested = [run for wanted, run in actions if wanted]
    if requested:
        for flag, given in (("--json", args.json), ("--kind", args.kind is not None)):
            if given:
                print(f"{flag} describes the paths dump and does not apply to an "
                      "action flag; ignoring it.", file=sys.stderr)
        codes = [run() for run in requested]
        return next((c for c in codes if c != 0), 0)

    if args.kind in ("clear", "second-wind"):
        request_reset(args.kind)
    paths = collect(args.kind or "manual")
    if args.json:
        print(json.dumps(paths, indent=2))
    else:
        for key, value in paths.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
