#!/usr/bin/env python3
"""
context-floor-audit.py - measure the always-loaded context floor, repeatably.

Every session pays the floor before it does any work, and every compaction
re-injects it. The skill descriptions and the rest of the skill frontmatter
were once conflated into one ad hoc figure that set an unreachable target
(2026-08-19), so this script reports the two separately and never conflates them, reports
bytes as bytes and tokens as tokens, and says plainly what its method cannot
see.

WHAT IT CANNOT SEE, stated up front because the number is otherwise read as
complete: this measures WORKSPACE-OWNED content only. The system prompt, the
tool schemas, and the harness's own scaffolding are not on disk here and are not
counted. It also cannot observe WHICH parts of a SKILL.md the harness injects -
that is a harness behaviour, not a file property - so it reports the description
component and the remaining frontmatter as two candidate surfaces and leaves the
choice between them to whoever can measure the harness.

Usage:
    python scripts/context-floor-audit.py
    python scripts/context-floor-audit.py --json
    python scripts/context-floor-audit.py --write-baseline
    python scripts/context-floor-audit.py --baseline          # exit 1 on growth
    python scripts/context-floor-audit.py sessions [--days N] [--root PATH] [--json]

The `sessions` mode reads the top-level session transcripts under
~/.claude/projects modified in the last N days (default 30) and prints
aggregates only: attachments by type, path-scoped rules, Hindsight content
signatures, tool results by tool, and the peak context per session. It never
prints message text, project directory names or session ids. Subagent
transcripts and directories whose name contains `probe` are left out and
counted.

THE BASELINE IS A RATCHET, AND SOMEONE HAS TO TURN IT. `--baseline` fails only
upward, so every byte the floor loses becomes permanent slack the floor may
regrow for free. Nothing re-tightens it automatically, by design: an
auto-shrinking baseline would silently redefine the target between two runs and
no diff would ever show it. Re-baselining is therefore a deliberate, reviewable
commit, in BOTH directions:

    - after a reduction, re-run `--write-baseline` to bank the win, or the next
      author inherits the slack rather than the gain;
    - after a legitimate addition (a new skill, a new always-on rule) that
      pushes the floor up, `--write-baseline` is how you raise it on purpose.
      Raising it is not cheating the gate; failing to say you raised it is. The
      committed diff to config/context-floor-baseline.json IS the disclosure.

NOT REPRODUCIBLE OFF THE OPERATOR WORKSPACE, which is why no CI job or
pre-commit hook consumes `--baseline` today. Two of the five components
(`memory_index`, and the private-overlay half of `claude_md`, which since
2026-09-20 is the whole `CLAUDE.operational.md` chain rather than that file
alone) live in the private data overlay, so a clone without one measures
roughly 40 KB lower rather than the ~25 KB this said before that date. A
baseline written here fails open in CI (always a shrink); one written in CI
fires spuriously here (always growth). Wiring a blocking gate needs an
engine-only subtotal
first - the three components that ARE reproducible anywhere are
`skill_descriptions`, `skill_other_frontmatter` and `always_on_rules`.

Coverage: no live check since the 2026-09-21 test-suite split. Read this file
before changing it, because nothing else will object.
"""

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.claude_md_chain import HARNESS_MAX_DEPTH, resolve_chain  # noqa: E402
from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.checkpoint_paths import transcript_dir  # noqa: E402
from scripts.utils.colors import BOLD, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.markdown import FM_OK, split_frontmatter  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402

# Bytes per token, as an ESTIMATE. English prose and YAML sit near four bytes per
# token for this tokenizer family. The ratio is stated wherever a token figure is
# printed, because the mistake this script exists to prevent was a byte count
# wearing a token label.
BYTES_PER_TOKEN = 4

# `description:` runs until the next top-level key. Skill descriptions are often
# folded scalars spanning many lines, so a single-line match undercounts badly.
# `|\Z` in the lookahead. Without it the pattern required a FOLLOWING top-level
# key, so a `description:` that is the last key in a frontmatter block matched
# nothing at all: that skill was counted at description_bytes 0, its whole
# frontmatter was attributed to `skill_other_frontmatter`, and both of the two
# totals this script exists to separate moved the wrong way. YAML key order is
# free, so description-last is an ordinary layout. The header says this script
# was written because that exact conflation once set an unreachable reduction
# target.
DESCRIPTION_RE = re.compile(r"^description:\s*(.*?)(?=^[A-Za-z_][\w-]*:|\Z)",
                            re.S | re.M)

BASELINE_PATH = "config/context-floor-baseline.json"
GROWTH_TOLERANCE = 0.05


def _tokens(byte_count: int) -> int:
    return byte_count // BYTES_PER_TOKEN


def _read_or_refuse(path: Path) -> str:
    """The file's text, retried once, REFUSING rather than skipping when it is gone.

    Both walks below gather their paths and read them afterwards. Several agents
    work against one checkout here, so a file can be created and deleted inside
    that window and `read_text` then raises FileNotFoundError from the middle of
    an audit that had found nothing wrong.

    This deliberately does NOT use `scripts.utils.repo_files.read_sources`, the
    shared answer for that race, because `read_sources` SKIPS the vanished path
    and warns. That is right for a scanner whose verdict is per-file. It is wrong
    here, because every byte this script reads is summed into a BUDGET that
    `--baseline` ratchets. `measure_skills` already spells out the direction: "a
    dropped skill makes the measured floor SMALLER: the gate would then pass on a
    floor that grew, which is the one direction a gate must not fail in." A
    warning beside a floor that is short by one file is still a floor that is
    short by one file, and the ratchet reads it as a shrink.

    The retry is not decoration and it is not a fix for deletion. It recovers one
    shape only: a writer that unlinks and rewrites, leaving the path briefly
    absent. A file that is genuinely gone is still gone on the second look, and
    that is the case this refuses, naming it.

    `errors="replace"` is kept from both call sites: a byte that does not decode
    still has a length, and this script counts bytes. Only the vanished case is
    caught; a permission error or a directory handed in where a file was expected
    is a real fault about a file that IS there and still raises.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        pass
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise SystemExit(
            f"{RED}{path}: listed by the walk and gone by the read. This audit "
            f"sums a byte budget and --baseline is a growth ratchet, so a "
            f"dropped file makes the floor read smaller and growth elsewhere "
            f"passes as a shrink. No figure computed over a corpus that changed "
            f"underneath it can be trusted; re-run once the tree is quiet."
            f"{RESET}"
        ) from exc


def measure_skills(root: Path) -> dict:
    """The catalogue, split into the surface a caller can edit and the rest.

    A SKILL.md this cannot read is NAMED, never skipped in silence. The number
    below feeds a growth gate with a 5% tolerance, and a dropped skill makes the
    measured floor SMALLER: the gate would then pass on a floor that grew, which
    is the one direction a gate must not fail in.
    """
    total_fm = total_desc = count = 0
    per_skill = []
    unreadable: list[str] = []
    for path in sorted((root / ".claude" / "skills").glob("*/SKILL.md")):
        # Refuses on a SKILL.md that vanished between this glob and the read,
        # for the reason the docstring above already gives about a skill that
        # drops out. See `_read_or_refuse` for why skipping is the wrong
        # degradation for a budget.
        text = _read_or_refuse(path)
        frontmatter, _body, kind = split_frontmatter(text)
        if frontmatter is None or kind != FM_OK:
            unreadable.append(f"{path.parent.name} ({kind})")
            continue
        # The shared block KEEPS the newline before the closing fence; dropping
        # it keeps the byte count identical to the recorded baseline
        # (config/context-floor-baseline.json, checked 2026-08-29).
        if frontmatter.endswith("\n"):
            frontmatter = frontmatter[:-1]
            if frontmatter.endswith("\r"):
                frontmatter = frontmatter[:-1]
        count += 1
        fm_bytes = len(frontmatter.encode("utf-8"))
        found = DESCRIPTION_RE.search(frontmatter)
        desc_bytes = len(found.group(1).encode("utf-8")) if found else 0
        total_fm += fm_bytes
        total_desc += desc_bytes
        per_skill.append({
            "skill": path.parent.name,
            "frontmatter_bytes": fm_bytes,
            "description_bytes": desc_bytes,
        })
    per_skill.sort(key=lambda row: row["description_bytes"], reverse=True)
    if unreadable:
        print(f"{YELLOW}warn:{RESET} {len(unreadable)} SKILL.md file(s) carry no "
              f"readable frontmatter and are NOT in the figures below: "
              f"{', '.join(unreadable)}", file=sys.stderr)
    return {
        "skills": count,
        "frontmatter_bytes": total_fm,
        "description_bytes": total_desc,
        "other_frontmatter_bytes": total_fm - total_desc,
        "per_skill": per_skill,
        "unreadable_skills": unreadable,
    }


def _is_always_on(text: str) -> bool:
    """A rule with no `paths:` key, or an empty one, loads in every session.

    Same grammar as `measure_skills`, and for a sharper reason: a rule that
    HAS `paths:` but whose fence carries a trailing space read here as "no
    frontmatter", which this function turns into always-on. A path-scoped rule
    was then counted in the always-on floor and reported to the operator as
    loading in every session when it does not.
    """
    body, _rest, kind = split_frontmatter(text)
    if body is None or kind != FM_OK:
        return True
    found = re.search(r"^paths:(.*)$", body, re.M)
    if not found:
        return True
    trailing = found.group(1).strip()
    if trailing in ("", "[]"):
        # An inline empty list is always-on; a bare `paths:` needs its block read.
        if trailing == "[]":
            return True
        # Read ONLY the block that belongs to `paths:`. It ends at the next
        # top-level key, which in this frontmatter grammar is any line starting
        # at column 0.
        #
        # This scanned to the END of the frontmatter and asked whether ANY
        # indented list item appeared anywhere after `paths:`. So a rule with an
        # empty `paths:` (always-on) that also carried a later YAML list of any
        # kind was classified path-scoped. MEASURED 2026-09-02:
        # `paths:\ntags:\n  - alpha` returned False, the same frontmatter
        # without the later list returned True. One unrelated key decided it.
        #
        # The direction is the dangerous one. `always_on_bytes` is a component
        # of the ratcheted total this script gates growth on, so a rule wrongly
        # called path-scoped takes its bytes OUT of the floor. The floor then
        # drops, the `--baseline` comparison sees room, and the gate passes over
        # a context budget that actually grew.
        rest = body[found.end():]
        next_key = re.search(r"^\S", rest, re.M)
        block = rest[:next_key.start()] if next_key else rest
        return not re.search(r"^\s+-\s+\S", block, re.M)
    return False


def measure_rules(root: Path) -> dict:
    always, scoped = [], []
    for path in sorted((root / ".claude" / "rules").glob("*.md")):
        # The second walk-then-read in this script, refused for the same reason
        # as the first and fixed in the same change: `always_on_bytes` is a
        # component of the ratcheted total, so a rule file that vanished mid-walk
        # lowers the floor exactly like a dropped skill. A fix that lands in one
        # of two copies is the copy that stops being fixed.
        text = _read_or_refuse(path)
        row = {"rule": path.name, "bytes": len(text.encode("utf-8"))}
        (always if _is_always_on(text) else scoped).append(row)
    return {
        "always_on": always,
        "path_scoped": scoped,
        "always_on_bytes": sum(r["bytes"] for r in always),
        "path_scoped_bytes": sum(r["bytes"] for r in scoped),
    }


def _file_bytes(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").encode("utf-8"))
    except OSError:
        return 0


def measure(root: Path) -> dict:
    skills = measure_skills(root)
    rules = measure_rules(root)

    from scripts.utils.data_path import DataPathError, data_path
    from scripts.utils.paths import DataRootError
    from scripts.utils.workspace import get_data_root

    # The registry name `memory-index`, so an edit to its entry moves what this
    # row measures (HOS-5 section 7, 2026-09-25). A tree that cannot answer the
    # name is still reported as zero rather than fatal, as before, but now says
    # so, and only for the two refusals the resolver makes: the bare
    # `except Exception` it replaces also turned a defect in this function into
    # a silent zero.
    try:
        memory = _file_bytes(data_path("memory-index"))
    except (DataPathError, DataRootError) as exc:
        print(f"context-floor-audit: memory index not measured: {exc}", file=sys.stderr)
        memory = 0
    claude_md = _file_bytes(root / "CLAUDE.md")
    try:
        # THE WHOLE CHAIN, not its first file (since 2026-09-20): every file the
        # operational CLAUDE.md imports is resident in every session. Walked
        # through the shared resolver, so a file added to the chain is counted
        # without editing this line; depth is one less than the harness's
        # ceiling because the file sits one import below its root. The engine's
        # own `CLAUDE.md` is NOT walked: it imports nothing, and walking it would
        # double-count the chain where an operator wired the import there.
        operational = get_data_root() / "CLAUDE.operational.md"
        chain = resolve_chain(operational, max_depth=max(1, HARNESS_MAX_DEPTH - 1))
        if not chain.root_readable:
            # A resolvable overlay that does not hold the file. Distinct from
            # the exception below, and it used to be silent: `_file_bytes`
            # answers 0 for a missing file, the ratchet only ever fails upward,
            # and a vanished 26 KB therefore read as a shrink and passed.
            print(f"context-floor-audit: no operational CLAUDE.md at "
                  f"{operational}; 0 bytes counted for the private chain",
                  file=sys.stderr)
        claude_md += sum(len(f.text.encode("utf-8")) for f in chain.files)
        for problem in chain.problems:
            # An import the chain could not follow is bytes a session may well
            # be loading and this total is not counting. Named, with the reason.
            print(f"context-floor-audit: not counted, {problem.describe()}",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a bare engine clone has no overlay
        # Said out loud rather than swallowed: on a public engine clone there is
        # no data overlay and the operational CLAUDE.md genuinely does not exist,
        # which is a smaller floor and not an error. Silence here would let a
        # real resolution failure read as that same benign case.
        print(f"context-floor-audit: no operational CLAUDE.md counted ({exc})",
              file=sys.stderr)

    components = {
        "skill_descriptions": skills["description_bytes"],
        "skill_other_frontmatter": skills["other_frontmatter_bytes"],
        "always_on_rules": rules["always_on_bytes"],
        "memory_index": memory,
        "claude_md": claude_md,
    }
    # Two totals, not one. `total_bytes` sums every surface this script can see on
    # disk. `observed_bytes` sums only the four whose injection into a live session
    # this workspace has actually confirmed by reading its own system prompt.
    #
    # `claude_md` is the engine's CLAUDE.md plus the operational chain, read
    # straight from the data root rather than through the route a session gets
    # it by (the USER-level `~/.claude/CLAUDE.md` import, or the SessionStart
    # hook in a yard). Counting the bytes does not establish which route
    # delivered them, or whether one did.
    #
    # STILL NOT COUNTED, and named rather than implied by silence: the harness's
    # own system prompt, the tool definitions, any skill body a session actually
    # opens, and the USER-level root `~/.claude/CLAUDE.md` itself (only the
    # chain it imports is counted, not its own bytes).
    #
    # A SEPARATE FINDING, named here rather than fixed: this script has no floor
    # over the corpora it walks. `_file_bytes` answers 0 for a file that is not
    # there, `measure_skills` and `measure_rules` answer 0 for a directory that
    # is empty or gone, and `--baseline` only ever fails UPWARD, so a collapse
    # reads as a shrink and passes. The one refusal the script has covers a
    # SKILL.md that will not parse. The two overlay reads above now say so on
    # stderr when they find nothing, which is this row's half of it; the skills
    # and rules walks still do not.
    #
    # The difference is `skill_other_frontmatter`, the x-heading-* blocks (its
    # current size is in `--json`; no figure is kept in prose). The harness's skill listing carries a
    # name and a description; no x-heading content has ever been observed in a
    # session's available-skills block. But this script's method cannot settle
    # that either way, so it does not assert the opposite: the row keeps its place
    # in the on-disk total and is simply excluded from the observed one.
    #
    # Why it matters: config/context-floor-baseline.json commits a number, and a
    # later "we cut the floor by N" has to name WHICH of the two it moved. Added
    # 2026-08-20 per .claude/rules/scope-claims.md.
    observed_keys = (
        "skill_descriptions",
        "always_on_rules",
        "memory_index",
        "claude_md",
    )
    return {
        "bytes_per_token": BYTES_PER_TOKEN,
        "skill_count": skills["skills"],
        # What `skill_count` does NOT cover. A machine reader of this payload
        # would otherwise take the count as the whole catalogue; the stderr
        # warning it cannot see is not a substitute for the field.
        "unreadable_skills": skills["unreadable_skills"],
        "components": components,
        "total_bytes": sum(components.values()),
        "observed_bytes": sum(components[k] for k in observed_keys),
        "observed_components": list(observed_keys),
        "path_scoped_rules_bytes": rules["path_scoped_bytes"],
        "largest_descriptions": skills["per_skill"][:10],
        "always_on_rules": rules["always_on"],
    }


def render(result: dict) -> None:
    print(f"{BOLD}Always-loaded context floor{RESET}  "
          f"{GRAY}({result['skill_count']} skills){RESET}\n")
    print(f"{GRAY}{'component':<28}{'bytes':>12}{'~tokens':>10}{RESET}")
    for name, byte_count in result["components"].items():
        print(f"{name:<28}{byte_count:>12}{_tokens(byte_count):>10}")
    total = result["total_bytes"]
    observed = result["observed_bytes"]
    print(f"{BOLD}{'TOTAL (surfaces on disk)':<28}{total:>12}{_tokens(total):>10}{RESET}")
    print(f"{BOLD}{'OBSERVED FLOOR':<28}{observed:>12}{_tokens(observed):>10}{RESET}"
          f"  {GRAY}<- injection confirmed{RESET}")

    scoped = result["path_scoped_rules_bytes"]
    print(f"\n{GRAY}Path-scoped rules, NOT in the total (they load only on a "
          f"path match): {scoped} bytes, ~{_tokens(scoped)} tokens{RESET}")

    print(f"\n{BOLD}Largest skill descriptions{RESET} "
          f"{GRAY}(the only surface a description edit can shrink){RESET}")
    for row in result["largest_descriptions"]:
        print(f"  {row['skill']:<26}{row['description_bytes']:>7} bytes"
              f"{_tokens(row['description_bytes']):>7} tok")

    router = next(
        (r for r in result["always_on_rules"] if r["rule"] == "skill-router.md"),
        None,
    )
    if router:
        print(f"\n{YELLOW}Note:{RESET} .claude/rules/skill-router.md is counted "
              f"under always-on rules ({router['bytes']} bytes, "
              f"~{_tokens(router['bytes'])} tokens). It carries the trigger list "
              f"for every skill, generated from x-heading-routing.triggers - a "
              f"DIFFERENT field from description. Compressing descriptions does "
              f"not shrink it, so the two wins do not add up.")

    # scope-claims: name what the method cannot establish, rather than letting a
    # partial measurement read as a complete one.
    print(f"\n{GRAY}Method: workspace-owned files only. The system prompt and the "
          f"tool schemas are not on disk here and are NOT counted. Token figures "
          f"are bytes/{BYTES_PER_TOKEN}, an estimate, not a tokenizer result. "
          f"This script cannot observe which parts of a SKILL.md the harness "
          f"actually injects, so it prints two totals. TOTAL sums every surface "
          f"on disk. OBSERVED FLOOR sums only "
          f"{', '.join(result['observed_components'])} - the four whose presence "
          f"in a live system prompt has been read directly. The gap between them "
          f"is skill_other_frontmatter (the x-heading-* blocks), which is on disk "
          f"and whose injection is unconfirmed in either direction. Say which "
          f"total you moved.{RESET}")


# ============================================================
# sessions mode: what fills the context window in real transcripts
# ============================================================

# Values of these attachment keys are metadata, not text the model reads.
ATTACHMENT_META_KEYS = frozenset({
    "type", "hookEvent", "hookName", "path", "uuid", "timestamp", "toolUseID",
})
SIGNATURES = (
    ("refresh", "<hindsight_knowledge_refresh>"),
    ("reflect", "<hindsight_memory>"),
    ("facts", "<memory_facts source="),
)
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf")
SESSIONS_NOTE = ("bytes/4 underestimates tokens for markdown and JSON; "
                 "calibrate against /context on your machine.")


def _utf8(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _attachment_strings(value, key: str | None = None):
    """Every string value inside an attachment, skipping the metadata keys."""
    if isinstance(value, str):
        if key not in ATTACHMENT_META_KEYS:
            yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _attachment_strings(v, k)
    elif isinstance(value, list):
        for item in value:
            yield from _attachment_strings(item, key)


def _result_size(block: dict) -> tuple[int, int]:
    """(bytes, images) of one tool_result block."""
    content = block.get("content")
    if isinstance(content, str):
        return _utf8(content), 0
    size = images = 0
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image":
                images += 1
            elif isinstance(item.get("text"), str):
                size += _utf8(item["text"])
    return size, images


def _nearest_rank(ordered: list[int], p: float) -> int:
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[rank - 1]


def _add(table: dict, name: str, size: int, images: int = 0) -> None:
    row = table.setdefault(name, [0, 0, 0])
    row[0] += 1
    row[1] += size
    row[2] += images


def measure_sessions(root: Path, days: int) -> dict:
    cutoff = time.time() - days * 86400
    scope = {"sessions": 0, "days": days, "probe_dirs_skipped": 0,
             "subagent_files_excluded": 0, "malformed_lines": 0,
             "unreadable_files": 0}
    attachments: dict = {}
    rules: dict = {}
    # Seeded so all three rows print, at zero when nothing matched.
    signatures: dict = {needle: [0, 0, 0] for _, needle in SIGNATURES}
    per_day: dict = {}
    tools: dict = {}
    peaks: list[int] = []
    no_usage = 0

    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        if "probe" in project.name:
            scope["probe_dirs_skipped"] += 1
            continue
        for sub in project.glob("*/subagents/*.jsonl"):
            try:
                if sub.stat().st_mtime >= cutoff:
                    scope["subagent_files_excluded"] += 1
            except OSError as exc:
                print(f"[sessions] cannot stat a subagent file: {exc.strerror}",
                      file=sys.stderr)
        for path in project.glob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                # "\n" only: `splitlines` also breaks on U+2028, U+0085 and
                # the other Unicode separators, which JSON leaves unescaped
                # inside a string, and so cuts one valid record in two.
                lines = path.read_text(encoding="utf-8").split("\n")
            except (OSError, UnicodeDecodeError):
                scope["unreadable_files"] += 1
                continue
            scope["sessions"] += 1
            records = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    scope["malformed_lines"] += 1
                    continue
                if isinstance(record, dict):
                    records.append(record)
                else:
                    scope["malformed_lines"] += 1

            def blocks(record):
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                return [b for b in content if isinstance(b, dict)] \
                    if isinstance(content, list) else []

            uses = {}
            for record in records:
                for b in blocks(record):
                    if b.get("type") == "tool_use" and isinstance(b.get("id"), str):
                        uses[b["id"]] = b
            peak = None
            for record in records:
                attachment = record.get("attachment")
                if isinstance(attachment, dict):
                    size = sum(_utf8(s) for s in _attachment_strings(attachment))
                    kind = str(attachment.get("type"))
                    event = attachment.get("hookEvent")
                    _add(attachments, f"{kind}:{event}" if event else kind, size)
                    if kind == "nested_memory":
                        _add(rules, Path(str(attachment.get("path", ""))).name, size)
                    values = list(_attachment_strings(attachment))
                    day = str(record.get("timestamp", ""))[:10]
                    for label, needle in SIGNATURES:
                        if any(needle in value for value in values):
                            _add(signatures, needle, size)
                            row = per_day.setdefault(
                                day, {lb: 0 for lb, _ in SIGNATURES})
                            row[label] += 1
                for b in blocks(record):
                    if b.get("type") != "tool_result":
                        continue
                    use = uses.get(b.get("tool_use_id"))
                    if use is None:
                        name = "unmatched"
                    else:
                        name = str(use.get("name"))
                        target = (use.get("input") or {}).get("file_path")
                        if name == "Read" and isinstance(target, str) \
                                and target.lower().endswith(IMAGE_SUFFIXES):
                            name = "Read(image/pdf)"
                    _add(tools, name, *_result_size(b))
                if record.get("type") == "assistant":
                    message = record.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if isinstance(usage, dict):
                        total = sum(int(usage.get(k) or 0) for k in (
                            "input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens"))
                        peak = total if peak is None else max(peak, total)
            if peak is None:
                no_usage += 1
            else:
                peaks.append(peak)

    def rows(table: dict, count_key: str = "count") -> list[dict]:
        out = []
        for name, (count, size, images) in sorted(
                table.items(), key=lambda kv: (-kv[1][1], kv[0])):
            row = {"name": name, count_key: count, "bytes": size,
                   "tokens": _tokens(size)}
            if count_key == "calls":
                row["images"] = images
            out.append(row)
        return out

    peaks.sort()
    return {
        "scope": scope,
        "attachments": rows(attachments),
        "rules": rows(rules),
        "signatures": rows(signatures),
        "signatures_per_day": [{"date": d, **per_day[d]} for d in sorted(per_day)],
        "tools": rows(tools, "calls"),
        "sessions": {
            "count": len(peaks) + no_usage,
            "no_usage": no_usage,
            "median": _nearest_rank(peaks, 0.5) if peaks else None,
            "p90": _nearest_rank(peaks, 0.9) if peaks else None,
            "max": peaks[-1] if peaks else None,
        },
        "note": SESSIONS_NOTE,
    }


def render_sessions(result: dict) -> None:
    s = result["scope"]
    print(f"{BOLD}Scope{RESET}  sessions read {s['sessions']}, days {s['days']}, "
          f"probe directories skipped {s['probe_dirs_skipped']}, subagent files "
          f"excluded {s['subagent_files_excluded']}, malformed lines skipped "
          f"{s['malformed_lines']}, unreadable files skipped "
          f"{s['unreadable_files']}")

    def table(title: str, rows: list[dict], first: str = "count",
              images: bool = False) -> None:
        print(f"\n{BOLD}{title}{RESET}")
        head = f"{'name':<52}{first:>8}{'bytes':>12}{'~tokens':>10}"
        if images:
            head += f"{'images':>8}"
        print(f"{GRAY}{head}{RESET}")
        for r in rows:
            line = f"{r['name']:<52}{r[first]:>8}{r['bytes']:>12}{r['tokens']:>10}"
            print(line + (f"{r['images']:>8}" if images else ""))

    table("Attachments by type", result["attachments"])
    table("Path-scoped rules", result["rules"])
    table("Content signatures", result["signatures"])
    print(f"\n{BOLD}Signatures per day{RESET}")
    print(f"{GRAY}{'date':<12}"
          + "".join(f"{label:>10}" for label, _ in SIGNATURES) + RESET)
    for row in result["signatures_per_day"]:
        print(f"{row['date']:<12}"
              + "".join(f"{row[label]:>10}" for label, _ in SIGNATURES))
    table("Tool results", result["tools"], "calls", images=True)

    st = result["sessions"]
    print(f"\n{BOLD}Sessions{RESET}  {GRAY}(peak input + cache read + cache "
          f"creation tokens per session, nearest-rank){RESET}")

    def show(v):
        return "-" if v is None else str(v)
    print(f"count {st['count']}  no usage {st['no_usage']}  median "
          f"{show(st['median'])}  p90 {show(st['p90'])}  max {show(st['max'])}")
    print(f"\n{YELLOW}Note:{RESET} {result['note']}")


def sessions_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="context-floor-audit.py sessions",
        description="Aggregate what filled the context window in recent "
                    "session transcripts. Prints aggregates only.",
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--root", type=Path,
                        help="transcript root (default: the harness's own, "
                             "~/.claude/projects)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.root is not None:
        root = args.root.expanduser()
    else:
        # The one copy of the harness's transcript layout; its parent is the
        # root holding every project's transcripts.
        own = transcript_dir(get_workspace_root())
        if own is None:
            print("sessions: no default transcript root off POSIX; pass --root",
                  file=sys.stderr)
            return 2
        root = own.parent
    if not root.is_dir():
        print(f"sessions: --root is not a directory: {root}", file=sys.stderr)
        return 2
    result = measure_sessions(root, args.days)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        render_sessions(result)
    return 0


def main() -> int:
    if sys.argv[1:2] == ["sessions"]:
        return sessions_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Measure the always-loaded context floor by component."
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="overwrite the committed baseline with the current measurement. "
             "Use it to bank a reduction, and to RAISE the floor deliberately "
             "when a new skill or always-on rule legitimately grows it. The "
             "diff to " + BASELINE_PATH + " is the disclosure that it moved.",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="compare against the committed baseline and exit 1 on growth "
             "beyond the tolerance. Never fails on a shrink, so the recorded "
             "number only ratchets when someone runs --write-baseline.",
    )
    args = parser.parse_args()

    root = get_workspace_root()
    result = measure(root)
    baseline_file = root / BASELINE_PATH

    # A corpus with a hole in it is not a floor, and `measure_skills` already
    # spells out why: "a dropped skill makes the measured floor SMALLER: the
    # gate would then pass on a floor that grew, which is the one direction a
    # gate must not fail in." Until 2026-09-01 that reasoning ended at a warning
    # on stderr and neither ratchet mode read `unreadable_skills`. MEASURED in
    # the ratchet sandbox: truncating one SKILL.md's closing fence took the
    # measured floor from 273 bytes to 96 and `--baseline` called the drop
    # "within tolerance" and exited 0.
    #
    # Both modes refuse, and banking is the worse of the two: a comparison that
    # passes wrongly is undone by the next run, while a baseline WRITTEN while a
    # skill was invisible holds the ratchet at a number that was never the floor.
    # A bare informational run still prints, because reporting is not asserting.
    if (args.baseline or args.write_baseline) and result["unreadable_skills"]:
        print(f"\n{RED}Refusing: {len(result['unreadable_skills'])} SKILL.md "
              f"file(s) carry no readable frontmatter, so this measurement is "
              f"smaller than the real floor and growth elsewhere would read as "
              f"a shrink: {', '.join(result['unreadable_skills'])}{RESET}",
              file=sys.stderr)
        return 1

    if args.write_baseline:
        atomic_write_text(
            baseline_file,
            json.dumps({
                "components": result["components"],
                "total_bytes": result["total_bytes"],
                "skill_count": result["skill_count"],
                "bytes_per_token": BYTES_PER_TOKEN,
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{GREEN}Baseline written to {BASELINE_PATH}{RESET}")
        return 0

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        render(result)

    if args.baseline:
        # The verdict goes to stderr, never stdout. With `--json --baseline`
        # these three lines were printed AFTER the JSON document on the same
        # stream, so a machine caller got an unparseable mix. `crm-health.py`
        # already routes its warnings to stderr for exactly this reason, and
        # says so in a comment.
        def verdict(line: str) -> None:
            print(line, file=sys.stderr)

        if not baseline_file.is_file():
            verdict(f"\n{YELLOW}No baseline at {BASELINE_PATH}; "
                    f"write one with --write-baseline.{RESET}")
            return 1
        recorded = json.loads(baseline_file.read_text(encoding="utf-8"))
        was = recorded.get("total_bytes", 0)
        now = result["total_bytes"]
        ceiling = was * (1 + GROWTH_TOLERANCE)
        if now > ceiling:
            verdict(f"\n{RED}Floor grew: {was} -> {now} bytes "
                    f"(tolerance {int(GROWTH_TOLERANCE * 100)}%).{RESET}")
            return 1
        verdict(f"\n{GREEN}Floor within tolerance: {was} -> {now} bytes.{RESET}")
        # A pass is not the whole story. The gate only fails upward, so every
        # byte between the live figure and the ceiling is room the floor may
        # regrow unnoticed. Printing the slack is what makes a stale baseline
        # visible; without it "within tolerance" reads as "tight" and the
        # ratchet quietly stops ratcheting.
        slack = int(ceiling) - now
        if slack > 0:
            verdict(f"{YELLOW}Slack: {slack} bytes (~{_tokens(slack)} tokens) "
                    f"before this gate fires - the baseline is {was} and the "
                    f"ceiling is {int(ceiling)} at {int(GROWTH_TOLERANCE * 100)}% "
                    f"tolerance. Re-run with --write-baseline to bank the "
                    f"reduction, or the next author inherits the slack.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
