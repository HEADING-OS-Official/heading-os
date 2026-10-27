#!/usr/bin/env python3
"""Council aggregate - Track C frontier-evidence reader.

Walks every /council transcript wherever the seam below resolves them,
extracts the question + each model's answer, and writes a single aggregate with
side-by-side summaries and the recorded CEO verdict (if any).

VERDICT WORKFLOW (2026-05-24 redesign): the CEO does NOT edit this file.
After every /council run, the model asks the CEO which answer landed best
and calls scripts/council-record-verdict.py to append the choice to
_verdicts.jsonl. This script then reads that JSONL and renders the
verdicts inline. The aggregate is a READ-ONLY view onto the verdict log.

Once >=20 verdicts accumulate, the data is enough to calibrate a
Gemini-as-judge mode (Phase 3b per the plan). Until then, every entry is
either Recorded (with choice + notes) or Pending CEO verdict.

Usage:
  python scripts/council-aggregate.py              # rebuild aggregate
  python scripts/council-aggregate.py --json       # JSON output

Output: `_aggregate.md` under the registry name `council-transcripts`
(CEO-only by directory default classification). Its links to the transcripts
are relative to that directory, so a transcript filed under `council-reports`
still links.

Every address here is a registry name read through `scripts/utils/data_path.py`.
The council is TWO names: a transcript whose topic carries a report hint word
("review", "audit", "brief", "summary", "analysis", "findings") was filed under
`council-reports` by the HEADING OS 2 move, every other one under
`council-transcripts` (MEASURED 2026-09-14). Both are read and unioned.

The verdict ledger is the registry name `council-verdicts`, the file
`scripts/council-record-verdict.py` (its only writer) appends to. `verdicts_paths()`
also looks for a `_verdicts.jsonl` among the council records and beside each
transcript directory, so a ledger left elsewhere by a partial move is still
read. Merging across ledgers is by `recorded_at`, which the writer stamps on
every record; within one ledger the last line wins. When no timestamp settles
two differing records, nothing is chosen: the entry renders as a CONFLICT,
counts as pending, and is named on stderr.

Cross-platform: pure Python, runs anywhere a workspace clone runs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from scripts.utils.colors import CYAN, GREEN, RESET, YELLOW  # noqa: E402
from scripts.utils.council_models import VERDICT_CHOICES  # noqa: E402
from scripts.utils.jsonl_lines import jsonl_lines  # noqa: E402
from scripts.utils.markdown import FM_OK, split_frontmatter  # noqa: E402
from scripts.utils.data_path import data_path, data_read  # noqa: E402
from scripts.utils.workspace import display_path, get_default_tz  # noqa: E402


#: The append-only verdict ledger. Located as a record of its own AND beside
#: the transcripts; see `verdicts_paths()`.
VERDICTS_FILENAME = "_verdicts.jsonl"


def council_dir() -> Path:
    """The registry name `council-transcripts`, the transcripts' own home.

    The fallback directory `transcript_dirs()` answers when no transcript is
    found, and where a rebuilt aggregate is written.
    """
    return data_path("council-transcripts")


def council_records(suffixes: tuple[str, ...] | None = (".md",)) -> list[Path]:
    """Every council file on disk now, of either kind, on either side of the move.

    Both names are asked, `council-transcripts` (`("trace", "scratch")`) and
    `council-reports` (`("trace", "report")`), because `manifest._operations_leaf`
    files a council file under `report` when its topic carries a hint word
    ("review", "audit", "brief") and under `scratch` otherwise. The two answers
    are unioned and deduplicated: on a tree with no manifest both names degrade
    to the same pre-move directory, so the same file arrives twice.

    One level of each directory, as the `depth=1` filter this replaced allowed:
    a nested `.../council/archive/x.md` is not a council record. A symlink is
    never a record, as `resolve.read_records` rules; a directory that is not
    there contributes nothing.
    """
    found: dict[Path, None] = {}
    # Each name spelled at its own call, so the leak guard can read which data
    # it addresses.
    for directory in (*data_read("council-transcripts"),
                      *data_read("council-reports")):
        if not directory.is_dir():
            continue
        for p in directory.iterdir():
            if p.is_symlink() or not p.is_file():
                continue
            if suffixes is None or p.suffix in suffixes:
                found[p] = None
    return sorted(found)


def _is_transcript_name(name: str) -> bool:
    """`_aggregate.md` and `_verdicts.jsonl` are not transcripts, and neither is
    a dotfile. The rule predates the seam and does the same job on both sides."""
    return not name.startswith("_") and not name.startswith(".")


def _is_transcript(path: Path) -> bool:
    """The name rule plus the extension, because the corpus is no longer
    markdown-only: the ledger is resolved as a record now, so `main` asks the
    seam for every suffix and filters here."""
    return path.suffix == ".md" and _is_transcript_name(path.name)


def transcript_dirs(records: list[Path] | None = None) -> list[Path]:
    """Every directory the council TRANSCRIPTS were found in.

    `records` lets a caller that has already resolved the set hand it over
    rather than making the seam rglob the tree again; `main` does, so one run is
    one resolution instead of three.

    One entry in the ordinary case; more than one only while a partial move has
    records on both sides. Derived from the transcripts rather than from every
    council record, so the aggregate this script itself writes into the new
    tree cannot add a directory to the list and send the ledger search after a
    file that was never there."""
    found = council_records() if records is None else records
    dirs = sorted({p.parent for p in found if _is_transcript(p)})
    return dirs or [council_dir()]


def aggregate_path() -> Path:
    """Where a REBUILT aggregate is written."""
    return council_dir() / "_aggregate.md"


def verdicts_path() -> Path:
    """The live ledger: the registry name `council-verdicts`, the file its only
    writer appends to. `verdicts_paths()` is the full set the run reads."""
    return data_path("council-verdicts")


def verdicts_paths(dirs: list[Path] | None = None,
                   records: list[Path] | None = None) -> list[Path]:
    """Every `_verdicts.jsonl` to merge, from three sources that overlap on purpose.

    1. The ledger AS A RECORD: any council record of either name called
       `_verdicts.jsonl`.
    2. Beside each directory holding transcripts.
    3. `verdicts_path()`, the live ledger its only writer appends to.

    The ledger is one file and the transcripts span two names, so a partial
    move can separate them; the union is cheap because a path that does not
    exist costs nothing (`_merge_verdicts` skips it).
    """
    # Resolved once and reused for both source 1 and source 2, so a
    # no-argument call is one pass over the seam rather than two.
    resolved = council_records(suffixes=None) if records is None else records
    out: dict[Path, None] = {}
    for p in resolved:
        if p.name == VERDICTS_FILENAME:
            out[p] = None
    for directory in (transcript_dirs(resolved) if dirs is None else dirs):
        out[directory / VERDICTS_FILENAME] = None
    out[verdicts_path()] = None
    return list(out)

_H1_TOPIC_RE = re.compile(r"^# (?:Council Consultation\s*-\s*)?(.*?)$", re.MULTILINE)
_SECTION_RES = {
    "question": re.compile(r"^## (?:Question|Question\s*/\s*Draft|Draft).*?\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE),
    # The plain terminator, like `question` and `claude` two lines away. These
    # three carried `(?!Side-by-side)`, so a `## Side-by-side` heading did NOT
    # end the capture and that section's content — the COMPARISON table — was
    # folded into whichever model's response preceded it, in the file whose
    # whole job is attributing an answer to a model.
    #
    # It never fired on the canonical layout, where Side-by-side comes after
    # every full response and after Claude's view
    # (`.claude/skills/council/references/transcript-format.md`). It fired on a
    # transcript that deviates: one model present, then the table. Dead on the
    # normal path, wrong on the abnormal one, and inconsistent with its siblings
    # for no recorded reason.
    "gemini":   re.compile(r"^## Gemini'?s full response.*?\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE),
    "grok":     re.compile(r"^## Grok'?s full response.*?\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE),
    "kimi":     re.compile(r"^## Kimi'?s full response.*?\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE),
    "chatgpt":  re.compile(r"^## ChatGPT'?s full response.*?\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE),
    "claude":   re.compile(r"^## Claude'?s (?:view|response|answer|critique).*?\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE),
}


# Display spelling for the choices whose name is not `str.capitalize()` of the
# key. Only entries that differ belong here; everything else falls through.
_LABELS = {"chatgpt": "ChatGPT"}


@dataclass
class Transcript:
    """One /council run extracted to comparison form."""
    path: Path
    timestamp: str
    mode: str
    topic: str
    question_snippet: str
    claude_snippet: str
    gemini_snippet: str
    grok_snippet: str
    kimi_snippet: str
    chatgpt_snippet: str

    def models_present(self) -> list[str]:
        out = []
        if self.claude_snippet:  out.append("claude")
        if self.gemini_snippet:  out.append("gemini")
        if self.grok_snippet:    out.append("grok")
        if self.kimi_snippet:    out.append("kimi")
        if self.chatgpt_snippet: out.append("chatgpt")
        return out


def _snippet(text: str, max_chars: int = 320) -> str:
    """Squash whitespace + truncate cleanly. None / empty -> empty string."""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", text.strip())
    if len(t) <= max_chars:
        return t
    # Trim to last word boundary before max_chars
    truncated = t[:max_chars].rsplit(" ", 1)[0]
    return truncated + "..."


def _parse_frontmatter(text: str) -> dict:
    """The transcript's `key: value` header, or {} when it has none.

    The fences come from the shared splitter. `^---\\n(.*?)\\n---` sat here and
    took the fence to be exactly three characters. MEASURED 2026-08-29: a
    transcript whose fence carries a trailing space or a tab returned {}, and
    `parse_transcript` reads `mode` out of this dict to decide whether the file
    IS a council transcript at all. A transcript with no recognised section
    heading was therefore dropped from the aggregate entirely, and one with a
    section fell through to `mode: "?"` and a timestamp guessed from the
    filename.
    """
    block, _body, kind = split_frontmatter(text)
    if block is None or kind != FM_OK:
        return {}
    out: dict = {}
    for line in block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def parse_transcript(path: Path) -> Transcript | None:
    """Parse one transcript file. Returns None on shape mismatch.

    It never did. Only `OSError` returned None, so ANY readable `.md` that
    landed in the council directory — a scratch note, a README, anything not
    prefixed `_` or `.` — became a Transcript with `mode="?"`, empty snippets,
    and a rendered "_(pending CEO verdict)_" row. Those inflate the Pending
    count that feeds the Phase-3b calibration gate ("once >= 20 verdicts
    accumulate"), so a stray file moves a threshold the operator reads as a
    measurement.

    A council transcript is recognisable: it carries frontmatter with a `mode`,
    or at least one of the sections this aggregator renders. A file with
    neither is not one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it used
        # to escape this handler entirely. `collect_transcripts` has no guard
        # of its own, so ONE stray `.md` saved as Latin-1 by an editor -- the
        # exact class of file the paragraph above is about -- aborted the whole
        # scan and left the aggregate unwritten. MEASURED 2026-08-29. The
        # contract is skip what is not a transcript, and an undecodable file is
        # not one.
        return None
    fm = _parse_frontmatter(text)
    topic_match = _H1_TOPIC_RE.search(text)
    topic = topic_match.group(1).strip() if topic_match else path.stem

    def _section(key: str) -> str:
        m = _SECTION_RES[key].search(text)
        return m.group(1).strip() if m else ""

    if not fm.get("mode") and not any(_section(k) for k in _SECTION_RES):
        return None

    return Transcript(
        path=path,
        timestamp=fm.get("timestamp", path.stem.split("_")[0]),
        mode=fm.get("mode", "?"),
        topic=topic[:120],
        question_snippet=_snippet(_section("question")),
        claude_snippet=_snippet(_section("claude")),
        gemini_snippet=_snippet(_section("gemini")),
        grok_snippet=_snippet(_section("grok")),
        kimi_snippet=_snippet(_section("kimi")),
        chatgpt_snippet=_snippet(_section("chatgpt")),
    )


def collect_transcripts(records: list[Path] | None = None) -> list[Transcript]:
    # The leading-underscore skip is what keeps `_aggregate.md` and
    # `_verdicts.jsonl` out of the corpus this file rebuilds from; it predates
    # the seam and does the same job on both sides of the move.
    found = council_records() if records is None else records
    files = sorted(
        [p for p in found if _is_transcript(p)],
        key=lambda p: p.name,
        reverse=True,  # newest first
    )
    parsed = [t for t in (parse_transcript(p) for p in files) if t is not None]
    return parsed


# ============================================================
# Verdict loading (source of truth = _verdicts.jsonl, last-write-wins)
# ============================================================
def load_verdicts(paths: list[Path] | None = None) -> dict[str, dict]:
    """Returns {verdict_id: latest verdict record} from the verdict ledger(s).

    The JSONL is append-only - last record per id wins. CEO can revise a
    verdict by recording again via scripts/council-record-verdict.py.

    `paths` DEFAULTS to `verdicts_paths()`, the same discovery `main` runs. It
    defaulted to the single legacy ledger until 2026-09-14, which made the
    no-argument call correct only before the move: every other caller of this
    function -- a test, a future reader, the `--json` path if it ever stops
    passing its own list -- got an empty dict on a migrated tree and rendered
    every verdict as pending. A default that is right on one side of a move is
    the shape this seam exists to remove from callers.

    Merging is per ledger first and then across ledgers. WITHIN one file the
    append-only rule stands: the last record for an id wins. ACROSS files it
    does not, and "the later file wins" is what this used to do -- file order
    here is an accident of how far the move has got, so a stale ledger listed
    second silently replaced a newer verdict. `recorded_at`, which
    `council-record-verdict.py` stamps on every record, decides instead.
    """
    per_ledger: list[tuple[Path, dict[str, dict]]] = []
    for path in (verdicts_paths() if paths is None else paths):
        one: dict[str, dict] = {}
        _merge_verdicts(one, Path(path))
        if one:
            per_ledger.append((Path(path), one))
    return _resolve_across_ledgers(per_ledger)


def _recorded_at(rec: dict) -> str:
    """The record's own timestamp, or "". ISO-8601 UTC to the second, so a
    lexicographic comparison is a chronological one; `append()` writes it with
    `datetime.now(timezone.utc).isoformat(timespec="seconds")`."""
    return str(rec.get("recorded_at") or "")


def _resolve_across_ledgers(
        per_ledger: list[tuple[Path, dict[str, dict]]]) -> dict[str, dict]:
    """One verdict per id out of several ledgers, by timestamp, never by order.

    Four cases, in order, and only the last one gives up:

      * one ledger holds the id -- that record.
      * every holder carries `recorded_at` and one is strictly newest -- that
        one. This is the ordinary case, because the writer always stamps.
      * the holders carry the same record byte for byte -- the same record,
        which is what a copied ledger looks like mid-move.
      * otherwise -- a CONFLICT. Two ledgers disagree about one verdict and
        nothing in the data says which is current, so nothing is chosen: the
        entry is flagged, rendered as a conflict, counted as pending, and named
        on stderr. Choosing silently is how an obsolete verdict gets restored
        into a tally the operator reads as a measurement.
    """
    ids = sorted({vid for _p, d in per_ledger for vid in d})
    out: dict[str, dict] = {}
    for vid in ids:
        holders = [(p, d[vid]) for p, d in per_ledger if vid in d]
        if len(holders) == 1:
            out[vid] = holders[0][1]
            continue
        stamps = [_recorded_at(rec) for _p, rec in holders]
        newest = max(stamps)
        if all(stamps) and stamps.count(newest) == 1:
            out[vid] = holders[stamps.index(newest)][1]
            continue
        bodies = {json.dumps(rec, sort_keys=True, ensure_ascii=False)
                  for _p, rec in holders}
        if len(bodies) == 1:
            out[vid] = holders[0][1]
            continue
        out[vid] = _conflicted(vid, holders)
    return out


def _conflicted(vid: str, holders: list[tuple[Path, dict]]) -> dict:
    """The record for a verdict two ledgers disagree about, flagged and announced."""
    where = "; ".join(f"{p} says {rec.get('choice', '?')}" for p, rec in holders)
    print(f"{YELLOW}council-aggregate: {vid} is recorded differently in "
          f"{len(holders)} ledgers and no recorded_at settles it ({where}); "
          f"rendering it as a conflict and counting it pending, rather than "
          f"choosing one{RESET}", file=sys.stderr)
    rec = dict(holders[0][1])
    rec["conflict"] = [{"ledger": str(p), "choice": r.get("choice"),
                        "recorded_at": _recorded_at(r)} for p, r in holders]
    return rec


def _merge_verdicts(out: dict[str, dict], path: Path) -> None:
    """Fold one ledger file into `out`. A later record for an id replaces an
    earlier one, which is the append-only file's own rule carried across files."""
    if not path.exists():
        return
    # Decoded a line at a time, through the shared reader. This was
    # `read_text(encoding="utf-8").splitlines()` with no handler at all, and two
    # defects rode on that one expression -- both permanent, because the ledger
    # is append-only and nothing prunes it. `scripts/utils/jsonl_lines.py`
    # carries the full account and the measurements; the short version is that
    # one non-UTF-8 byte raised out of `main` so NO aggregate was written ever
    # again, and a record holding a raw U+2028 was shredded and dropped in
    # silence. The identical expression sat in `council-record-verdict.py`,
    # which is why the fix is one function and not a third copy.
    try:
        lines = list(jsonl_lines(path))
    except OSError as exc:
        print(f"{YELLOW}council-aggregate: {path} could not be read "
              f"({exc}); reporting 0 verdicts{RESET}", file=sys.stderr)
        return
    for line in lines:
        if line is None:
            # Named, never silent: a dropped verdict changes the tally that
            # feeds the Phase-3b calibration gate.
            print(f"{YELLOW}council-aggregate: skipped an undecodable line in "
                  f"{path}; that verdict is not counted{RESET}",
                  file=sys.stderr)
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        # `json.loads` returns any JSON value, and only a DECODE failure is
        # caught above. A ledger line of `null`, `[]` or `42` parses fine and
        # then raises AttributeError on `.get`, out of `main`, so no aggregate
        # is written -- on every run afterwards, because the ledger is
        # append-only and nothing removes the bad line. MEASURED 2026-08-29.
        # `scripts/council-record-verdict.py` already guards the identical
        # parse of this identical file; the guard reached one of the two
        # readers and not this one.
        if not isinstance(rec, dict):
            continue
        vid = rec.get("verdict_id")
        if vid:
            out[vid] = rec


# ============================================================
# Rendering
# ============================================================
def _link_target(path: Path, base: Path | None) -> str:
    """The href for one transcript, relative to the directory the aggregate is
    written in: the bare filename for a sibling, a relative path for a
    transcript under the other council name.
    """
    if base is None or path.parent == base:
        return path.name
    return os.path.relpath(path, base).replace(os.sep, "/")


def render(transcripts: list[Transcript], verdicts: dict[str, dict],
           searched: list[Path] | None = None,
           base: Path | None = None) -> str:
    """`searched` names the directories the transcripts were looked for in, so
    the empty-corpus line below can say WHERE nothing was found. `base` is the
    directory the rendered file will be written to, for the relative links.
    Both default to None for the callers that only render a corpus they hold."""
    today = datetime.now(get_default_tz()).strftime("%Y-%m-%d")
    lines: list[str] = []
    lines.append("# /council comparative log")
    lines.append("")
    lines.append(f"Last rebuilt: {today}. Transcripts: {len(transcripts)}.")
    lines.append("")
    lines.append("**What this is.** Side-by-side comparison of every `/council` consultation. "
                 "Verdicts come from `scripts/council-record-verdict.py` writing to "
                 "`_verdicts.jsonl`; this file is a READ-ONLY view onto that log. Once >=20 verdicts "
                 "accumulate, the dataset is large enough to calibrate a Gemini-as-judge mode "
                 "(Phase 3b - waiting per CEO decision 2026-05-24).")
    lines.append("")
    lines.append("**How verdicts get recorded.** The `/council` skill asks the CEO at the end of "
                 "every run which answer landed best (claude / gemini / grok / kimi / chatgpt / "
                 "mix / reject). The "
                 "model writes the answer + any free-text note to `_verdicts.jsonl` and rebuilds "
                 "this aggregate. CEO never edits this file or the JSONL directly.")
    lines.append("")

    # Derived from the writer's own vocabulary, never re-typed. A seventh choice
    # spelled here by hand and nowhere else would count nothing, and a choice
    # `council-record-verdict.py` accepts but this dict omits falls into
    # `pending` - so the Pending number, which feeds the Phase-3b calibration
    # gate this file's header quotes, would silently over-report. `chatgpt`
    # joined VALID_CHOICES on 2026-09-11 and reached this tally without an edit.
    counts = dict.fromkeys(VERDICT_CHOICES, 0)
    pending = 0
    # A CONFLICTED verdict is counted pending, never as its first holder's
    # choice. The tally feeds the Phase-3b calibration gate this file's header
    # quotes, and a verdict two ledgers disagree about has not been decided;
    # counting it would be the silent choice `_resolve_across_ledgers` refuses
    # to make, arriving one function later.
    for t in transcripts:
        rec = verdicts.get(t.path.stem)
        if rec and not rec.get("conflict") and rec.get("choice") in counts:
            counts[rec["choice"]] += 1
        else:
            pending += 1

    # Rendered from the same mapping that was counted. The six labels were a
    # third hand-written spelling of the vocabulary, so a choice that reached
    # `counts` but not this f-string was tallied and then not shown, and the
    # parts stopped summing to the whole with nothing saying so.
    tally = ", ".join(f"{_LABELS.get(c, c.capitalize())}={counts[c]}"
                      for c in VERDICT_CHOICES)
    lines.append(f"**Tally so far:** {tally}, "
                 f"Pending={pending} of {len(transcripts)}.")
    lines.append("")
    lines.append("---")
    lines.append("")

    for t in transcripts:
        rec = verdicts.get(t.path.stem)
        models = t.models_present()
        lines.append(f"## {t.timestamp} - {t.topic}")
        lines.append("")
        lines.append(f"- File: [{t.path.name}]({_link_target(t.path, base)})")
        lines.append(f"- Mode: {t.mode}. Models: {', '.join(models) if models else 'none parsed'}.")
        lines.append("")
        lines.append(f"**Question:** {t.question_snippet or '_(no question section parsed)_'}")
        lines.append("")
        if t.claude_snippet:
            lines.append(f"**Claude:** {t.claude_snippet}")
            lines.append("")
        if t.gemini_snippet:
            lines.append(f"**Gemini:** {t.gemini_snippet}")
            lines.append("")
        if t.grok_snippet:
            lines.append(f"**Grok:** {t.grok_snippet}")
            lines.append("")
        if t.kimi_snippet:
            lines.append(f"**Kimi:** {t.kimi_snippet}")
            lines.append("")
        if t.chatgpt_snippet:
            lines.append(f"**ChatGPT:** {t.chatgpt_snippet}")
            lines.append("")
        if rec and rec.get("conflict"):
            # Visible in the artifact, not only on a stderr line nobody keeps.
            said = "; ".join(f"`{c.get('choice') or '?'}` in {c['ledger']}"
                             for c in rec["conflict"])
            lines.append(f"**CEO verdict:** CONFLICT - {said}. Counted as "
                         "pending until one ledger wins.")
        elif rec:
            choice = rec.get("choice", "?").upper()
            notes = rec.get("notes", "").strip()
            recorded_at = rec.get("recorded_at", "")
            verdict_line = f"**CEO verdict:** {choice}"
            if notes:
                verdict_line += f" - _{notes}_"
            if recorded_at:
                verdict_line += f"  ({recorded_at})"
            lines.append(verdict_line)
        else:
            lines.append("_(pending CEO verdict)_")
        lines.append("")
        lines.append("---")
        lines.append("")

    if not transcripts:
        # The directories are RESOLVED, never a literal. A hard-coded
        # pre-move council path here would name the pre-migration home
        # whatever the seam answered, so the one line a reader gets when the
        # corpus is empty would send them to look in the wrong place.
        where = ", ".join(f"`{d}`" for d in (searched or transcript_dirs()))
        lines.append(f"_No `/council` transcripts found yet in {where}. "
                     "Run `/council` to start populating this aggregate._")
        lines.append("")

    lines.append("_Generated by `scripts/council-aggregate.py` from `_verdicts.jsonl`. "
                 "To record or revise a verdict, use `scripts/council-record-verdict.py` "
                 "(the /council skill calls it automatically at end of each run)._")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate /council transcripts.")
    parser.add_argument("--json", action="store_true", help="Print parsed transcripts as JSON.")
    args = parser.parse_args(argv)

    # Resolved ONCE. Every helper below takes the set rather than asking the
    # seam again: after the move `read_records` rglobs the whole target tree,
    # and `("trace", "scratch")` is the widest kind in the collection.
    # Every suffix, not `.md`: the ledger is resolved as a record of its own
    # now (`verdicts_paths()` source 1), so one resolution answers both
    # questions and `collect_transcripts` filters the markdown out of it.
    records = council_records(suffixes=None)
    transcripts = collect_transcripts(records)
    dirs = transcript_dirs(records)
    ledgers = verdicts_paths(dirs, records)
    verdicts = load_verdicts(ledgers)
    print(f"{CYAN}Parsed {len(transcripts)} transcripts, {len(verdicts)} verdicts from "
          f"{', '.join(str(d) for d in dirs)}{RESET}", file=sys.stderr)

    if args.json:
        out = []
        for t in transcripts:
            rec = verdicts.get(t.path.stem)
            out.append({
                "path": display_path(t.path),
                "timestamp": t.timestamp,
                "mode": t.mode,
                "topic": t.topic,
                "models_present": t.models_present(),
                "question_snippet": t.question_snippet,
                "claude_snippet": t.claude_snippet,
                "gemini_snippet": t.gemini_snippet,
                "grok_snippet": t.grok_snippet,
                "kimi_snippet": t.kimi_snippet,
                "chatgpt_snippet": t.chatgpt_snippet,
                "verdict": rec,
            })
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    out = aggregate_path()
    md = render(transcripts, verdicts, dirs, out.parent)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"{GREEN}Aggregate written: {out}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
