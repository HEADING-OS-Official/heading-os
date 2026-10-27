#!/usr/bin/env python3
"""review-eval.py - score a code-reviewer configuration on defects this engine really shipped.

The defect set is built from the engine's own history. Every fix that shipped
without a test carries a `Fix-Note:` line (Phase 5 of
`.claude/rules/development-standards.md`), so the history already says, per
fix, what broke. `harvest` keeps the fixes whose defect can be placed on lines
of code, blames those lines on the fix's parent to find the commit that
introduced them, and stores one case per defect: the introducing commit, the
file and the line region in that commit's version, and the Fix-Note text. It
also stores known-clean ranges: commits whose added code lines all still stand
unchanged at the head of `main`, which no fix ever touched.

`run` gives each introducing commit's diff, with its own commit message as the
stated intent, to one reviewer configuration (`scripts/utils/diff_review.py`
`CONFIGS`) and scores it: a defect is FOUND when a finding names its file and a
line within `TOLERANCE` lines of its region, and, with `--judge`, when a second
model also agrees the finding describes the Fix-Note's defect. On the clean
ranges every bug or security finding counts as a false report. That count is an
upper bound on noise: a line nobody changed can still be wrong.

`report` compares the runs: recall, false reports on clean ranges, time and
estimated tokens per configuration.

The mechanical filters cannot tell a code defect from a note about a new
requirement, a recorded number or drift in the environment, so a reviewed list
of exclusions, `curation.json` beside the cases (fix commit prefix -> reason),
is applied after them and counted under "curated out: <reason>".

What the location of a defect rests on, stated because it is not a human's
confirmation: the lines the fix commit removed or rewrote. A fix that only
added code has no such line and is excluded, as are several other shapes; each
exclusion is counted under its reason in the harvest summary.

Cases and runs are records and live in the data repository, under the registry
names `review-eval-cases` and `review-eval-runs`. Every model call goes through
the proxy, to the named trust list only. Nothing here is scheduled: it runs
when typed.

Usage:
  .venv/bin/python scripts/review-eval.py harvest
  .venv/bin/python scripts/review-eval.py run --config sonnet-rules --sample 12 --clean 6 --judge
  .venv/bin/python scripts/review-eval.py report
  .venv/bin/python scripts/review-eval.py configs

Exit codes: 0 ok, 1 nothing to do (no cases, no runs), 2 usage error.
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import data_path, data_write_dir  # noqa: E402
from scripts.utils.diff_review import (  # noqa: E402
    BLOCKING, CONFIGS, Config, est_tokens, git, is_trusted, numbered, raw_diff, review,
)
from scripts.utils.workspace import get_workspace_root  # noqa: E402

# ============================================================
# Configuration
# ============================================================

ROOT = get_workspace_root()

TOLERANCE = 5            # lines either side of a defect region that still count as a hit
MAX_FIX_LINES = 200      # a fix wider than this in code is a sweep, not one locatable defect
MAX_FIX_FILES = 3
MAX_INTRO_LINES = 1500   # an introducing diff larger than this does not fit one review pass
MIN_CLEAN_LINES = 30
CLEAN_MIN_AGE_DAYS = 7
CLEAN_SURVIVAL = 0.9
CLEAN_POOL = 20
JUDGE_MODEL = "claude-haiku-5-5"

_HUNK0 = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def is_code(path: str) -> bool:
    """Engine code a reviewer is asked about: Python, shell, hooks; never the tests."""
    p = Path(path)
    if path.startswith("tests/") or "/tests/" in path or p.name.startswith("test_") or p.name == "conftest.py":
        return False
    return p.suffix in (".py", ".sh") or path.startswith(".claude/hooks/")


# ============================================================
# Harvest
# ============================================================


def fix_notes(message: str) -> list[str]:
    return [ln.strip()[len("Fix-Note:"):].strip() for ln in message.splitlines() if ln.strip().startswith("Fix-Note:")]


def non_code_lines(text: str, path: str) -> set[int]:
    """Blank, comment and docstring lines of a file, 1-based."""
    lines = text.splitlines()
    out = {i + 1 for i, ln in enumerate(lines) if not ln.strip() or ln.strip().startswith("#")}
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return out
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                        and isinstance(first.value.value, str):
                    out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def removed_old_lines(sha: str, path: str) -> list[int]:
    """Old-side line numbers the commit removed or rewrote in `path`."""
    out: list[int] = []
    for ln in git(ROOT, "diff", "-U0", "--no-color", f"{sha}^", sha, "--", path).splitlines():
        m = _HUNK0.match(ln)
        if m:
            start, count = int(m.group(1)), int(m.group(2) or "1")
            out.extend(range(start, start + count))
    return out


def blame(rev: str, path: str, lines: list[int]) -> list[tuple[str, str, int]]:
    """(introducing sha, its filename, its line) for each line of `path` at `rev`."""
    out: list[tuple[str, str, int]] = []
    ranges: list[tuple[int, int]] = []
    for n in sorted(set(lines)):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], n)
        else:
            ranges.append((n, n))
    args = ["blame", "--line-porcelain", "-w"]
    for a, b in ranges:
        args += ["-L", f"{a},{b}"]
    text = git(ROOT, *args, rev, "--", path)
    sha = orig = None
    for ln in text.splitlines():
        head = re.match(r"^([0-9a-f]{40}) (\d+) \d+", ln)
        if head:
            sha, orig = head.group(1), int(head.group(2))
        elif ln.startswith("filename ") and sha:
            out.append((sha, ln[len("filename "):], orig))
            sha = None
    return out


def numstat(sha: str, base: str | None = None) -> list[tuple[str, int, int]]:
    rows = []
    args = ["diff", "--numstat", "-M", base or f"{sha}^", sha]
    for ln in git(ROOT, *args).splitlines():
        a, d, p = ln.split("\t", 2)
        if "=>" in p:  # rename shown as old => new
            p = re.sub(r"\{[^{}]*=> ([^{}]*)\}", r"\1", p).split(" => ")[-1]
        if a == "-":
            continue
        rows.append((p, int(a), int(d)))
    return rows


def parents(sha: str) -> list[str]:
    return git(ROOT, "rev-list", "--parents", "-n1", sha).split()[1:]


def harvest_one(sha: str, why: Counter) -> dict | None:
    msg = git(ROOT, "log", "-1", "--format=%B", sha)
    notes = fix_notes(msg)
    if len(notes) != 1:
        why["several Fix-Notes in one commit (a sweep; no single defect to locate)"] += 1
        return None
    code = [(p, a, d) for p, a, d in numstat(sha) if is_code(p)]
    if not code:
        why["fix touched no engine code (documentation, rule, config or process)"] += 1
        return None
    if len(code) > MAX_FIX_FILES or sum(a + d for _, a, d in code) > MAX_FIX_LINES:
        why[f"fix wider than {MAX_FIX_FILES} code files or {MAX_FIX_LINES} changed code lines"] += 1
        return None
    defect_lines: dict[str, list[int]] = {}
    any_removed = False
    for path, _, d in code:
        if not d:
            continue
        old = removed_old_lines(sha, path)
        if not old:
            continue
        any_removed = True
        try:
            text = git(ROOT, "show", f"{sha}^:{path}")
        except RuntimeError:
            continue
        skip = non_code_lines(text, path)
        real = [n for n in old if n not in skip]
        if real:
            defect_lines[path] = real
    if not any_removed:
        why["fix only added code (no line of the defect to locate)"] += 1
        return None
    if not defect_lines:
        why["fix changed only comments or docstrings in code files"] += 1
        return None
    blamed: list[tuple[str, str, int]] = []
    for path, lines in defect_lines.items():
        blamed += blame(f"{sha}^", path, lines)
    if not blamed:
        why["blame returned nothing"] += 1
        return None
    intro, _ = Counter(b[0] for b in blamed).most_common(1)[0]
    if len(parents(intro)) != 1:
        why["defect lines come from a merge resolution or the root commit"] += 1
        return None
    intro_code = [(p, a, d) for p, a, d in numstat(intro) if is_code(p)]
    if sum(a + d for _, a, d in intro_code) > MAX_INTRO_LINES:
        why[f"introducing change larger than {MAX_INTRO_LINES} code lines (initial import, bulk move)"] += 1
        return None
    region: dict[str, list[int]] = defaultdict(list)
    for s, fname, line in blamed:
        if s == intro:
            region[fname].append(line)
    if not any(f in {p for p, _, _ in intro_code} for f in region):
        why["defect file is not in the introducing commit's code diff"] += 1
        return None
    return {
        "fix": sha,
        "fix_subject": msg.splitlines()[0],
        "fix_note": notes[0],
        "intro": intro,
        "intro_date": git(ROOT, "log", "-1", "--format=%cI", intro).strip(),
        "intro_code_files": [p for p, _, _ in intro_code],
        "intro_code_lines": sum(a + d for _, a, d in intro_code),
        "region": {f: sorted(set(v)) for f, v in region.items()},
        "blame_share": round(sum(1 for b in blamed if b[0] == intro) / len(blamed), 2),
    }


def patch_id(sha: str) -> str:
    import subprocess

    show = git(ROOT, "show", sha)
    proc = subprocess.run(["git", "-C", str(ROOT), "patch-id", "--stable"], input=show,
                          capture_output=True, text=True, check=True)
    return proc.stdout.split()[0] if proc.stdout.strip() else sha


def harvest_clean(blamed_by_fixes: set[str], case_intros: set[str]) -> tuple[list[dict], Counter]:
    """Known-clean ranges: code commits none of whose added code lines any later commit changed."""
    why: Counter = Counter()
    cutoff = datetime.now(timezone.utc) - timedelta(days=CLEAN_MIN_AGE_DAYS)
    out: list[dict] = []
    for ln in git(ROOT, "log", "--no-merges", "--format=%H %cI", "main").splitlines():
        if len(out) >= CLEAN_POOL:
            break
        sha, when = ln.split(" ", 1)
        if datetime.fromisoformat(when) > cutoff:
            continue
        if sha in blamed_by_fixes or sha in case_intros:
            continue
        if len(parents(sha)) != 1:  # before numstat, which diffs against sha^: a root has none
            continue
        code = [(p, a, d) for p, a, d in numstat(sha) if is_code(p)]
        size = sum(a + d for _, a, d in code)
        if not code or size < MIN_CLEAN_LINES or size > MAX_INTRO_LINES:
            continue
        added = total = 0
        for path, a, _ in code:
            if not a:
                continue
            try:
                git(ROOT, "cat-file", "-e", f"main:{path}")
            except RuntimeError:
                total += a
                continue
            stand = sum(1 for s, _, _ in blame_file("main", path) if s == sha)
            added += min(stand, a)
            total += a
        if not total or added / total < CLEAN_SURVIVAL:
            why["added code lines did not survive to main unchanged"] += 1
            continue
        out.append({"sha": sha, "date": when, "code_files": [p for p, _, _ in code], "code_lines": size,
                    "survival": round(added / total, 2)})
    return out, why


def blame_file(rev: str, path: str) -> list[tuple[str, str, int]]:
    text = git(ROOT, "blame", "--line-porcelain", "-w", rev, "--", path)
    out, sha = [], None
    for ln in text.splitlines():
        head = re.match(r"^([0-9a-f]{40}) (\d+) \d+", ln)
        if head:
            sha, orig = head.group(1), int(head.group(2))
        elif ln.startswith("filename ") and sha:
            out.append((sha, ln[9:], orig))
            sha = None
    return out


def cmd_harvest(args) -> int:
    fixes = git(ROOT, "log", "--no-merges", "--grep=^Fix-Note:", "--format=%H", "main").split()
    all_with_merges = len(git(ROOT, "log", "--grep=^Fix-Note:", "--format=%H", "main").split())
    why: Counter = Counter()
    # Merge commits stay out, by choice: a merge's Fix-Note repeats the one on
    # the branch commit it brings in, which is harvested on its own, and the
    # merge's first-parent diff is the whole branch, not one fix. They are
    # counted as an exclusion so the report accounts for every Fix-Note commit.
    if all_with_merges > len(fixes):
        why["merge commit (its fix is harvested from the branch commit it merges)"] += all_with_merges - len(fixes)
    roots = set(git(ROOT, "rev-list", "--max-parents=0", "main").split())
    if roots & set(fixes):  # numstat diffs against sha^, which a root commit does not have
        why["root commit (no parent to diff against)"] += len(roots & set(fixes))
        fixes = [f for f in fixes if f not in roots]
    cur_file = data_path("review-eval-cases") / "curation.json"
    curation = {k: v for k, v in json.loads(cur_file.read_text()).items() if not k.startswith("_")} \
        if cur_file.exists() else {}
    cases, seen_patch = [], set()
    blamed_by_fixes: set[str] = set()
    for i, sha in enumerate(fixes, 1):
        pid = patch_id(sha)
        if pid in seen_patch:
            why["same patch as an earlier fix (cherry-pick or replay)"] += 1
            continue
        seen_patch.add(pid)
        case = harvest_one(sha, why)
        reason = next((r for k, r in curation.items() if case and sha.startswith(k)), None)
        if reason:
            why[f"curated out: {reason}"] += 1
            continue
        if case:
            cases.append(case)
            blamed_by_fixes.add(case["intro"])
        if i % 25 == 0:
            print(f"{GRAY}  {i}/{len(fixes)} fixes read, {len(cases)} kept{RESET}", file=sys.stderr)
    # Every commit any fix touched is excluded from the clean pool, kept case or not.
    for sha in fixes:
        for path, _, d in numstat(sha):
            if d and is_code(path):
                old = removed_old_lines(sha, path)
                if not old:
                    continue
                try:
                    blamed_by_fixes.update(s for s, _, _ in blame(f"{sha}^", path, old))
                except RuntimeError as e:
                    print(f"{GRAY}  blame skipped for {sha[:8]} {path}: {e}{RESET}", file=sys.stderr)
    clean, clean_why = harvest_clean(blamed_by_fixes, {c["intro"] for c in cases})
    out_dir = data_write_dir("review-eval-cases")
    head = git(ROOT, "rev-parse", "main").strip()
    doc = {
        "harvested": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "main": head,
        "fix_note_commits_with_merges": all_with_merges,
        "fix_note_commits_no_merges": len(fixes),
        "kept": len(cases),
        "excluded": dict(why.most_common()),
        "introducing_commits": len({c["intro"] for c in cases}),
        "clean_ranges": len(clean),
        "clean_skipped": dict(clean_why),
        "rules": {"tolerance": TOLERANCE, "max_fix_lines": MAX_FIX_LINES, "max_fix_files": MAX_FIX_FILES,
                  "max_intro_lines": MAX_INTRO_LINES, "clean_min_age_days": CLEAN_MIN_AGE_DAYS,
                  "clean_survival": CLEAN_SURVIVAL},
    }
    (out_dir / "cases.json").write_text(json.dumps({"meta": doc, "cases": cases, "clean": clean}, indent=1) + "\n")
    print(f"{BOLD}Defect set{RESET}: {len(cases)} cases kept of {len(fixes)} Fix-Note commits "
          f"({all_with_merges} with merges), over {doc['introducing_commits']} introducing commits; "
          f"{len(clean)} known-clean ranges.")
    for reason, n in why.most_common():
        print(f"  excluded {n:>4}  {reason}")
    print(f"Written: {out_dir / 'cases.json'}")
    return 0



# ============================================================
# Run
# ============================================================


def load_cases() -> dict:
    path = data_path("review-eval-cases") / "cases.json"
    if not path.exists():
        raise SystemExit(f"{RED}No defect set at {path}; run `harvest` first.{RESET}")
    return json.loads(path.read_text())


def code_diff(sha: str) -> tuple[str, bool]:
    files = [p for p, _, _ in numstat(sha) if is_code(p)]
    text, _, truncated = numbered(raw_diff(ROOT, f"{sha}^", sha, files))
    return text, truncated


def hits(defect_region: dict[str, list[int]], findings: list[dict]) -> list[dict]:
    out = []
    for f in findings:
        for path, lines in defect_region.items():
            if not (f["file"] == path or path.endswith("/" + f["file"]) or f["file"].endswith("/" + path)):
                continue
            lo, hi = f["line"] - TOLERANCE, f["end_line"] + TOLERANCE
            if any(lo <= n <= hi for n in lines):
                out.append(f)
                break
    return out


JUDGE_PROMPT = """A code reviewer reported a finding on a change. Later, a fix was made whose note says what was actually broken.
Does the finding describe the same defect the fix note describes (the same wrong behaviour, not merely the same lines)?
Answer with exactly one word: MATCH or NO.

Fix note: {note}

Finding: {title}. Failure: {failure}"""


def judge(note: str, f: dict) -> bool | None:
    from scripts.utils.proxy_transport import call_model

    try:
        ans = call_model(JUDGE_MODEL, JUDGE_PROMPT.format(note=note, title=f["title"], failure=f["failure"]),
                         temperature=0.0, max_tokens=4096, timeout=300.0)
    except RuntimeError as e:
        print(f"{YELLOW}judge failed: {e}{RESET}", file=sys.stderr)
        return None
    return "MATCH" in ans.upper()


def run_unit(cfg: Config, unit: dict, use_judge: bool) -> dict:
    diff_text, truncated = code_diff(unit["sha"])
    intent = git(ROOT, "log", "-1", "--format=%B", unit["sha"])
    r = review(cfg, diff_text, intent)
    row = {"kind": unit["kind"], "sha": unit["sha"], "config": cfg.name, "model": cfg.model,
           "seconds": round(r.seconds, 1), "prompt_chars": r.prompt_chars, "answer_chars": r.answer_chars,
           "error": r.error, "truncated": truncated, "findings": r.findings, "dropped_nits": r.dropped_nits}
    if unit["kind"] == "defect":
        scored = []
        for case in unit["cases"]:
            located = hits(case["region"], r.findings)
            verdict = None
            if located and use_judge:
                verdict = any(judge(case["fix_note"], f) for f in located)
            scored.append({"fix": case["fix"], "located": bool(located), "judged": verdict})
        row["defects"] = scored
    return row


def cmd_run(args) -> int:
    cfg = CONFIGS.get(args.config)
    if not cfg:
        print(f"{RED}Unknown config {args.config!r}; see `configs`.{RESET}", file=sys.stderr)
        return 2
    if not is_trusted(cfg.model) or not is_trusted(JUDGE_MODEL):
        print(f"{RED}Model outside the named trust list.{RESET}", file=sys.stderr)
        return 2
    data = load_cases()
    by_intro: dict[str, list[dict]] = defaultdict(list)
    for c in data["cases"]:
        by_intro[c["intro"]].append(c)
    rng = random.Random(args.seed)  # noqa: S311 - a reproducible sample, not a secret
    intros = sorted(by_intro)
    rng.shuffle(intros)
    clean = sorted(data["clean"], key=lambda c: c["sha"])
    rng.shuffle(clean)
    units = [{"kind": "defect", "sha": s, "cases": by_intro[s]} for s in intros[: args.sample or None]]
    units += [{"kind": "clean", "sha": c["sha"]} for c in clean[: args.clean if args.clean is not None else None]]
    if not units:
        print(f"{YELLOW}Nothing to run.{RESET}")
        return 1
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{cfg.name}"
    out = data_write_dir("review-eval-runs") / f"{run_id}.jsonl"
    print(f"{CYAN}{cfg.name}{RESET} ({cfg.model}): {len(units)} reviews, seed {args.seed}, {args.workers} at a time -> {out}")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool, out.open("w") as fh:
        fh.write(json.dumps({"kind": "meta", "run": run_id, "config": cfg.name, "model": cfg.model,
                             "rules": cfg.rules, "seed": args.seed, "sample": args.sample, "clean": args.clean,
                             "judge": JUDGE_MODEL if args.judge else None, "cases_main": data["meta"]["main"]}) + "\n")
        for row in pool.map(lambda u: run_unit(cfg, u, args.judge), units):
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            mark = RED + "error" + RESET if row["error"] else f"{len([f for f in row['findings'] if f['severity'] in BLOCKING])} blocking"
            extra = ""
            if row["kind"] == "defect":
                extra = " found " + ",".join("Y" if d["located"] else "n" for d in row["defects"])
            print(f"  {row['kind']:<6} {row['sha'][:8]} {row['seconds']:>6.0f}s  {mark}{extra}")
    print(f"Done in {time.monotonic() - t0:.0f}s. Written: {out}")
    return 0


# ============================================================
# Report
# ============================================================


def summarise(rows: list[dict]) -> dict:
    defects = [d for r in rows if r["kind"] == "defect" and not r["error"] for d in r["defects"]]
    clean = [r for r in rows if r["kind"] == "clean" and not r["error"]]
    reviews = [r for r in rows if r["kind"] in ("defect", "clean")]
    judged = [d for d in defects if d["judged"] is not None]
    false = sum(1 for r in clean for f in r["findings"] if f["severity"] in BLOCKING)
    return {
        "reviews": len(reviews),
        "errors": sum(1 for r in reviews if r["error"]),
        "defects": len(defects),
        "located": sum(d["located"] for d in defects),
        "judged_match": sum(1 for d in judged if d["judged"]),
        "judge_ran": any(r.get("defects") and any(d["judged"] is not None for d in r["defects"]) for r in rows),
        "defect_reviews": sum(1 for r in rows if r["kind"] == "defect" and not r["error"]),
        "blocking_on_defect_diffs": sum(1 for r in rows if r["kind"] == "defect" and not r["error"]
                                        for f in r["findings"] if f["severity"] in BLOCKING),
        "clean": len(clean),
        "false_reports": false,
        "clean_flagged": sum(1 for r in clean if any(f["severity"] in BLOCKING for f in r["findings"])),
        "seconds": sum(r["seconds"] for r in reviews),
        "tokens_in": sum(est_tokens(r["prompt_chars"]) for r in reviews),
        "tokens_out": sum(est_tokens(r["answer_chars"]) for r in reviews),
    }


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({100 * a / b:.0f}%)" if b else "n/a"


def cmd_report(args) -> int:
    run_dir = data_path("review-eval-runs")
    files = sorted(run_dir.glob("*.jsonl")) if run_dir.exists() else []
    if args.runs:
        files = [f for f in files if any(r in f.name for r in args.runs)]
    if not files:
        print(f"{YELLOW}No runs under {run_dir}.{RESET}")
        return 1
    data = load_cases()
    meta = data["meta"]
    lines = [f"# Reviewer evaluation, {datetime.now(timezone.utc):%Y-%m-%d}", "",
             f"Defect set: {meta['kept']} cases kept of {meta['fix_note_commits_no_merges']} Fix-Note commits "
             f"({meta['fix_note_commits_with_merges']} with merges) on main {meta['main'][:8]}, over "
             f"{meta['introducing_commits']} introducing commits; {meta['clean_ranges']} known-clean ranges.", "",
             "Excluded, by reason:"]
    lines += [f"- {n} {reason}" for reason, n in meta["excluded"].items()]
    lines += ["", "| run | config | model | reviews (errors) | located | judged match | blocking per defect diff | "
                  "false reports on clean | clean ranges flagged | review time | est. tokens in/out |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for f in files:
        rows = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        m = rows[0] if rows and rows[0].get("kind") == "meta" else {}
        s = summarise(rows[1:] if m else rows)
        per = f"{s['blocking_on_defect_diffs'] / s['defect_reviews']:.1f}" if s["defect_reviews"] else "n/a"
        jm = pct(s["judged_match"], s["defects"]) if s["judge_ran"] else "not run"
        lines.append(f"| {f.stem} | {m.get('config', '?')} | {m.get('model', '?')} | {s['reviews']} ({s['errors']}) | "
                     f"{pct(s['located'], s['defects'])} | {jm} | {per} | {s['false_reports']} | "
                     f"{pct(s['clean_flagged'], s['clean'])} | {s['seconds'] / 60:.1f} min | "
                     f"{s['tokens_in']:,}/{s['tokens_out']:,} |")
    lines += ["", "Located: a finding names the defect's file and a line within "
              f"{TOLERANCE} lines of the lines the fix later changed. Judged match: the judge model ({JUDGE_MODEL}) "
              "also says the finding describes the Fix-Note's defect. False reports: bug or security findings on "
              "known-clean ranges (an upper bound; an unchanged line can still be wrong). Tokens are estimated at "
              "four characters a token; the proxy reports no usage and its subscription has no per-call price.", ""]
    text = "\n".join(lines)
    out = data_write_dir("review-eval-runs") / f"report-{datetime.now(timezone.utc):%Y-%m-%d}.md"
    out.write_text(text)
    print(text)
    print(f"{GREEN}Written: {out}{RESET}")
    return 0


def cmd_configs(_args) -> int:
    for c in CONFIGS.values():
        print(f"  {c.name:<14} {c.model:<20} rules={'yes' if c.rules else 'no'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("harvest", help="build the defect set and the clean ranges from main")
    r = sub.add_parser("run", help="review a sample of the set with one configuration")
    r.add_argument("--config", required=True)
    r.add_argument("--sample", type=int, default=0, help="introducing commits to review (0 = all)")
    r.add_argument("--clean", type=int, default=None, help="clean ranges to review (default all)")
    r.add_argument("--seed", type=int, default=20261018)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--judge", action="store_true", help=f"confirm located hits with {JUDGE_MODEL}")
    rp = sub.add_parser("report", help="compare runs")
    rp.add_argument("runs", nargs="*", help="substrings of run ids to include (default all)")
    sub.add_parser("configs", help="list reviewer configurations")
    args = ap.parse_args()
    return {"harvest": cmd_harvest, "run": cmd_run, "report": cmd_report, "configs": cmd_configs}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
