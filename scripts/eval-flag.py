#!/usr/bin/env python3
"""eval-flag.py - one-keystroke "this output was wrong" capture (R13).

Turns a bad output into a versioned DRAFT regression case, keyed by the R12
trace ID, in one command. Two paths:

  eval-flag.py <id-or-prefix>            resolve a live Action Queue card from the
                                          queue store and stage a draft from it.
  eval-flag.py --skill NAME --note "..."  stage a draft from a note (and optional
            [--input-file F] [--type ...]  input file), touching no queue at all.
  eval-flag.py --list                     list staged drafts across all skills.

Drafts are written in the data repository, under the registry name
``skill-eval-drafts`` (the ``_staged/`` directory beneath the skill's
``skill-eval-outcomes``), never into the engine checkout: a draft from a live
card carries CEO-only mail content. They are INERT: no runner globs ``_staged/``
(run-skill-eval and eval-outcomes read the top level only). Suite entry stays
CEO-gated: the CEO edits the draft, then moves it up into the outcomes directory.
This NEVER writes into live outcomes or cases (matches eval-case-template.md
Phase 4.5).

Both paths run in-process against files: the card path reads the Action Queue
store through `list_action_queue`, the same seam `scripts/action-queue.py`
uses, and the note path touches no queue at all.

Exit codes: 0 ok, 1 usage error, 2 the queue store answered off the Action
Queue contract (only on the <id> path).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.record_readers.sources.action_queue import list_action_queue
from scripts.utils import tracing
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET
from scripts.utils.data_path import data_path
from scripts.utils.workspace import get_data_root, get_default_tz, get_workspace_root

ROOT = get_workspace_root()
SKILLS_DIR = ROOT / ".claude" / "skills"
# Live Action Queue cards come from the email/CRM/comms spine; drafts captured
# from a card default to this skill. Override with --skill.
DEFAULT_CARD_SKILL = "email-intel"


# ============================================================
# Action Queue reader (in-process, the same seam scripts/action-queue.py uses)
# ============================================================
#
# The store is append-only JSONL written by several producers, so `_resolve_id`
# checks the shape of what it reads.


def _die_off_contract(what: str) -> None:
    """The queue store answered, but not with a list of card objects.

    Exit 2: the store this tool needs is not shaped like the queue. Not a
    usage error, and not a missing card.
    """
    print(f"{RED}the Action Queue store is off contract{RESET}: {what}\n"
          f"  (read it with `python scripts/action-queue.py list`)", file=sys.stderr)
    sys.exit(2)


def _resolve_id(items: list[dict], prefix: str) -> str:
    # `list[dict]` is an annotation, not a check: every branch below calls
    # `c.get(...)`, so the ELEMENTS are checked here.
    #
    # Refused whole, never filtered. Dropping the non-dict elements and
    # resolving against the rest would answer "no active card matches" over a
    # queue this tool could not read, which is the wrong-answer version of the
    # same bug.
    if not isinstance(items, list) or not all(isinstance(c, dict) for c in items):
        kinds = type(items).__name__ if not isinstance(items, list) else \
            sorted({type(c).__name__ for c in items})
        _die_off_contract(f"'items' must be a list of card objects, got {kinds}")
    exact = [c for c in items if c.get("id") == prefix]
    if exact:
        return exact[0]["id"]
    matches = [c for c in items if str(c.get("id", "")).startswith(prefix)]
    if not matches:
        print(f"{RED}no active card matches '{prefix}'{RESET}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"{RED}ambiguous prefix '{prefix}' ({len(matches)} matches){RESET}", file=sys.stderr)
        sys.exit(1)
    return matches[0]["id"]


# ============================================================
# Draft staging
# ============================================================

def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:40] or "flagged")


_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _valid_skill(skill: str) -> str:
    """A skill NAME, not a path fragment.

    The name becomes a path segment, so `..` or an absolute value would steer
    `_stage_draft` to write wherever it was asked.
    """
    if not isinstance(skill, str) or not _SKILL_NAME_RE.match(skill):
        raise ValueError(
            f"--skill must be a bare skill name matching [a-z0-9][a-z0-9-]*, "
            f"got {skill!r}"
        )
    return skill


def _staged_dir(skill: str) -> Path:
    """The registry name `skill-eval-drafts` for one skill, in the data root."""
    staged = data_path("skill-eval-drafts", skill=_valid_skill(skill))
    resolved = staged.resolve()
    # Belt and braces: the name rule already forbids traversal, and this check
    # is what makes that a guarantee rather than a claim.
    data_root = get_data_root().resolve()
    if not resolved.is_relative_to(data_root):
        raise ValueError(f"refusing a staged dir outside {data_root}: {resolved}")
    return staged


def _stage_draft(skill: str, draft: dict) -> Path:
    """Atomically write a draft JSON into the skill's _staged/ dir."""
    staged = _staged_dir(skill)
    staged.mkdir(parents=True, exist_ok=True)
    path = staged / f"{draft['id']}.json"
    # A unique scratch name per writer. One fixed `<id>.json.tmp` meant two
    # concurrent captures could write the same scratch path, and one
    # `os.replace` moved the other writer's file out from under it.
    fd, tmp_name = tempfile.mkstemp(dir=str(staged), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(draft, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def _new_draft(description: str, input_text: str, trace_id: str, source: str,
               case_type: str) -> dict:
    ts = datetime.now(get_default_tz()).strftime("%Y-%m-%dT%H%M%S")
    # The id is also the filename. Second resolution plus a slug of the
    # description meant two captures of the same regression inside one second
    # produced the SAME path, and the second silently overwrote the first —
    # one of the two reported regressions simply gone. The suffix is what makes
    # each capture its own file.
    uniq = uuid.uuid4().hex[:6]
    draft: dict = {
        "id": f"flag-{ts}-{_slugify(description)}-{uniq}",
        "description": description or "(untitled)",
        "input": input_text,
        "trace_id": trace_id,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "status": "draft",
    }
    if case_type == "prose":
        draft["checks"] = {"must_mention": [], "must_not_mention": []}
    else:
        draft["outcome"] = {
            "type": "TODO (crm_log | doctype_render)",
            "note": "Fill in the expected side-effect, then move this file up into the outcomes directory.",
        }
    return draft


# ============================================================
# Commands
# ============================================================

def cmd_list(as_json: bool) -> int:
    drafts: list[dict] = []
    # One staged directory per engine skill; the registry address takes the
    # skill name as a parameter, so the skills are enumerated, not globbed.
    for skill in sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir()):
        staged = _staged_dir(skill)
        if not staged.is_dir():
            continue
        for f in sorted(staged.glob("*.json")):
            # An unreadable draft used to fall back to `{}` and then list with
            # an empty description - indistinguishable from an untitled draft
            # the operator simply had not filled in. The one file that needs
            # attention looked like the ones that do not.
            #
            # Caught BY CATEGORY, not by enumeration. The tuple used to name
            # `json.JSONDecodeError` and `OSError`, and a draft holding invalid
            # UTF-8 raises `UnicodeDecodeError`, disjoint from both, so it
            # escaped this handler, reached `main`'s `except ValueError`, and
            # printed a raw codec message having listed NOTHING. The one bad
            # file hid every good one: the precise inversion of the feature.
            # `ValueError` is the category of "the bytes are not JSON text":
            # `json.JSONDecodeError` and `UnicodeDecodeError` both subclass it,
            # and it is disjoint from `OSError`, which is "the file would not
            # open". Together they are complete over `read_text` + `loads` and
            # still refuse TypeError, KeyError and every unrelated bug, so a
            # later defect here surfaces instead of being swallowed. Adding the
            # next sibling by name would have left the one after it out.
            unreadable = ""
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                d, unreadable = {}, f"UNREADABLE ({type(e).__name__}: {e})"
            if isinstance(d, dict):
                desc = d.get("description", "")
            else:
                d, unreadable = {}, f"UNREADABLE (not a JSON object: {type(d).__name__})"
                desc = ""
            drafts.append({
                "skill": skill,
                "path": str(f),
                "id": d.get("id", f.stem),
                "description": unreadable or desc,
                "unreadable": bool(unreadable),
                "trace_id": d.get("trace_id", "-"),
            })
    if as_json:
        print(json.dumps(drafts, indent=2, ensure_ascii=False))
        return 0
    if not drafts:
        print(f"{GRAY}no staged eval drafts{RESET}")
        return 0
    print(f"{BOLD}{len(drafts)} staged eval draft(s){RESET}")
    for d in drafts:
        print(f"  {CYAN}{d['skill']}{RESET}  {d['id']}  {GRAY}trace={d['trace_id']}{RESET}")
        colour = RED if d["unreadable"] else ""
        print(f"      {colour}{d['description']}{RESET if colour else ''}")
        print(f"      {GRAY}{d['path']}{RESET}")
    return 0


def cmd_from_card(card_id: str, skill: str | None, case_type: str, as_json: bool) -> int:
    items = list_action_queue(get_data_root()).get("items", [])
    aid = _resolve_id(items, card_id)
    card = next(c for c in items if c.get("id") == aid)

    # Defensive field mapping: title is the only guaranteed human field; the
    # rest are email_send-only and absent on note/alert/pipeline_update cards.
    description = card.get("title") or "(untitled)"
    parts = []
    for key in ("subject", "draft_body", "note"):
        val = card.get(key)
        if val:
            parts.append(f"{key}: {val}")
    input_text = "\n".join(parts) if parts else description
    recipient = card.get("to")
    if recipient:
        input_text = f"to: {recipient}\n{input_text}"
    trace_id = card.get("trace_id") or tracing.get() or "-"
    target_skill = skill or DEFAULT_CARD_SKILL

    draft = _new_draft(description, input_text, trace_id,
                       source=f"action-queue-card:{aid}", case_type=case_type)
    draft["card_action_type"] = card.get("action_type", "")
    path = _stage_draft(target_skill, draft)
    if as_json:
        print(json.dumps({"staged": str(path), "skill": target_skill,
                          "id": draft["id"], "trace_id": trace_id}, indent=2))
    else:
        print(f"{GREEN}staged{RESET} draft from card {aid[:8]} -> "
              f"{GRAY}{path}{RESET}  (trace {trace_id})")
        print("  edit it, then mv it up into the outcomes directory to promote (CEO-gated).")
    return 0


def cmd_offline(skill: str, note: str, input_file: str | None, case_type: str,
                as_json: bool) -> int:
    input_text = note
    if input_file:
        try:
            input_text = Path(input_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"{RED}cannot read --input-file{RESET}: {e}", file=sys.stderr)
            return 1
    trace_id = tracing.get() or "-"
    draft = _new_draft(note, input_text, trace_id, source="manual", case_type=case_type)
    path = _stage_draft(skill, draft)
    if as_json:
        print(json.dumps({"staged": str(path), "skill": skill,
                          "id": draft["id"], "trace_id": trace_id}, indent=2))
    else:
        print(f"{GREEN}staged{RESET} draft -> {GRAY}{path}{RESET}  "
              f"(trace {trace_id})")
        print("  edit it, then mv it up into the outcomes directory to promote (CEO-gated).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Capture a flagged bad output as a draft regression case (R13).")
    ap.add_argument("id", nargs="?", help="Action Queue card id-or-prefix (live-card path)")
    ap.add_argument("--skill", help="target skill dir (required for the offline path; "
                                    "overrides the default for the card path)")
    ap.add_argument("--note", help="what was wrong (offline path)")
    ap.add_argument("--input-file", help="file whose contents are the case input (offline path)")
    ap.add_argument("--type", choices=["outcome", "prose"], default="outcome",
                    help="draft case shape (default: outcome)")
    ap.add_argument("--list", action="store_true", help="list staged drafts")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    # `_valid_skill` raises ValueError on a path fragment, and nothing caught
    # it: `--skill ../evil` printed a raw traceback. The sibling tool over the
    # same validator (eval-outcomes.py) already answers a bad --skill with the
    # message and exit 1, and one refusal that reads as a crash teaches the
    # operator to distrust both.
    try:
        if args.list:
            return cmd_list(args.json)
        if args.id:
            return cmd_from_card(args.id, args.skill, args.type, args.json)
        if args.skill and args.note:
            return cmd_offline(args.skill, args.note, args.input_file, args.type, args.json)
    except ValueError as e:
        print(f"{RED}{e}{RESET}", file=sys.stderr)
        return 1

    print(f"{RED}usage error{RESET}: give a card <id>, or --skill NAME --note \"...\", "
          f"or --list", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
