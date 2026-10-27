#!/usr/bin/env python3
"""Advisory audit: skill bash blocks passing bare data-class paths to scripts.

Engine/data separation (CLAUDE.md): every data artifact must resolve under the DATA
root via `data_path()` and a registry name. The PreToolUse data-path-redirect hook rewrites
`@outputs/...` for Read/Write/Edit/Grep/Glob tool ops, but NOT for Bash. So a SKILL (leak-guard: ok, scanner vocabulary)
that hands a bare `outputs/...` path to a Bash-invoked script (cwd = engine root) can (leak-guard: ok, scanner vocabulary)
misroute a write into the engine clone: the class the operator's auto-memory
flags under the rule that a skill's data paths need explicit resolution.

This is ADVISORY, not the guarantee. The authoritative, how-agnostic guarantee is
`tests/test_engine_tree_clean.py` (any data artifact landing in the engine clone fails,
regardless of how the write happened). This scanner is the earlier, narrower signal:
it surfaces SKILL bash lines that *could* misroute, so they can be reviewed.

The gate is a BASELINE RATCHET: it fails only when a SKILL gains a NEW bare-data-path
bash line beyond the frozen baseline, catching regressions without forcing churn on
examples that were reviewed and accepted.

A hit carrying a placeholder (YYYY-MM-DD, {sender-slug}) is NOT thereby illustrative.
The baseline was first frozen on that reasoning and it was wrong: nine of its ten
entries were live commands the skill instructs the agent to run, placeholder and all.
Triage a new hit by asking whether the line is executed, never by what it spells.

Since 2026-09-14 the data-name alternation also covers the nine HEADING OS 2
family roots (canon, ledger, status, research, work, inbound, build, trace,
asset), derived from `families.family_roots()` rather than typed. The records
under `outputs` and its siblings are moving to `<family>/<leaf>/<their old
path>`, and a bare `work/deliverable/...` handed to a Bash-invoked script (leak-guard: ok, scanner vocabulary)
misroutes into the engine clone the same way a bare `outputs/...` does. (leak-guard: ok, scanner vocabulary)
MEASURED that day over the 201-file corpus: 0 new hits, so `BASELINE` stays
empty and no entry was re-frozen to buy quiet.

The corpus is every markdown file a skill ships, SKILL.md and `references/` alike,
because a reference file carries runnable bash the same way. It read only SKILL.md
until 2026-09-02; see `skill_files`.

Usage:
  python scripts/audit-skill-bash-paths.py            # list all candidates
  python scripts/audit-skill-bash-paths.py --check    # exit 1 if any skill exceeds baseline
  python scripts/audit-skill-bash-paths.py --json      # machine-readable

Exit codes: 0 clean, 1 a skill is over baseline (--check only), 2 the audit did not
read the corpus it covers and refuses to report a verdict at all.
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.workspace import get_workspace_root  # noqa: E402
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET  # noqa: E402
from scripts.utils.repo_files import read_sources  # noqa: E402
from scripts.heading_os_2.families import (  # noqa: E402
    NOT_A_RESOURCE_NAME, family_roots, subject_roots,
)
from scripts.utils.data_path import addresses_in  # noqa: E402

# Data-class top-level dirs. No other list mirrors this one (corrected
# 2026-09-14): `tests/test_engine_tree_clean.py` filters by routing destination.
#
# Widened 2026-09-14 with the nine HEADING OS 2 family roots, DERIVED from
# `families.family_roots()` and never typed. A skill handing Bash a bare
# `work/deliverable/...` misroutes into the engine clone exactly as a bare  # leak-guard: ok (scanner vocabulary)
# `outputs/...` does, and this scanner could not see it. The nine C3  # leak-guard: ok (scanner vocabulary)
# subject-tree roots joined 2026-10-13, from `families.subject_roots()`.
_LEGACY_DATA_NAMES = ("outputs", "crm", "knowledge", "threads", "plans",
                      "datastore", "auto-memory")
_DATA = re.compile(
    r"\b(" + "|".join(re.escape(n)
                      for n in (*_LEGACY_DATA_NAMES, *family_roots(),
                                *subject_roots())) + r")/" + NOT_A_RESOURCE_NAME
)
# Tokens that prove the path was resolved through the seam (not a bare literal).
#
# `scripts/data-path.py` joined the list on 2026-09-19 and is deliberately
# accepted ONLY in the command-substitution form, `$(... data-path.py ...)`,
# the exact analogue of the `$(.*get_` clause beside it. That script is the
# shell entry point to the HOS-5 front door, and its own docstring names THIS
# audit as the reason it exists -- so an audit that could not see it was the
# one gate its own remedy failed.
#
# Narrow on purpose. Every token here gags the WHOLE line, so a bare
# `python scripts/data-path.py path crm-config > outputs/x.md` must stay a hit:  # leak-guard: ok (scanner vocabulary)
# the address on the left is resolved and the `outputs/` on the right is not,
# and forgiving the line would forgive the half that is still a misroute. The
# substitution form is what actually carries the answer into another command.
_RESOLVED = re.compile(
    r"get_\w+_dir|get_data_root|\$OUTPUTS_DIR|\$DATA_ROOT|\$\(.*get_"
    r"|\$\(.*data-path\.py")

# Options that designate a destination path, so a command carrying one is writing
# somewhere even when it invokes no script this scanner recognises.
#
# Enumerated from the corpus, not guessed. Counting the flags that appear inside
# bash-labelled fences across all 94 skills on 2026-08-30 gives: -o (10),
# --output (8), --out (5), --file (4), --out-dir (2), -f (2), --output-dir (2),
# --path (1). All eight are covered, and the list holds nothing else -- a flag
# with no use in the corpus would make the sentence above false, so `--dest` was
# dropped after the count came back zero for it.
#
# Deliberately NOT covered, and why: -c (29 uses, every one of them `python3 -c`,
# already matched by the `python \S` alternative), and -m, -C, -u, -v, -A, -p,
# which designate no destination. Adding a short flag costs precision, because a
# single letter collides across tools; each of these was read in context first.
# -f is in because both of its uses take a path, though `-f` means "force" in
# other tools -- the conjunction with `_DATA` and `not _RESOLVED` is what keeps
# that affordable.
#
# Longest-first, because Python alternation is leftmost-FIRST rather than
# leftmost-longest. The trailing lookahead happens to save the ordering today,
# but relying on that couples the tuple's order to a regex detail two lines
# below it.
_DEST_OPTS = ("--output-dir", "--out-dir", "--output", "--file", "--path",
              "--out", "-o", "-f")
# A bash command only counts if it invokes a script, redirects, or names a
# destination. `(?<!\S)` stops `-o` matching inside `--output`; `(?=[=\s]|$)`
# stops `--out` matching the front of `--output`.
_COMMAND = re.compile(
    r"python \S|scripts/|>\s|(?<!\S)(?:"
    + "|".join(re.escape(o) for o in _DEST_OPTS)
    + r")(?=[=\s]|$)"
)

# Frozen baseline (skill -> count). EMPTY since 2026-08-31 and frozen empty: any
# bare data path in any skill's bash fails `--check`. Never add a key back to buy
# quiet; a frozen entry is a defect with a note on it. A placeholder in a path
# (YYYY-MM-DD, {sender-slug}) does not make a line illustrative.
BASELINE: dict[str, int] = {}


# No empty string: an UNLABELLED fence holds output samples, diagrams and diffs,
# not commands, and scanning it failed --check on non-commands (narrowed
# 2026-08-30; tests/test_engine_tree_clean.py stays the authoritative guarantee).
_BASH_FENCES = ("bash", "sh", "shell")


def fence_language(stripped: str) -> str:
    """The lowered LANGUAGE of a fence line, which is the first token only.

    A fence is an opening delimiter plus an info string, and everything after
    the first token is metadata for whatever renders the block. This read
    ``stripped.strip("`").lower()`` and compared the WHOLE info string against
    `_BASH_FENCES`, so ```` ```bash linenos ```` produced `"bash linenos"`,
    matched nothing, and the scanner walked past every command in the block.

    MEASURED 2026-09-02 on one line, `python scripts/x.py -o outputs/a.png`: (leak-guard: ok, history)
    one hit inside ```` ```bash ````, zero inside ```` ```bash linenos ````.
    The command is identical; only the fence metadata differed. That is the one
    input that can silence this ratchet without tripping its coverage floor,
    since `Coverage.refusals` counts files OPENED and a file whose fences are
    all mislabelled is opened, read, and scored zero.

    The narrowing that dropped the empty string from `_BASH_FENCES` is
    untouched: an unlabelled fence still yields `""`, which is in no tuple.
    Taking the first token is deliberately not a prefix test, so `bashful` and
    `pythonbash` stay out.
    """
    info = stripped.strip("`").split()
    return info[0].lower() if info else ""


def scan_skill(path: Path) -> list[tuple[int, str]] | None:
    """Return (lineno, command) candidates inside bash-LABELLED fences.

    `None` rather than `[]` when the file could not be read, so a caller can tell
    "opened it, found nothing" from "never opened it". The two used to be the same
    empty list, which is the shape that lets a corpus quietly shrink to nothing and
    still report a clean tree.

    Backslash continuations are joined into one logical command before matching,
    and the line number reported is the line the command STARTS on.

    That join is the whole point: matched per physical line, a command whose
    destination sits on a continuation line splits into two halves that each
    miss (fixed 2026-08-30).

    The harm this detects is misrouting, not leaking. `.gitignore` carries
    `/outputs/`, so an engine-tree `outputs` file cannot be pushed -- and for
    the same reason `tests/test_engine_tree_clean.py` and the push wall both
    walk past it. The artifact lands in the wrong tree and nothing else notices.
    """
    hits: list[tuple[int, str]] = []
    in_block = False
    cur_bash = False
    pending: str | None = None
    pending_line = 0
    # `errors="replace"`: this runs over every SKILL.md in the tree and drives a
    # `--check` gate, so one stray non-UTF-8 byte in any one of them used to
    # raise UnicodeDecodeError - a ValueError, caught nowhere on the path - and
    # take the whole audit down naming no file. A replaced byte cannot invent or
    # conceal a `scripts/...` path, which is all this scanner matches on.
    #
    # Through `read_sources`, and SKIPPING is the right answer here rather than
    # failing. `skill_files` walks the LIVE tree (see its docstring), several
    # agents share one checkout, and a SKILL.md created and deleted inside the
    # window between that glob and this read raised FileNotFoundError from
    # inside a gate that had found nothing wrong. A skill that is not on disk
    # cannot be over its baseline, so dropping it narrows the corpus without
    # changing any verdict this scanner reaches -- and `read_sources` WARNS
    # naming the file, so the narrowing is stated rather than hidden. `list()`
    # rather than a `for`-with-`return`: the warning fires when the generator is
    # exhausted, and an abandoned generator never warns.
    sources = list(read_sources([path], errors="replace"))
    if not sources:
        return None
    text = sources[0][1]
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_block:
                cur_bash = fence_language(stripped) in _BASH_FENCES
            in_block = not in_block
            pending = None  # a fence never continues across its own boundary
            continue
        if not (in_block and cur_bash):
            continue
        if pending is None:
            command, start = stripped, i
        else:
            command, start = f"{pending} {stripped}", pending_line
        if stripped.endswith("\\"):
            pending, pending_line = command.rstrip("\\").rstrip(), start
            continue
        pending = None
        # The logical addresses come out FIRST, blanked in place, so `_DATA`
        # never sees the family root inside one. `record:trace/report` is a
        # NAME the front door answers; read as a path it is a bare `trace/`
        # and this audit refuses the one command that does the right thing.
        # See `addresses_in` for the measurement.
        _, scanned = addresses_in(command)
        if _COMMAND.search(scanned) and _DATA.search(scanned):
            if not _RESOLVED.search(scanned):
                hits.append((start, command))
    return hits


def skill_files(root: Path) -> list[Path]:
    """Every markdown file a skill ships, as its own assertable value.

    Its own value so the corpus is measurable: with `BASELINE` empty, a clean
    result over ZERO files looks like a clean tree, and the `Coverage` floor
    below is what refuses that.

    RECURSIVE since 2026-09-02, because a `references/` file carries runnable
    bash exactly as its SKILL.md does, and the one-level glob never opened one.

    The LIVE tree on purpose, not `git ls-files`: an unstaged new skill is the
    file most likely to carry a misroute, and "a new skill appearing" is one of
    the two regressions this gate exists to catch. Rooting the walk at
    `.claude/skills/` rather than `.claude/` keeps an agent worktree under
    `.claude/worktrees/` out of it, which is what the one-level glob used to buy.
    """
    return sorted((root / ".claude" / "skills").rglob("*.md"))


def skill_name(path: Path, root: Path) -> str:
    """The skill a file belongs to: the first path component under `.claude/skills/`.

    `path.parent.name` was right only while the corpus was one level deep. Over the
    widened corpus it keys every reference file as `references`, collapsing hits
    from unrelated skills into one bucket and making the per-skill ratchet report a
    skill name that does not exist.
    """
    return path.relative_to(root / ".claude" / "skills").parts[0]


def walk_markdown(root: Path) -> set[Path]:
    """Every `*.md` under `.claude/skills/`, walked WITHOUT `skill_files`'s glob.

    Deliberately a second implementation of the same question, and that duplication
    is the whole value: it is the number `skill_files` is checked against, so a glob
    that narrows (the defect this file was fixed for) shows up as a shortfall
    instead of shrinking the yardstick along with the measurement.
    """
    found: set[Path] = set()
    for dirpath, _dirnames, filenames in os.walk(root / ".claude" / "skills"):
        for name in filenames:
            if name.endswith(".md"):
                found.add(Path(dirpath) / name)
    return found


@dataclass
class Coverage:
    """What the audit actually opened, so a caller can refuse a pass over nothing.

    `tree` is the independent walk; `corpus` is what `skill_files` enumerated;
    `opened` is what was really read. The floor compares the first against the
    last, so it is stated in terms of the tree in front of it rather than an
    absolute count. An absolute floor was the first attempt and it was wrong in
    both directions: it refused a legitimate one-skill scratch tree (which was how
    the check that once exercised the FAIL branch reached it), and on a downstream
    fork carrying twenty skills it would refuse every run forever. That check went
    with the 2026-09-21 test-suite split, so the FAIL branch is now exercised by
    nobody and the honest-case behaviour above rests on review.
    """

    tree: set[Path] = field(default_factory=set)
    corpus: list[Path] = field(default_factory=list)
    opened: list[Path] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)

    @staticmethod
    def _classify(path: Path) -> str | None:
        if path.name == "SKILL.md":
            return "SKILL.md"
        if path.parent.name == "references":
            return "references"
        return None

    def missed(self) -> list[Path]:
        """Tree files never opened, excluding anything no longer on disk.

        The exclusion is the documented mid-walk race, not a loophole: several
        agents share one checkout, and a file created and deleted inside the run
        window is not a surface this audit failed to cover. A file still sitting
        there unopened is.
        """
        opened = {p.resolve() for p in self.opened}
        return sorted(p for p in self.tree
                      if p.resolve() not in opened and p.exists())

    def refusals(self) -> list[str]:
        """Every reason this run must not report a pass, each naming both numbers."""
        out: list[str] = []
        if not self.opened:
            out.append(
                f"opened 0 files of the {len(self.tree)} markdown file(s) under "
                ".claude/skills/; a pass over an empty corpus is not a pass")
            return out
        missed = self.missed()
        if missed:
            shown = ", ".join(str(p) for p in missed[:5])
            out.append(
                f"opened {len(self.opened)} of the {len(self.tree)} markdown "
                f"file(s) the skills tree holds; {len(missed)} never read, "
                f"e.g. {shown}")
        # Per class as well as over the union, because a union shortfall can be
        # small while one whole surface is gone: 94 SKILL.md out of 201 is a
        # 53-percent union figure and a 0-percent references figure, and it is the
        # second number that names the defect.
        for label in ("SKILL.md", "references"):
            in_tree = sum(1 for p in self.tree if self._classify(p) == label)
            was_read = sum(1 for p in self.opened if self._classify(p) == label)
            if was_read < in_tree and any(self._classify(p) == label for p in missed):
                out.append(
                    f"opened {was_read} of the {in_tree} {label} file(s) the tree "
                    "holds; the corpus stopped covering that surface")
        return out


def scan_corpus(root: Path) -> tuple[dict[str, list[tuple[str, int, str]]], Coverage]:
    """Scan every file in the corpus; return the hits per skill and the coverage.

    The independent walk is taken twice, bracketing the scan, and only their
    INTERSECTION becomes the yardstick. A file another agent creates or deletes
    while this runs appears in one walk and not the other, so it can neither
    manufacture a shortfall nor hide one.
    """
    before = walk_markdown(root)
    out: dict[str, list[tuple[str, int, str]]] = {}
    coverage = Coverage(corpus=skill_files(root))
    for path in coverage.corpus:
        hits = scan_skill(path)
        if hits is None:
            coverage.unreadable.append(path)
            continue
        coverage.opened.append(path)
        if hits:
            rel = path.relative_to(root).as_posix()
            out.setdefault(skill_name(path, root), []).extend(
                (rel, ln, text) for ln, text in hits)
    coverage.tree = before & walk_markdown(root)
    return out, coverage


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if a skill exceeds its baseline")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    root = get_workspace_root()
    found, coverage = scan_corpus(root)
    counts = {name: len(h) for name, h in found.items()}

    # Refuse BEFORE reporting. A findings table drawn over a corpus that was never
    # read is the misleading artifact, not the exit code: `--json` printed
    # `{"counts": {}}` and the console printed a clean list, both indistinguishable
    # from a tree with nothing wrong in it. Exit 2, as scripts/ste-check.py and
    # scripts/validate-crm-schema.py do, so a setup failure is never a findings
    # verdict.
    refusals = coverage.refusals()
    if refusals:
        print(f"{RED}{BOLD}REFUSING{RESET} -- the skill bash data-path audit did not "
              f"read the corpus it claims to cover:", file=sys.stderr)
        for r in refusals:
            print(f"  {RED}{r}{RESET}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"counts": counts, "baseline": BASELINE,
                          "scanned": len(coverage.opened)}, indent=2))
    else:
        print(f"{BOLD}{CYAN}Skill bash data-path audit (advisory){RESET} "
              f"{GRAY}{len(coverage.opened)} markdown files{RESET}")
        for name, hits in found.items():
            base = BASELINE.get(name, 0)
            tag = f"{GREEN}=baseline{RESET}" if len(hits) <= base else f"{RED}OVER baseline ({base}){RESET}"
            print(f"\n{BOLD}{name}{RESET} [{len(hits)}] {tag}")
            for rel, ln, text in hits:
                print(f"  {GRAY}{rel}:{ln}:{RESET} {text[:110]}")
        print(f"\n{GRAY}Authoritative guarantee: tests/test_engine_tree_clean.py. This is advisory.{RESET}")

    # Regression check: any skill over baseline, or a new skill not in baseline.
    regressions = []
    for name, n in counts.items():
        base = BASELINE.get(name)
        if base is None:
            regressions.append(f"{name}: NEW skill with {n} bare-data-path bash line(s) (not in baseline)")
        elif n > base:
            regressions.append(f"{name}: {n} > baseline {base}")

    if args.check:
        if regressions:
            print(f"\n{RED}{BOLD}FAIL{RESET} -- new SKILL bash data-path misroute candidate(s):", file=sys.stderr)
            for r in regressions:
                print(f"  {RED}{r}{RESET}", file=sys.stderr)
            print(f"{YELLOW}Resolve it: in Python through data_path('<name>'), in bash through "
                  f'"$(python scripts/data-path.py path <name>)". BASELINE is frozen empty '
                  f"and adding a key back to buy quiet is forbidden (see its note).{RESET}",
                  file=sys.stderr)
            return 1
        # stderr, like the FAIL path two lines up. On stdout it landed AFTER the
        # --json document, so `--json --check | json.tool` died on trailing text.
        print(f"\n{GREEN}OK{RESET} -- no SKILL bash data-path regressions vs baseline.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
