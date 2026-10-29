#!/usr/bin/env python3
"""skill-trigger-test.py - LLM-judge regression test for the skill router.

This workspace routes natural-language messages to skills via a markdown RULE the model
interprets (`.claude/rules/skill-router.md`), NOT a callable function. So routing cannot
be unit-tested deterministically. This harness tests the rule AS IT ACTUALLY EXECUTES:
it feeds the router rules plus the target skill's own trigger description to a judge model,
asks whether a given query routes to that skill, and compares the verdict to the
`should_trigger` expectation in the skill's `triggers.json`.

Because the judge is a model, results are NON-DETERMINISTIC. This is an ADVISORY signal,
run by hand, never a hard
blocking CI gate. `--strict` makes it exit non-zero when a skill's pass rate falls below
`--threshold`, for callers that want a gate; the default is advisory (always exit 0 on a
completed run).

triggers.json shape (array of cases), per skill at .claude/skills/{name}/triggers.json:

    [
      { "query": "investigate ExampleTelco's leadership", "should_trigger": true },
      { "query": "validate this claim",               "should_trigger": false }
    ]

Usage:
  python scripts/skill-trigger-test.py --skill osint
  python scripts/skill-trigger-test.py --all
  python scripts/skill-trigger-test.py --all --json
  python scripts/skill-trigger-test.py --all --strict --threshold 0.9
  python scripts/skill-trigger-test.py --skill osint --model haiku

A case the judge never returns a usable verdict for is UNMEASURED, not a routing miss:
it is retried once, then counted in `errored` and excluded from the pass rate, so an
API hiccup or a more verbose judge cannot masquerade as a router regression.

Exit codes: 0 completed (advisory, or strict-pass), 1 UNDER --strict, either the
threshold was breached or a skill was left unmeasured, 2 setup error, 3 API/key error.

An advisory run in which a skill is left unmeasured completes and exits 0; a caller
that needs to know reads the `unmeasured` field in --json.

COMPETITIVE MODE (`--competitive`) asks a different question, the one a live
session answers: given EVERY model-invocable skill description and the router
under test, which one skill (or none) does this message go to? No detail file is
sent, because a live session does not read one before choosing. Scored by exact
match, per skill, per overlap cluster (`OVERLAP_CLUSTERS`) and overall.

  python scripts/skill-trigger-test.py --all --competitive --model opus --extra-cases cases.json --json
  python scripts/skill-trigger-test.py --cluster corporate --competitive --router-file <.claude/rules/*.md>

`--router-file`, `--extra-cases` and `--cluster` belong to this mode only. The
default mode and `router-accuracy-nightly.py` are untouched by it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import claude_models  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.content_denylist import build_denylist  # noqa: E402
from scripts.utils.egress_proof import EGRESS_CLEAR, egress_state  # noqa: E402
from scripts.utils.router_payload import (  # noqa: E402
    OVERLAP_CLUSTERS,
    InvalidSkillMetadata,
    ProvenanceError,
    competitive_snapshot,
    competitive_user_text,
    git_ignored,
    load_skill_description,
    provenance_mismatches,
    load_triggers,
    router_rules_text,
    system_text,
    user_text,
)
from scripts.utils.workspace import get_data_root, get_workspace_root, load_env  # noqa: E402

ROOT = get_workspace_root()
SKILLS_DIR = ROOT / ".claude" / "skills"
ROUTER_RULE = ROOT / ".claude" / "rules" / "skill-router.md"


def load_full_router_rules() -> str:
    """The exact rule text the judge is sent.

    Delegates to `scripts.utils.router_payload`, which is the single place the
    outbound payload is built, so the nightly egress check cannot drift from what
    this script actually sends. Kept as a name here because it is the harness's
    published surface.
    """
    return router_rules_text()


def list_skills_with_triggers() -> list[str]:
    out = []
    for child in sorted(SKILLS_DIR.iterdir()):
        if child.is_dir() and (child / "triggers.json").exists():
            out.append(child.name)
    return out


# The judge runs on a family, never a version, so a new Sonnet judges routing
# the day it ships. `claude_models.resolve` passes an explicit model id through
# untouched for a caller reproducing an older judge run.
DEFAULT_FAMILY = "sonnet"


# ---------------------------------------------------------------------------
# Changed-scope selection (--changed)
# ---------------------------------------------------------------------------

def _git_changed_files(base: str = "origin/main") -> set[str]:
    """Union of changed paths (POSIX, repo-relative) in the engine tree: committed
    `base..HEAD` (only if `base` resolves), working-tree edits, and untracked files.

    Degrades clearly: a missing/unresolvable `base` drops the committed diff and
    prints a note; a git failure yields an empty contribution, never an exception.
    """
    files: set[str] = set()

    def _run(args: list[str], *, paths: bool = False) -> list[str]:
        """Lines from a git command; with `paths=True`, NUL-separated real paths.

        `core.quotePath` defaults to on, so `diff --name-only` and `ls-files`
        C-quote any path with a byte outside printable ASCII. A quoted name
        matches no skill directory, so the routing gate would silently test
        nothing for that skill. `paths` is opt-in because this helper also runs
        `rev-parse`, which takes no such flag; the caller writes `-z` itself, so
        a repo-wide guard could read it in the argv. That guard was parked in
        the 2026-09-21 test-suite split, so the `-z` at each call site is now
        held by the call site alone.

        Decoded from BYTES rather than through subprocess text mode, for the
        half of the defect `-z` does not reach: text mode turns on universal
        newlines and rewrites every CR byte to LF, with no `newline=` knob to
        switch it off. MEASURED 2026-08-30 - `docs/x\\r\\ny.md` and
        `docs/x\\ny.md` are two tracked files that `text=True` reports as one. A
        mistranslated name matches no skill directory, which is exactly the
        silent no-test the paragraph above describes.
        """
        try:
            out = subprocess.run(
                ["git", *args], cwd=str(ROOT), capture_output=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if out.returncode != 0:
            return []
        decoded = out.stdout.decode("utf-8", "surrogateescape")
        if paths:
            return [p for p in decoded.split("\0") if p]
        return [ln.strip() for ln in decoded.splitlines() if ln.strip()]

    # Through `_run`, not around it. This probe had no timeout and no
    # FileNotFoundError guard, so a machine without git on PATH -- or a hung
    # repo -- crashed the --changed gate with a traceback, past the very
    # "degrades clearly, never an exception" contract `_run` implements.
    base_ok = bool(_run(["rev-parse", "--verify", "--quiet", base]))
    if base_ok:
        files.update(_run(["diff", "--name-only", "-z", f"{base}..HEAD"], paths=True))
    else:
        print(f"{GRAY}routing-gate: base '{base}' unresolved; using working-tree diff only{RESET}")
    files.update(_run(["diff", "--name-only", "-z", "HEAD"], paths=True))
    files.update(_run(["ls-files", "-z", "--others", "--exclude-standard"], paths=True))
    return files


def changed_routing_skills(base: str = "origin/main") -> list[str]:
    """Skills whose routing surface changed since `base`. A change to the router
    rule widens scope to every skill with a triggers.json (the rule affects all)."""
    changed = _git_changed_files(base)
    if ".claude/rules/skill-router.md" in changed or any(
        c.startswith("reference/skill-router/") and c.endswith(".md") for c in changed
    ):
        return list_skills_with_triggers()
    skills: set[str] = set()
    for path in changed:
        parts = path.split("/")
        if (
            len(parts) >= 4
            and parts[0] == ".claude"
            and parts[1] == "skills"
            and parts[3] in ("SKILL.md", "triggers.json")
            and (SKILLS_DIR / parts[2] / "triggers.json").exists()
        ):
            skills.add(parts[2])
    return sorted(skills)


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

def build_system(router_rules: str, skill_name: str, skill_desc: str) -> str:
    """The system prompt, byte for byte, from the shared payload module."""
    return system_text(skill_name, skill_desc, router_rules)


def build_user(query: str, target: str) -> str:
    """The user message for one case, byte for byte, from the same module."""
    return user_text(query, target)


# Measured 2026-08-10, the first night the judge family resolved to Sonnet 5: the
# previous 300-token ceiling truncated the verdict JSON mid-`reason` string, and two
# replies came back empty. 47 cases across 32 skills scored as routing misses when the
# router had not changed at all - no commit had touched .claude/skills or .claude/rules
# since 2026-08-08. The ceiling is a property of the judge's verbosity, which moves
# whenever the family resolves to a newer model, so it is set well clear of the
# ~120-token verdict rather than trimmed to it. It is the VISIBLE budget: on a
# model that thinks, thinking is billed against the same cap, so
# `claude_models.request_params` raises it (the empty replies above are the
# shape that failure takes).
JUDGE_MAX_TOKENS = 1000

# The verdict `_parse_verdict` reads, enforced on the model's side. The text
# parser stays for a reply that somehow arrives without it.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "routes_to_target": {"type": "boolean"},
        "skill": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["routes_to_target", "skill", "reason"],
    "additionalProperties": False,
}

# One retry, because an empty reply is transient weather and a second call is cheap
# next to mislabelling a case. A verdict that is still absent after the retry is
# reported as unmeasured, never as a routing miss.
JUDGE_ATTEMPTS = 2


def _parse_verdict(text: str) -> dict:
    """Parse one judge reply. `routes_to_target: None` means the judge did not answer."""
    # Tolerate a fenced or chatty reply: extract the first {...} block.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {"routes_to_target": None, "skill": "?", "reason": f"unparseable: {text[:80]}"}
    try:
        # The slice starts at `{`, so json.loads yields a dict or raises; no
        # isinstance(dict) guard is needed.
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {"routes_to_target": None, "skill": "?", "reason": f"bad json: {text[:80]}"}


def judge_query(client, model: str, system: str, query: str, target: str) -> dict:
    """Ask the judge whether `query` routes to `target`. Returns the parsed verdict dict.

    Retries once when the reply does not carry a usable verdict. The caller reads a
    non-boolean `routes_to_target` as "not measured", so this function never has to
    invent one.
    """
    user = build_user(query, target)
    verdict: dict = {}
    for _ in range(JUDGE_ATTEMPTS):
        try:
            response = client.messages.create(
                model=model,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **claude_models.request_params(
                    model, max_tokens=JUDGE_MAX_TOKENS, effort="low",
                    output_schema=VERDICT_SCHEMA),
            )
        except Exception as exc:  # noqa: BLE001 - any transport fault, reported not raised
            # UNMEASURED, not fatal. This call had no handler at all, so one
            # transient 529 mid-sweep raised through run_skill and threw away a
            # 96-skill `--all` run -- dozens of paid judge calls already made.
            # The caller already reads a non-boolean `routes_to_target` as "not
            # measured", which is exactly the right meaning here.
            verdict = {"routes_to_target": None, "skill": "?",
                       "reason": f"api error: {type(exc).__name__}: {exc}"[:160]}
            continue
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason in ("refusal", "max_tokens"):
            # Unmeasured and named: a refusal or a spent cap used to reach
            # `_parse_verdict` as empty text and read as "unparseable".
            verdict = {"routes_to_target": None, "skill": "?",
                       "reason": f"judge stopped: stop_reason={stop_reason}"}
            continue
        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()
        verdict = _parse_verdict(text)
        if isinstance(verdict.get("routes_to_target"), bool):
            return verdict
    return verdict


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_skill(client, model: str, router_rules: str, skill_name: str) -> dict:
    """Run all trigger cases for one skill. Returns a result dict.

    `cases` counts the cases the judge actually answered, so the rate measures the
    ROUTER. A case the judge never returned a verdict for is counted in `errored`
    and kept out of both numerator and denominator: it is no evidence either way,
    and scoring it as a miss is what turned a judge-model upgrade into a phantom
    38-point drop on /voss on 2026-08-10.
    """
    skill_dir = SKILLS_DIR / skill_name
    cases = load_triggers(skill_dir)
    if not cases:
        # Say which of the two it is. One message covered both, and "no
        # triggers.json" was a false statement of what was measured for the
        # skill whose triggers.json is present and empty.
        reason = ("triggers.json holds no cases"
                  if (skill_dir / "triggers.json").exists()
                  else "no triggers.json")
        return {"skill": skill_name, "cases": 0, "passed": 0, "errored": 0,
                "results": [], "skipped": True, "skip_reason": reason}

    system = build_system(router_rules, skill_name, load_skill_description(skill_dir))
    results = []
    passed = 0
    judged = 0
    errored = 0
    for case in cases:
        query = case["query"]
        expected = bool(case["should_trigger"])
        verdict = judge_query(client, model, system, query, skill_name)
        got = verdict.get("routes_to_target")
        if isinstance(got, bool):
            judged += 1
            ok = got is expected
            passed += ok
        else:
            errored += 1
            ok = None  # unmeasured, distinct from False
        results.append({
            "query": query,
            "expected": expected,
            "got": got,
            "ok": ok,
            "judged_skill": verdict.get("skill", "?"),
            "reason": verdict.get("reason", ""),
        })
    return {"skill": skill_name, "cases": judged, "passed": passed, "errored": errored,
            "results": results, "skipped": False, "skip_reason": ""}


def print_skill_report(r: dict, threshold: float) -> None:
    if r["skipped"]:
        print(f"{YELLOW}skip{RESET}: {r['skill']} - "
              f"{r.get('skip_reason', 'no triggers.json')}; nothing was measured")
        return
    rate = r["passed"] / r["cases"] if r["cases"] else 0.0
    errored = r.get("errored", 0)
    if r["cases"]:
        color = GREEN if rate >= threshold else RED
        head = f"{color}{r['passed']}/{r['cases']}{RESET} ({rate:.0%})"
    else:
        head = f"{YELLOW}unmeasured{RESET}"
    tail = f"  {YELLOW}{errored} unmeasured{RESET}" if errored and r["cases"] else ""
    print(f"\n{BOLD}{CYAN}{r['skill']}{RESET}  {head}{tail}")
    for res in r["results"]:
        if res["ok"] is None:
            print(f"  {YELLOW}NO VERDICT{RESET} {res['query']!r}  ({res['reason']})")
        elif not res["ok"]:
            exp = "trigger" if res["expected"] else "NOT trigger"
            print(f"  {RED}MISS{RESET} {res['query']!r}")
            print(f"       expected {exp}; judge said skill={res['judged_skill']!r} ({res['reason']})")


# ---------------------------------------------------------------------------
# Competitive mode
# ---------------------------------------------------------------------------

# The field description tells the judge how to spell "no skill". Without it the
# judge answered "" for a message no skill may take: MEASURED 2026-10-29, 3 of 6
# handover-cluster cases, each reason correctly saying no skill applies, scored
# as wrong answers.
COMPETITIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {"type": "string",
                  "description": "the one skill name, or none when no skill applies"},
        "reason": {"type": "string"},
    },
    "required": ["skill", "reason"],
    "additionalProperties": False,
}

# Parallel calls only shorten the wall clock; each case is judged alone and the
# result does not depend on the order. Kept low so a 755-case run stays clear of
# the rate limit the retry would otherwise have to absorb.
COMPETITIVE_WORKERS = 6


def normalise_answer(value: str) -> str:
    """`skill` as the judge wrote it, stripped, lower-cased, one leading `/` off."""
    value = value.strip().lower()
    return value[1:] if value.startswith("/") else value


class CallCounter:
    """Every attempt to call the API, retries and raising attempts included."""

    def __init__(self) -> None:
        self.n = 0
        self._lock = threading.Lock()

    def bump(self) -> None:
        with self._lock:
            self.n += 1


def judge_competitive(client, model: str, system: str, user: str,
                      counter: CallCounter) -> tuple[str | None, str]:
    """Send `system` and `user`, the strings the egress check scanned, unchanged.
    Returns (normalised answer or None, reason).

    MEASURED means the reply parsed to an object whose `skill` is a string.
    Anything else, after the one retry, is (None, why): unmeasured, never a miss.
    """
    why = ""
    for _ in range(JUDGE_ATTEMPTS):
        counter.bump()
        try:
            response = client.messages.create(
                model=model,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **claude_models.request_params(
                    model, max_tokens=JUDGE_MAX_TOKENS, effort="low",
                    output_schema=COMPETITIVE_SCHEMA),
            )
        except Exception as exc:  # noqa: BLE001 - any transport fault, reported not raised
            why = f"api error: {type(exc).__name__}: {exc}"[:160]
            continue
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason in ("refusal", "max_tokens"):
            why = f"judge stopped: stop_reason={stop_reason}"
            continue
        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()
        start, end = text.find("{"), text.rfind("}")
        try:
            verdict = json.loads(text[start:end + 1]) if start != -1 and end != -1 else None
        except json.JSONDecodeError:
            verdict = None
        if isinstance(verdict, dict) and isinstance(verdict.get("skill"), str):
            return normalise_answer(verdict["skill"]), str(verdict.get("reason", ""))
        why = f"unparseable: {text[:80]}"
    return None, why


def load_extra_cases(path: Path) -> list[dict]:
    """The extra-case corpus: a JSON array of {query, skill, cluster}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON array")
    for i, case in enumerate(data):
        if not (isinstance(case, dict) and isinstance(case.get("query"), str)
                and isinstance(case.get("skill"), str)
                and case.get("cluster") in OVERLAP_CLUSTERS):
            raise ValueError(
                f"{path}: case {i} needs a string query and skill and a cluster "
                f"from {sorted(OVERLAP_CLUSTERS)}")
    return data


def clusters_of(skill: str) -> list[str]:
    return [c for c, members in OVERLAP_CLUSTERS.items() if skill in members]


def _rate(passed: int, cases: int) -> float:
    return round(passed / cases, 4) if cases else 0.0


def score_case(expected: str, answer: str | None, vocabulary: set[str]) -> bool | None:
    """One case's verdict. None means unmeasured.

    An answer outside `vocabulary` (the listed skills plus `none`), the empty
    string included, is measured and WRONG for every case type. Scoring it
    only against the named skill let `imaginary-skill` pass every
    `should_trigger: false` case: MEASURED 2026-10-29, 3 of 3 on /voss with a
    stub judge.
    """
    if answer is None:
        return None
    if answer not in vocabulary:
        return False
    if expected.startswith("not:"):
        return answer != expected[4:]
    return answer == expected


def case_kind(case: dict) -> str:
    """`positive`, `negative` (a should_trigger: false case) or `extra`."""
    if case["source"] == "extra":
        return "extra"
    return "negative" if case["expected"].startswith("not:") else "positive"


def build_competitive_cases(cases_by_skill: dict[str, list[dict]],
                            extras: list[dict]) -> list[dict]:
    """The selected cases from the snapshot, each with the exact `user` string
    that is scanned and sent."""
    cases: list[dict] = []
    for name, rows in cases_by_skill.items():
        for case in rows:
            hit = bool(case["should_trigger"])
            cases.append({
                "source": "triggers", "skill": name,
                "cluster": ",".join(clusters_of(name)) or None,
                "query": case["query"],
                "expected": name if hit else f"not:{name}",
            })
    for case in extras:
        cases.append({
            "source": "extra", "skill": case["skill"], "cluster": case["cluster"],
            "query": case["query"], "expected": normalise_answer(case["skill"]),
        })
    for case in cases:
        case["user"] = competitive_user_text(case["query"])
    return cases


def run_competitive(client, model: str, system: str, cases: list[dict],
                    vocabulary: set[str]) -> tuple[list[dict], int]:
    """Judge every case once against the one system prompt the egress check
    scanned. Nothing is re-read here: the strings sent are the strings scanned."""
    counter = CallCounter()

    def _one(case: dict) -> dict:
        answer, reason = judge_competitive(client, model, system, case["user"], counter)
        return {**case, "answer": answer,
                "ok": score_case(case["expected"], answer, vocabulary), "reason": reason}

    with ThreadPoolExecutor(max_workers=COMPETITIVE_WORKERS) as pool:
        return list(pool.map(_one, cases)), counter.n


def score_competitive(results: list[dict], skills: list[str]) -> dict:
    """Per-skill, per-cluster, per-kind and overall rates over `results`."""
    def _score(rows: list[dict]) -> dict:
        judged = [r for r in rows if r["ok"] is not None]
        p = sum(1 for r in judged if r["ok"])
        return {"rate": _rate(p, len(judged)), "passed": p, "cases": len(judged)}

    skill_rows = [{"skill": name, **_score([r for r in results if r["source"] == "triggers"
                                             and r["skill"] == name])}
                  for name in skills]
    selected_clusters = {c for r in results
                         for c in (clusters_of(r["skill"]) if r["source"] == "triggers"
                                   else [r["cluster"]])}
    clusters = {
        c: _score([r for r in results
                   if (r["source"] == "triggers" and r["skill"] in members)
                   or (r["source"] == "extra" and r["cluster"] == c)])
        for c, members in OVERLAP_CLUSTERS.items() if c in selected_clusters
    }
    overall = _score(results)
    return {
        "overall_rate": overall["rate"],
        "total_passed": overall["passed"],
        "total_cases": overall["cases"],
        "unmeasured_cases": sum(1 for r in results if r["ok"] is None),
        "by_kind": {k: _score([r for r in results if case_kind(r) == k])
                    for k in ("positive", "negative", "extra")},
        "clusters": clusters,
        "skills": skill_rows,
    }


def print_competitive_report(report: dict) -> None:
    for r, reason in zip(report["cases"], report["_reasons"], strict=True):
        if r["ok"] is None:
            print(f"  {YELLOW}NO VERDICT{RESET} [{r['skill']}] {r['query']!r}  ({reason})")
        elif not r["ok"]:
            print(f"  {RED}MISS{RESET} [{r['source']}:{r['skill']}] {r['query']!r}")
            print(f"       expected {r['expected']}; judge said {r['answer']!r} ({reason})")
    print(f"\n{BOLD}By kind{RESET}")
    for name, k in report["by_kind"].items():
        print(f"  {name:10s} {k['passed']}/{k['cases']} ({k['rate']:.0%})")
    print(f"\n{BOLD}Clusters{RESET}")
    for name, c in report["clusters"].items():
        print(f"  {name:10s} {c['passed']}/{c['cases']} ({c['rate']:.0%})")
    print(f"\n{BOLD}Overall: {report['total_passed']}/{report['total_cases']} "
          f"({report['overall_rate']:.1%}){RESET}  {GRAY}model={report['model']} "
          f"router={report['router_file']} ({report['router_bytes']} bytes) "
          f"api_calls={report['api_calls']}{RESET}")
    if report["unmeasured_cases"]:
        print(f"{YELLOW}{report['unmeasured_cases']} case(s) had no judge verdict and "
              f"are excluded from every rate{RESET}")
    if report["invalid_answers"]:
        print(f"{YELLOW}{report['invalid_answers']} answer(s) named no listed skill "
              f"and no `none`; scored wrong{RESET}")
    if report["unmeasured_skills"]:
        print(f"{YELLOW}Selected but unmeasured (no triggers.json cases): "
              f"{', '.join(report['unmeasured_skills'])}{RESET}")
    if report["breached"]:
        print(f"{YELLOW}Below {report['_threshold']:.0%}: {', '.join(report['breached'])}{RESET}")
    if report["allow_uncommitted"]:
        print(f"{YELLOW}--allow-uncommitted: uncommitted sources sent: "
              f"{', '.join(report['dirty_sources']) or 'none'}{RESET}")


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--skill", help="Skill name (directory under .claude/skills/)")
    g.add_argument("--all", action="store_true", help="Run every skill with a triggers.json")
    g.add_argument("--changed", action="store_true",
                   help="Run only skills whose SKILL.md/triggers.json changed since --base "
                        "(a skill-router.md change widens to all)")
    g.add_argument("--cluster", choices=sorted(OVERLAP_CLUSTERS),
                   help="(--competitive) the triggers.json cases of one overlap "
                        "cluster's members plus the extra cases tagged with it")
    parser.add_argument("--competitive", action="store_true",
                        help="Pick one skill (or none) out of every listed description, "
                             "as a live session does, and score exact matches")
    parser.add_argument("--router-file",
                        help="(--competitive) router rule to measure: a .md file under "
                             ".claude/rules/ that git does not ignore, relative to the "
                             "checkout root, no symlink and no '..' (default the live "
                             ".claude/rules/skill-router.md)")
    parser.add_argument("--allow-uncommitted", action="store_true",
                        help="(--competitive) send even when a payload source has "
                             "uncommitted changes; names them and records it in the report")
    parser.add_argument("--extra-cases",
                        help="(--competitive) JSON array of {query, skill, cluster} cases")
    parser.add_argument("--base", default="origin/main",
                        help="Diff base for --changed (default origin/main)")
    parser.add_argument("--model", help=f"Judge model (haiku/sonnet/opus/fable or full id; default {DEFAULT_FAMILY})")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of text")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if a skill's pass rate < threshold")
    parser.add_argument("--threshold", type=float, default=0.9, help="Strict pass-rate threshold (default 0.9)")
    args = parser.parse_args(argv)

    if not args.competitive:
        for flag, value in (("--router-file", args.router_file),
                            ("--extra-cases", args.extra_cases),
                            ("--allow-uncommitted", args.allow_uncommitted),
                            ("--cluster", args.cluster)):
            if value:
                print(f"{RED}ERROR{RESET}: {flag} belongs to --competitive mode only",
                      file=sys.stderr)
                return 2
    else:
        return main_competitive(args)

    if not ROUTER_RULE.exists():
        print(f"{RED}ERROR{RESET}: router rule not found at {ROUTER_RULE}", file=sys.stderr)
        return 2

    if args.skill:
        if not (SKILLS_DIR / args.skill).is_dir():
            # A typo used to print one skip line and return 0, including under
            # --strict. There is no such skill to measure, which is a setup
            # error (exit 2), not a passing routing check.
            print(f"{RED}ERROR{RESET}: no skill directory "
                  f"{SKILLS_DIR / args.skill}", file=sys.stderr)
            return 2
        skills = [args.skill]
    elif args.changed:
        skills = changed_routing_skills(args.base)
        if not skills:
            # Empty scope returns BEFORE the key check / client build, so the gate
            # is cost-free (no API key required) on the common no-routing-change push.
            print(f"{GREEN}no routing-sensitive changes since {args.base}{RESET}")
            return 0
    else:
        skills = list_skills_with_triggers()
    if not skills:
        print(f"{YELLOW}No skills with triggers.json found{RESET}", file=sys.stderr)
        return 2

    # Degrade clearly per console-first rule: no key → plain message, non-zero, no hang.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"{RED}ERROR{RESET}: ANTHROPIC_API_KEY not set; cannot run the LLM judge.", file=sys.stderr)
        return 3
    try:
        import anthropic
    except ImportError:
        print(f"{RED}ERROR{RESET}: anthropic SDK not installed.", file=sys.stderr)
        return 3
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], default_headers={"User-Agent": user_agent()})
    model = claude_models.resolve(args.model, default_family=DEFAULT_FAMILY)

    router_rules = load_full_router_rules()
    t0 = time.time()
    reports = [run_skill(client, model, router_rules, name) for name in skills]
    elapsed = time.time() - t0

    active = [r for r in reports if not r["skipped"]]
    total_cases = sum(r["cases"] for r in active)
    total_passed = sum(r["passed"] for r in active)
    total_errored = sum(r.get("errored", 0) for r in active)
    overall = total_passed / total_cases if total_cases else 0.0
    # A skill with no answered case is UNMEASURED, not failing. Folding it into
    # `breached` would report a dead judge as a routing regression, which is the
    # confusion this whole split exists to end.
    breached = [r for r in active if r["cases"] and (r["passed"] / r["cases"]) < args.threshold]
    # Over `reports`, not `active`. A skipped skill measured nothing either, and
    # dropping it here is what let `--skill osnit-typo --strict` and an empty
    # triggers.json both exit 0 - a clean routing check that judged nothing.
    unmeasured = [r for r in reports if r["skipped"] or not r["cases"]]

    if args.json:
        print(json.dumps({
            "model": model,
            "elapsed_seconds": round(elapsed, 2),
            "overall_rate": round(overall, 4),
            "total_passed": total_passed,
            "total_cases": total_cases,
            "total_errored": total_errored,
            "threshold": args.threshold,
            "strict": args.strict,
            "below_threshold": [r["skill"] for r in breached],
            "unmeasured": [r["skill"] for r in unmeasured],
            "skills": reports,
        }, indent=2))
    else:
        for r in reports:
            print_skill_report(r, args.threshold)
        print(f"\n{BOLD}Overall: {total_passed}/{total_cases} ({overall:.0%})  "
              f"{GRAY}model={model} {elapsed:.1f}s{RESET}")
        if total_errored:
            print(f"{YELLOW}{total_errored} case(s) had no judge verdict and are "
                  f"excluded from the rate{RESET}")
        if breached:
            print(f"{YELLOW}Below {args.threshold:.0%}: {', '.join(r['skill'] for r in breached)}{RESET}")
        if unmeasured:
            print(f"{YELLOW}Unmeasured (no cases to judge, or no judge verdict): "
                  f"{', '.join(r['skill'] for r in unmeasured)}{RESET}")
        if not args.strict:
            print(f"{GRAY}advisory run (pass --strict to gate on the threshold){RESET}")

    # Strict means "gate on a clean measurement". A skill nobody could measure is
    # not a clean measurement, so it fails the gate too - loudly and by its own name.
    if args.strict and (breached or unmeasured):
        return 1
    return 0


def main_competitive(args) -> int:
    """`--competitive`: select cases, build the payload once, prove it, send it."""
    # Only a rule file can be a router candidate. Anything else under the
    # checkout (`.env` among them) would otherwise go to the provider whenever
    # `--allow-uncommitted` is also given, and that flag overrides neither
    # refusal here. A git error deciding "ignored" refuses too.
    # LEXICAL: only ROOT is resolved. Resolving the candidate or the rules
    # directory first would follow a symlink at either one and move the
    # boundary before `read_no_follow` could see the link (MEASURED
    # 2026-10-29: `.claude/rules -> ../private` sent private/notes.md).
    if args.router_file:
        given = Path(args.router_file)
        router_file = given if given.is_absolute() else ROOT / given
    else:
        router_file = ROUTER_RULE
    rules_dir = ROOT / ".claude" / "rules"
    if (".." in router_file.parts or not router_file.is_relative_to(rules_dir)
            or router_file.suffix != ".md" or not router_file.is_file()):
        print(f"{RED}ERROR{RESET}: --router-file {args.router_file} is not a .md file "
              f"inside {rules_dir}", file=sys.stderr)
        return 2
    try:
        ignored = git_ignored(router_file)
    except ProvenanceError as exc:
        print(f"{RED}ERROR{RESET}: router file {router_file}: {exc}", file=sys.stderr)
        return 2
    if ignored:
        print(f"{RED}ERROR{RESET}: router file {router_file} is ignored by git",
              file=sys.stderr)
        return 2
    try:
        all_extras = load_extra_cases(Path(args.extra_cases)) if args.extra_cases else []
    except (OSError, ValueError) as exc:
        print(f"{RED}ERROR{RESET}: --extra-cases: {exc}", file=sys.stderr)
        return 2

    if args.skill:
        if not (SKILLS_DIR / args.skill).is_dir():
            print(f"{RED}ERROR{RESET}: no skill directory "
                  f"{SKILLS_DIR / args.skill}", file=sys.stderr)
            return 2
        selected = [args.skill]
    elif args.changed:
        selected = changed_routing_skills(args.base)
    elif args.cluster:
        selected = list(OVERLAP_CLUSTERS[args.cluster])
    else:
        selected = list_skills_with_triggers()

    if args.all:
        extras = all_extras
    elif args.cluster:
        extras = [c for c in all_extras if c["cluster"] == args.cluster]
    else:
        chosen = set(selected)
        extras = [c for c in all_extras
                  if c["skill"] in chosen or chosen & set(OVERLAP_CLUSTERS[c["cluster"]])]

    # ONE snapshot. The router, every SKILL.md and the selected cases are read
    # here, once; the prompt, the vocabulary, the cases and the provenance
    # check all come from these bytes, and the run sends exactly the strings
    # scanned. Rebuilding anything after the check let a file change between
    # scan and send: MEASURED 2026-10-29, a router rewritten right after the
    # scan was the router sent.
    try:
        snap = competitive_snapshot(router_file, selected)
    except ProvenanceError as exc:
        print(f"{RED}ERROR{RESET}: not sending - provenance not established: {exc}.",
              file=sys.stderr)
        return 3
    except (InvalidSkillMetadata, ValueError, OSError) as exc:
        print(f"{RED}ERROR{RESET}: {exc}", file=sys.stderr)
        return 2
    skills = list(snap.cases_by_skill)
    # Named, never dropped: a selected skill with no case measured nothing,
    # and `--strict` fails on it, `--changed` included.
    unmeasured_skills = [s for s in selected if s not in snap.cases_by_skill]
    cases = build_competitive_cases(snap.cases_by_skill, extras)
    if not cases:
        if unmeasured_skills:
            print(f"{YELLOW}Selected but unmeasured (no triggers.json cases): "
                  f"{', '.join(unmeasured_skills)}{RESET}", file=sys.stderr)
            return 1 if args.strict else (0 if args.changed else 2)
        print(f"{YELLOW}No cases selected{RESET}", file=sys.stderr)
        return 0 if args.changed else 2
    system = snap.system
    payload = "\n".join([system] + [c["user"] for c in cases])
    payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # Fail closed on provenance, bound to the bytes captured above: every
    # captured source must equal its blob at HEAD. A mismatch, or a source not
    # in HEAD, refuses unless `--allow-uncommitted` authorises this run (it is
    # named and recorded). A git error, a symlink or a source outside the
    # checkout refuses ALWAYS: an answer that could not be established is not
    # a clean one.
    try:
        dirty = provenance_mismatches(snap.captured)
    except ProvenanceError as exc:
        print(f"{RED}ERROR{RESET}: not sending - provenance not established: {exc}.",
              file=sys.stderr)
        return 3
    if dirty and args.allow_uncommitted:
        print(f"{YELLOW}--allow-uncommitted: sending {len(dirty)} source(s) that "
              f"differ from HEAD: {', '.join(dirty)}{RESET}", file=sys.stderr)
    state, reason = egress_state(
        payload, build_denylist(get_data_root()),
        None if args.allow_uncommitted else dirty,
    )
    if state != EGRESS_CLEAR:
        print(f"{RED}ERROR{RESET}: not sending - {reason}.", file=sys.stderr)
        return 3

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"{RED}ERROR{RESET}: ANTHROPIC_API_KEY not set; cannot run the LLM judge.", file=sys.stderr)
        return 3
    try:
        import anthropic
    except ImportError:
        print(f"{RED}ERROR{RESET}: anthropic SDK not installed.", file=sys.stderr)
        return 3
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], default_headers={"User-Agent": user_agent()})
    model = claude_models.resolve(args.model, default_family=DEFAULT_FAMILY)

    vocabulary = snap.vocabulary
    results, api_calls = run_competitive(client, model, system, cases, vocabulary)
    for r in results:
        r["invalid"] = r["answer"] is not None and r["answer"] not in vocabulary
    scores = score_competitive(results, skills)
    # As in the default mode: a skill with no judged case is unmeasured, not breached.
    breached = [s["skill"] for s in scores["skills"] if s["cases"] and s["rate"] < args.threshold]
    report = {
        "mode": "competitive",
        "model": model,
        "router_file": str(router_file.relative_to(ROOT)),
        "router_bytes": len(snap.captured[router_file]),
        "payload_sha256": payload_sha256,
        "allow_uncommitted": bool(args.allow_uncommitted),
        "dirty_sources": dirty,
        **{k: scores[k] for k in ("overall_rate", "total_passed", "total_cases",
                                  "unmeasured_cases")},
        "invalid_answers": sum(1 for r in results if r["invalid"]),
        "unmeasured_skills": unmeasured_skills,
        "api_calls": api_calls,
        "by_kind": scores["by_kind"],
        "clusters": scores["clusters"],
        "skills": scores["skills"],
        "breached": breached,
        "cases": [{k: r[k] for k in ("source", "skill", "cluster", "query",
                                     "expected", "answer", "ok")} for r in results],
    }
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_competitive_report({**report, "_reasons": [r["reason"] for r in results],
                                  "_threshold": args.threshold})
    if args.strict and (report["overall_rate"] < args.threshold or breached
                        or report["unmeasured_cases"] or unmeasured_skills):
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
