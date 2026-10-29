#!/usr/bin/env python3
"""review-branch.py - review a yard branch before HELM merges it.

HELM runs this on a finished yard branch, before `git merge --no-ff
--no-commit`. It sends the branch's diff against `main`, with the work's stated
intent, to one reviewer configuration from `scripts/utils/diff_review.py`, one
call per part when the diff is split at file boundaries to fit the size limit
(code first, prose last), and prints a summary that opens with the count: how many findings need fixing
before the merge, or, plainly, that nothing does. Every finding carries a file
and a line, a bug or security finding blocks, and nits are capped.

A second run on the same branch reviews only what changed since the head the
last round saw, and tells the reviewer what was already reported so the same
finding is not raised twice; a finding that repeats one from an earlier round
anyway is dropped and counted. `--full` reviews the whole branch again.

The target is a branch name or, to review work already merged, the merge
commit: its first parent is the base and its second the branch head. A branch
name is always a branch, even when its tip is a merge of `main` into it.

A blocking finding stays open until a later round's reviewer names it fixed in
the diff it saw, or a `--full` round, which reviews the whole branch afresh, no
longer raises it; a clean incremental round does not clear it. A round is
partial only when a single file was longer than the size limit and was cut: it
is never reused, never the base of the next incremental round, and with nothing
blocking it exits 3.

The stated intent is `--intent-file` (the brief or the master instruction the
yard executed) followed by the branch's own commit messages; without the file,
the commit messages alone.

Rounds are records, kept in the data repository under the registry name
`branch-reviews`, one JSON per branch. Every model call goes through the proxy
to a model of the named trust list. Nothing here is scheduled and nothing
merges: it runs when HELM types it and only reports.

Usage:
  .venv/bin/python scripts/review-branch.py <branch> [--intent-file BRIEF.md]
  .venv/bin/python scripts/review-branch.py <merge-sha>
  .venv/bin/python scripts/review-branch.py <branch> --full --config opus-rules

Exit codes: 0 nothing blocks the merge; 1 bug or security findings, this
round's or still open from an earlier one; 2 usage error; 3 the review did not
happen or did not cover the whole change (the reviewer failed, answered off
contract, or a single file longer than the size limit was cut), which is never
a clean result.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import data_path, data_write_dir  # noqa: E402
from scripts.utils.diff_review import (  # noqa: E402
    BLOCKING, CONFIGS, Review, git, numbered_parts, raw_diff, review, summary_lines,
)
from scripts.utils.workspace import get_workspace_root  # noqa: E402

ROOT = get_workspace_root()
# The configuration with the highest located recall in the first eval
# (MEASURED 2026-10-18, 23 defects and 20 clean ranges: astra-rules 5/23 located
# and 22 false reports, sonnet-plain 4/23 and 12, opus-rules 4/23 and 14,
# sonnet-rules 1/23 and 13). At that size the gaps are inside the noise, so the
# choice is provisional; re-run review-eval before changing it.
DEFAULT_CONFIG = "astra-rules"


def rev(ref: str) -> str | None:
    try:
        return git(ROOT, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip() or None
    except RuntimeError:
        return None


def resolve(target: str, base: str) -> tuple[str, str, str]:
    """(base sha, head sha, store key) for a branch or a merge commit."""
    head = rev(target)
    if not head:
        raise SystemExit(f"{RED}No commit or branch named {target!r}.{RESET}")
    try:
        ref = git(ROOT, "rev-parse", "--symbolic-full-name", target).strip()
    except RuntimeError:
        ref = ""
    is_branch = ref.startswith(("refs/heads/", "refs/remotes/"))
    second = None if is_branch else rev(f"{head}^2")
    if second:
        return rev(f"{head}^1"), second, f"merge-{head[:12]}"
    base_sha = rev(base)
    if not base_sha:
        raise SystemExit(f"{RED}No base {base!r}.{RESET}")
    mb = git(ROOT, "merge-base", base_sha, head).strip()
    return mb, head, re.sub(r"[^A-Za-z0-9._-]+", "-", target)


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", s.lower()))


def repeats(f: dict, earlier: list[dict]) -> bool:
    """Same file and a title sharing most of its words with one already reported."""
    w = _words(f["title"])
    for e in earlier:
        if e["file"] != f["file"]:
            continue
        ew = _words(e["title"])
        if w and ew and len(w & ew) / len(w | ew) >= 0.6:
            return True
    return False


def fkey(f: dict) -> str:
    return f"{f['file']}:{f['line']}:{f['title']}"


def open_blockers(rounds: list[dict]) -> list[dict]:
    """Blocking findings no later round resolved; a complete whole-branch round starts afresh."""
    found: list[dict] = []
    for r in rounds:
        if r.get("error"):
            continue
        if r.get("scope") == "whole branch" and not r.get("truncated"):
            found = []
        gone = set(r.get("resolved", []))
        found = [f for f in found if fkey(f) not in gone]
        found += [f for f in r.get("findings", []) if f["severity"] in BLOCKING]
    return found


def print_open(carried: list[dict]) -> None:
    if carried:
        print(f"{RED}{len(carried)} blocking finding(s) from earlier rounds still open "
              f"(no round has seen them fixed; --full reviews the whole branch afresh):{RESET}")
        for ln in summary_lines(carried)[1:]:
            print(ln)


def merge_parts(reviews: list[Review], part_files: list[list[str]], already: list[dict] | None) -> Review:
    """One Review from the parts' reviews, in part order; the last one's error, if any, stops the round.

    A resolved id `R<i>` counts only from a part that rendered the file of
    `already[i-1]`: a part that never saw the file cannot vouch that it was fixed.
    """
    if reviews[-1].error:
        return Review([], "", sum(p.seconds for p in reviews), sum(p.prompt_chars for p in reviews),
                      sum(p.answer_chars for p in reviews), error=reviews[-1].error, dropped_nits=0,
                      raw=reviews[-1].raw, extra={"resolved": []})
    resolved: set[str] = set()
    for p, seen in zip(reviews, part_files, strict=True):
        for x in p.extra.get("resolved", []):
            m = re.fullmatch(r"R(\d{1,9})", x)
            i = int(m.group(1)) if m else 0
            if already and 1 <= i <= len(already) and already[i - 1].get("file") in seen:
                resolved.add(x)
    return Review([f for p in reviews for f in p.findings], " | ".join(p.summary for p in reviews),
                  sum(p.seconds for p in reviews), sum(p.prompt_chars for p in reviews),
                  sum(p.answer_chars for p in reviews), error=None,
                  dropped_nits=sum(p.dropped_nits for p in reviews), raw="\n".join(p.raw for p in reviews),
                  extra={"resolved": sorted(resolved)})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="a branch name, or a merge commit to review work already merged")
    ap.add_argument("--base", default="main")
    ap.add_argument("--intent-file", type=Path, help="the brief or master instruction the work executed")
    ap.add_argument("--config", default=DEFAULT_CONFIG, choices=sorted(CONFIGS))
    ap.add_argument("--full", action="store_true", help="review the whole branch even if a round exists")
    args = ap.parse_args()

    base, head, key = resolve(args.target, args.base)
    store = data_path("branch-reviews") / f"{key}.json"
    state = json.loads(store.read_text()) if store.exists() else {"target": args.target, "rounds": []}
    rounds = state["rounds"]
    last = rounds[-1] if rounds else None
    earlier = [f for r in rounds for f in r.get("findings", [])]

    complete = bool(last) and not last.get("error") and not last.get("truncated")
    if complete and last.get("head") == head and not args.full:
        print(f"{BOLD}Already reviewed at {head[:12]} (round {len(rounds)}); nothing new to review.{RESET}")
        for ln in summary_lines(last["findings"], last.get("dropped_nits", 0)):
            print(ln)
        carried = [f for f in open_blockers(rounds) if f not in last["findings"]]
        print_open(carried)
        return 1 if open_blockers(rounds) else 0

    since = None
    if complete and not args.full and last.get("head"):
        since = last["head"]
    diff_base = since or base
    parts, files, truncated, part_files = numbered_parts(raw_diff(ROOT, diff_base, head, prose_context=3))
    if not files:
        print(f"{YELLOW}No reviewable change between {diff_base[:12]} and {head[:12]}.{RESET}")
        print_open(open_blockers(rounds))
        return 1 if open_blockers(rounds) else 0

    intent = ""
    if args.intent_file:
        intent = args.intent_file.read_text()
    log = git(ROOT, "log", "--no-merges", "--format=%B%n---", f"{base}..{head}")
    intent = (intent + "\n\nBranch commit messages:\n" + log).strip()

    cfg = CONFIGS[args.config]
    # "Already reported" is what is still open plus the earlier nits; a finding
    # resolved since is left out, so the same defect brought back is raised again.
    already = open_blockers(rounds) + [f for f in earlier if f["severity"] not in BLOCKING] if since else None
    done: list[Review] = []
    for part in parts:
        done.append(review(cfg, part, intent, already=already))
        if done[-1].error:
            break
    r = merge_parts(done, part_files, already)
    dropped_repeats = 0
    resolved: list[str] = []
    if since and not r.error:
        ids = set(r.extra.get("resolved", []))
        resolved = [fkey(f) for i, f in enumerate(already, 1) if f"R{i}" in ids and f["severity"] in BLOCKING]
        kept = [f for f in r.findings if not repeats(f, already)]
        dropped_repeats = len(r.findings) - len(kept)
        r.findings = kept

    scope = f"since round {len(rounds)} ({since[:12]})" if since else "whole branch"
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base": base, "head": head,
             "diff_base": diff_base, "scope": scope, "config": cfg.name, "model": cfg.model, "files": files,
             "truncated": truncated, "parts": len(parts), "seconds": round(r.seconds, 1), "findings": r.findings,
             "dropped_nits": r.dropped_nits, "dropped_repeats": dropped_repeats, "resolved": resolved,
             "summary": r.summary, "error": r.error}
    rounds.append(entry)
    out = data_write_dir("branch-reviews") / f"{key}.json"
    out.write_text(json.dumps(state, indent=1) + "\n")

    print(f"{BOLD}Review of {args.target}{RESET}: {base[:12]}..{head[:12]}, {scope}, {len(files)} file(s), "
          f"{cfg.name} ({cfg.model}), {r.seconds:.0f}s, round {len(rounds)}, parts: {len(parts)}")
    if r.error:
        print(f"{RED}The review did not happen: {r.error}. This is not a clean result.{RESET}")
        print(f"Recorded: {out}")
        return 3
    carried = [f for f in open_blockers(rounds) if f not in r.findings]
    lines = summary_lines(r.findings, r.dropped_nits)
    colour = RED if r.blocking or carried else GREEN
    print(colour + lines[0] + RESET)
    for ln in lines[1:]:
        print(ln)
    if dropped_repeats:
        print(f"({dropped_repeats} finding(s) repeating an earlier round dropped)")
    if resolved:
        print(f"({len(resolved)} earlier finding(s) named fixed by this diff)")
    print_open(carried)
    if truncated:
        print(f"{YELLOW}PARTIAL: a single file was longer than the size limit and was cut; the rest of that "
              f"file was not reviewed. This is not a clean result, and the round is not reused.{RESET}")
    print(f"Recorded: {out}")
    if r.findings or carried:
        # The yard that receives these answers each one as FIX, CONTEST (with
        # evidence the reviewer could read) or ESCALATE, never "fix later".
        print("Triage for the yard: FIX / CONTEST / ESCALATE, "
              ".claude/skills/goldfish/references/triage.md")
    if r.blocking or carried:
        return 1
    return 3 if truncated else 0


if __name__ == "__main__":
    sys.exit(main())
