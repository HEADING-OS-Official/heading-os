#!/usr/bin/env python3
"""audit-skills.py - CAPTURE the skill population, never hand-write it.

Usage:
    python scripts/dev/audit-skills.py                       # summary to stdout
    python scripts/dev/audit-skills.py --json out/audit.json
    python scripts/dev/audit-skills.py --markdown out/audit.md
    python scripts/dev/audit-skills.py --out-dir <dir>       # writes both
    python scripts/dev/audit-skills.py --outputs             # writes both under
                                                             # data_path("skill-audit")

WHY THIS EXISTS. An inventory over ninety-odd skills that a person types out is
wrong the week after it is written, and the wrongness is invisible: nothing
diffs a paragraph against the tree it describes. This repository already treats
a CAPTURED contract as the only kind worth having
(`scripts/dev/capture-herdr-contract.py`). Same shape here: every row below is
read off the corpus on the run that prints it, and the markdown is rendered from
the same capture the JSON carries, so the two cannot disagree.

WHAT IT REUSES rather than re-parsing. `scripts/skill-metadata-check.py` owns
frontmatter parsing, the size budget and the triggers.json corpus verdict;
`scripts/context-floor-audit.py` owns the description-bytes measurement and the
always-on/path-scoped rule split; `get_routing_destination()` over
`config/routing-map.yaml` owns classification. A second copy of any of those is
the copy that stops being fixed.

WHAT ITS METHOD CANNOT ESTABLISH, said here rather than left to be inferred:

  * It reads SKILL.md bodies. A skill that reaches a script, an agent or another
    skill only from inside its own `references/` prose is not seen as reaching
    it. `references/` files are inventoried by name and size, not scanned for
    edges.
  * `allowed-tools` is a GRANT. It says what the harness will let the skill call,
    never what the skill confines itself to. Nothing here treats it as a sandbox.
  * The path-scoped-rule column answers "which rules would this skill's declared
    paths load", by glob. `fnmatch` lets `*` cross a slash, so a `scripts/**`
    pattern and a `scripts/*` pattern are indistinguishable to it. It over-reports
    rather than under-reports, deliberately.
  * The plugin half reads the machine's plugin cache. Whether a cached plugin is
    the one the loader actually ran is settled by `installed_plugins.json` and by
    `enabledPlugins` in the checkout's `.claude/settings.json`; both are read and
    reported, and a plugin present in the cache but absent from `enabledPlugins`
    is reported as disabled, not as installed.
  * Nothing here observes the harness's own skill LISTING. Description bytes are
    the corpus figure; what survives the listing budget is a harness behaviour
    this script cannot see, and `--markdown` says so where the number is printed.

Tests: none since the 2026-09-21 test-suite split. The rule the parked check
enforced still stands: every figure this script prints is captured from the
tree, never typed, and a number written by hand here is a defect.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils.workspace import (  # noqa: E402
    get_default_tz,
    routing_verdict,
)
from scripts.utils.markdown import (  # noqa: E402
    FM_OK,
    parse_frontmatter_strict,
    split_frontmatter,
)

# skill-metadata-check.py and context-floor-audit.py are kebab-case CLI scripts,
# so neither can be imported by name.
import importlib.util  # noqa: E402
import yaml  # noqa: E402


def _load_kebab_module(name: str, path: Path):
    """Import a kebab-case CLI script as a module.

    `scripts/skill-metadata-check.py` owns the frontmatter parser, the size
    budget and the corpus verdict this audit reports. Re-deriving any of them
    here would be a second copy of a gate that CI enforces, and the second copy
    is the one that stops being fixed. Hyphens are illegal in module names, so
    the import goes through the loader directly.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable on a sane tree
        raise SystemExit(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_meta = _load_kebab_module("skill_metadata_check",
                           ROOT / "scripts" / "skill-metadata-check.py")
_floor = _load_kebab_module("context_floor_audit",
                            ROOT / "scripts" / "context-floor-audit.py")

SKILLS_DIR = ROOT / ".claude" / "skills"
RULES_DIR = ROOT / ".claude" / "rules"
AGENTS_DIR = ROOT / ".claude" / "agents"

# A corpus this small is not the workspace's skill library; it is a broken walk,
# an empty checkout, or a path built against the wrong root. Reporting "0 skills,
# 0 collisions" over it would be a clean bill of health for a tree nobody read.
CORPUS_FLOOR = 40

#: `router: manual` skills that omit `disable-model-invocation` ON PURPOSE, so
#: another session or an orchestrating agent can call them through the Skill
#: tool; `.claude/rules/skill-router.md` names both. Not a defect.
CALLABLE_FROM_OUTSIDE = frozenset({"brain-audit", "handover"})

SCRIPT_RE = re.compile(r"\bscripts/[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh|ps1)")
SKILL_REF_RE = re.compile(r"(?<![\w/])/([a-z][a-z0-9-]{2,})\b")
MCP_TOOL_RE = re.compile(r"mcp__([a-z0-9_]+)__")
REFERENCE_RE = re.compile(r"\breference/[A-Za-z0-9_][A-Za-z0-9_./-]*\.md")
#: `data-path.py`, `data_path` and `data_read` are the registry front door and
#: count as overlay signals too: since 2026-09-25 (HOS-5 section 6) a skill names
#: a data address through them rather than through a deleted helper, and a
#: regex that knew only the helpers read every converted skill as writing
#: nothing.
OVERLAY_HELPER_RE = re.compile(
    r"\b(get_data_root|get_data_[a-z_]+_dir|"
    r"get_knowledge_dir|get_crm_dir|get_threads_dir|resolve_config_with_example|"
    r"data_path|data_read|data-path\.py)\b"
)

# A mention of another skill is a COMPOSITION only when the sentence hands work
# to it. The router files are full of the opposite sentence ("do NOT use for X,
# use /other"), and counting those as edges would make every skill adjacent to
# every skill it is distinguished from.
NEGATIVE_MENTION_RE = re.compile(
    r"(?i)\b(do not|don't|never|not for|instead|rather than|unless|"
    r"exclusion|disambiguat)\b|->"
)
POSITIVE_MENTION_RE = re.compile(
    r"(?i)\b(invoke|invokes|invoking|call|calls|run|runs|append|appends|"
    r"dispatch|dispatches|compose|composes|escalat|hand off|hands off|delegate|"
    r"feeds into|then )\b"
)


# ============================================================
# Rules
# ============================================================

def load_rules(root: Path) -> dict:
    """Split .claude/rules into always-on and path-scoped, with their globs.

    The always-on test is `context-floor-audit._is_always_on`, IMPORTED rather
    than re-implemented: it already carries the two corrections that make it
    correct (a fence with trailing whitespace read as no frontmatter at all, and
    a `paths:` block that must terminate at the next top-level key rather than
    at the end of the frontmatter). A local copy of that predicate would be the
    copy that stops being fixed.

    The globs themselves are parsed here, because `measure_rules` reports bytes
    and does not carry the patterns.
    """
    always, scoped = [], []
    rules_dir = root / ".claude" / "rules"
    for path in sorted(rules_dir.glob("*.md")) if rules_dir.is_dir() else []:
        text = path.read_text(encoding="utf-8", errors="replace")
        row = {"rule": path.name, "bytes": len(text.encode("utf-8"))}
        if _floor._is_always_on(text):
            always.append(row)
            continue
        patterns: list[str] = []
        # `split_frontmatter`, not `text.split("---", 2)`: a rule whose
        # frontmatter carries `---` inside a scalar had its block truncated, and
        # the `paths:` key could fall on the far side of the cut. The rule then
        # reported as path-scoped-with-no-paths, which reads identically to a
        # rule that genuinely declares none.
        block, _body, _kind = split_frontmatter(text)
        try:
            loaded = yaml.safe_load(block) if block is not None else {}
            patterns = [str(p) for p in ((loaded or {}).get("paths") or [])]
        except (AttributeError, yaml.YAMLError):
            patterns = []
        row["paths"] = patterns
        scoped.append(row)
    return {"always_on": always, "path_scoped": scoped}


def rules_matching(paths: list[str], scoped: list[dict]) -> list[str]:
    """Which path-scoped rules a set of declared paths would load.

    Over-reports on purpose: `fnmatch`'s `*` crosses a slash, so `scripts/*`
    and `scripts/**` are the same pattern here. A design that drops a rule it
    was told loads is worse than one that keeps a rule it did not need.

    That licence covers over-reporting INSIDE the tree a pattern names, and
    nothing wider. The prefix branch below used to read
    `pat.replace("**", "").rstrip("/*")`, which was a character SET where a
    separator was meant. MEASURED 2026-09-09 over every `paths:` entry in
    `.claude/rules/`: 22 patterns reach the `endswith("**")` branch, all of the
    shape `<dir>/**`, and the `replace` leaves exactly ONE `/` behind on every
    one of them. There is no run to strip and no `*` to strip, so the set was
    never the semantics; it was a slice written with the wrong tool. Its one
    live effect was to eat the trailing separator, turning `scripts/**` into the
    prefix `scripts`, which then also matched `scripts-old/x.py` -- a DIFFERENT
    tree, not a wider read of the same one.

    The literal head before the first wildcard is what the branch actually
    wants, and `split("*", 1)[0]` says so directly.

    One thing this does NOT fix, recorded so the next reader does not go looking
    for a consequence that was never there. On `a/**/b/**` the old form reduced
    to the prefix `a//b`, which matches nothing, but the function's ANSWER was
    unchanged: the `pat + "/*"` fnmatch arm above already matched, because
    fnmatch's `*` crosses a slash. The prefix arm was simply dead on that shape.
    MEASURED 2026-09-09 against the pre-fix code, which returns the same
    `['r.md']` there as this does. The sibling-tree case is the only behaviour
    that moves.
    """
    hits = []
    for rule in scoped:
        for pattern in rule["paths"]:
            pat = pattern.rstrip("/")
            # The literal directory head, separator intact: `scripts/**` -> `scripts/`.
            prefix = pat.split("*", 1)[0]
            if any(fnmatch.fnmatch(p.rstrip("/"), pat)
                   or fnmatch.fnmatch(p.rstrip("/"), pat + "/*")
                   or (pat.endswith("**")
                       # `p + "/"`: a declared path naming the bare directory
                       # (`scripts` against `scripts/**`) matches neither
                       # fnmatch branch, and keeping the separator in `prefix`
                       # would otherwise drop it.
                       and (p.rstrip("/").startswith(prefix)
                            or p.rstrip("/") + "/" == prefix))
                   for p in paths if p):
                hits.append(rule["rule"])
                break
    return sorted(set(hits))


# ============================================================
# Per-skill capture
# ============================================================

def _dir_inventory(directory: Path, root: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            rows.append({
                "path": str(path.relative_to(root)),
                "bytes": path.stat().st_size,
            })
    return rows


def scan_skill(skill_dir: Path, root: Path, scoped_rules: list[dict],
               agent_names: list[str]) -> dict:
    """Everything this audit records about one local skill."""
    skill_md = skill_dir / "SKILL.md"
    frontmatter, parse_error = _meta.parse_frontmatter(skill_md)
    body = ""
    size_lines = size_bytes = 0
    if skill_md.is_file():
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        size_lines = len(text.splitlines())
        size_bytes = len(text.encode("utf-8"))
        # Same reader as everywhere else. The character form put the tail of a
        # truncated frontmatter block INTO the body, so a skill with `---` in a
        # scalar had its measured body size inflated by its own metadata.
        _block, body, kind = split_frontmatter(text)
        if kind != FM_OK:
            body = text
    # The budget thresholds and the corpus verdict come from the CI gate itself,
    # never from constants re-typed here. `check_skill` is deliberately NOT used:
    # it resolves its `path` field against `get_workspace_root()`, a constant tied
    # to the real checkout, and this function must work over any root it is handed
    # (see .claude/rules development note: derive the tree from the checkout under
    # test, never from a constant). The two helpers below take no root at all.
    size_status = _meta.classify_size(size_lines, size_bytes)
    corpus_verdict: dict = {
        "corpus_issues": [], "has_valid_corpus": False,
        "is_auto_routable": False, "triggers_status": "UNKNOWN",
    }
    _meta._classify_corpus(corpus_verdict, skill_dir,
                           frontmatter if frontmatter else None, frozenset())

    routing = frontmatter.get("x-heading-routing") or {}
    capability = frontmatter.get("x-heading-capability") or {}
    if not isinstance(routing, dict):
        routing = {}
    if not isinstance(capability, dict):
        capability = {}

    description = str(frontmatter.get("description", "") or "")
    references = _dir_inventory(skill_dir / "references", root)
    own_scripts = _dir_inventory(skill_dir / "scripts", root)

    workspace_scripts = sorted({m.group(0) for m in SCRIPT_RE.finditer(body)})
    ref_files = sorted(set(REFERENCE_RE.findall(body)))
    mcp = sorted(set(MCP_TOOL_RE.findall(body)))
    agents = sorted({a for a in agent_names if a in body})

    triggers_json = skill_dir / "triggers.json"
    corpus_cases = 0
    if triggers_json.is_file():
        try:
            loaded = json.loads(triggers_json.read_text(encoding="utf-8"))
            corpus_cases = len(loaded) if isinstance(loaded, list) else 0
        except (OSError, json.JSONDecodeError):
            corpus_cases = -1

    declared_paths = [r["path"] for r in references] + \
        [r["path"] for r in own_scripts] + workspace_scripts + ref_files


    rel = str(skill_md.relative_to(root))
    return {
        "name": skill_dir.name,
        "path": rel,
        "category": str(routing.get("category", "") or ""),
        "router": str(routing.get("router", "") or ""),
        "disable_model_invocation": frontmatter.get("disable-model-invocation") is True,
        "disable_model_invocation_declared": "disable-model-invocation" in frontmatter,
        "user_invocable": frontmatter.get("user-invocable", True) is not False,
        "auto_routable": _meta.is_auto_routable(frontmatter) if frontmatter else False,
        "description": description,
        "description_bytes": len(description.encode("utf-8")),
        "routing_triggers": [str(t) for t in (routing.get("triggers") or [])],
        "routing_exclusions": [str(t) for t in (routing.get("exclusions") or [])],
        "compound": str(routing.get("compound", "") or ""),
        "allowed_tools": frontmatter.get("allowed-tools"),
        "model": frontmatter.get("model"),
        "capability_fields": sorted(k for k in ("what", "how", "when")
                                    if capability.get(k)),
        "size_lines": size_lines,
        "size_bytes": size_bytes,
        "size_status": size_status,
        "parse_error": parse_error,
        "references": references,
        "reference_bytes": sum(r["bytes"] for r in references),
        "own_scripts": own_scripts,
        "workspace_scripts": workspace_scripts,
        "workspace_references": ref_files,
        "agents": agents,
        "mcp_servers": mcp,
        "path_scoped_rules": rules_matching(declared_paths, scoped_rules),
        "overlay_helpers": sorted(set(OVERLAY_HELPER_RE.findall(body))),
        "writes_overlay": bool(OVERLAY_HELPER_RE.search(body)),
        "classification": routing_verdict(rel, tree="engine"),
        "triggers_json": {
            "present": triggers_json.is_file(),
            "bytes": triggers_json.stat().st_size if triggers_json.is_file() else 0,
            "cases": corpus_cases,
            "status": corpus_verdict["triggers_status"],
            "issues": corpus_verdict["corpus_issues"],
        },
        "_body": body,
    }


def scan_local(root: Path) -> list[dict]:
    """Every local skill, or a refusal.

    A walk that finds fewer than CORPUS_FLOOR skills has not found the library;
    reporting a shape over it would be a clean verdict about a tree nobody read.
    """
    rules = load_rules(root)
    agent_names = sorted(p.stem for p in (root / ".claude" / "agents").glob("*.md")) \
        if (root / ".claude" / "agents").is_dir() else []
    rows = []
    skills_dir = root / ".claude" / "skills"
    for skill_dir in sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []:
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        if skill_dir.name == "archive":
            continue
        rows.append(scan_skill(skill_dir, root, rules["path_scoped"], agent_names))
    if len(rows) < CORPUS_FLOOR:
        raise SystemExit(
            f"audit-skills: found {len(rows)} skills under {skills_dir}, below the "
            f"floor of {CORPUS_FLOOR}. An inventory over a corpus this small is a "
            f"broken walk reported as a clean population, so this refuses rather "
            f"than rendering it."
        )
    _link_compositions(rows)
    return rows


def _link_compositions(rows: list[dict]) -> None:
    """Fill composes/composed_by/excludes, from the bodies already read."""
    names = {r["name"] for r in rows}
    for row in rows:
        row["composes"] = []
        row["excludes"] = []
        row["mentions"] = []
    for row in rows:
        seen: dict[str, str] = {}
        for line in row["_body"].splitlines():
            for match in SKILL_REF_RE.finditer(line):
                target = match.group(1)
                if target not in names or target == row["name"]:
                    continue
                if NEGATIVE_MENTION_RE.search(line):
                    kind = "exclude"
                elif POSITIVE_MENTION_RE.search(line):
                    kind = "compose"
                else:
                    kind = "mention"
                # A composition claim beats an exclusion beats a bare mention.
                rank = {"compose": 2, "exclude": 1, "mention": 0}
                if rank[kind] > rank.get(seen.get(target, "mention"), -1) \
                        or target not in seen:
                    seen[target] = kind
        for target, kind in sorted(seen.items()):
            {"compose": row["composes"], "exclude": row["excludes"],
             "mention": row["mentions"]}[kind].append(target)
    for row in rows:
        row["composed_by"] = sorted(
            other["name"] for other in rows if row["name"] in other["composes"]
        )
        del row["_body"]


# ============================================================
# Plugin population (a different population, with different rules)
# ============================================================

def _read_counted_source(path: Path) -> str:
    """Read a file whose bytes this INVENTORY counts, or fail naming it.

    A vanished-file investigation offered a sweep two remedies, and they are not
    interchangeable. Its check was parked in the 2026-09-21 test-suite split;
    the distinction below is the part worth keeping. A sweep HUNTING
    offenders routes through `read_sources`, because a file that is gone cannot
    violate anything and dropping it only narrows the corpus. A sweep computing
    a COUNT, a CHECKSUM or a completeness claim must retry once and then FAIL
    naming the file, because there the skip makes the answer WRONG rather than
    narrower.

    This is the second kind, and unambiguously: every byte read here lands in
    `size_bytes` and `description_bytes`, which are summed into `skill_count`,
    `enabled_skill_count` and `enabled_description_bytes` -- the context-budget
    figures the audit exists to report. A plugin skill silently dropped
    understates the operator's budget by an amount nothing records, and a budget
    that is quietly short is exactly the shape of answer that gets trusted.

    So this raises. The retry is what keeps a genuine race from being an outage:
    the cache is written by the plugin installer with an atomic replace, so a
    read landing in that window succeeds immediately on the second attempt.
    A file still absent after it is not a race, it is a cache being deleted
    underneath a running audit, and the right answer then is to say so and stop
    rather than to publish a number derived from half of it.
    """
    last: OSError | None = None
    for _attempt in range(2):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            last = exc
    raise RuntimeError(
        f"{path} vanished between the plugin-cache walk and the read, and was "
        f"still gone on the retry. This inventory sums the bytes it reads into "
        f"the context-budget totals it reports, so skipping the file would not "
        f"narrow the answer -- it would make it wrong by an unrecorded amount. "
        f"Re-run the audit."
    ) from last


def scan_plugins(root: Path, home: Path | None = None) -> dict:
    """The plugin-namespaced skills a session on THIS machine can reach.

    Kept apart from the local population throughout, because the two obey
    different rules: a local skill carries `x-heading-*` metadata this workspace
    defines and is classified by `config/routing-map.yaml`; a plugin skill
    carries none of that, is versioned and updated by someone else, and is
    enabled per project path rather than per file.
    """
    home = home or Path.home()
    cache = home / ".claude" / "plugins" / "cache"
    installed_file = home / ".claude" / "plugins" / "installed_plugins.json"
    settings_file = root / ".claude" / "settings.json"

    enabled: dict[str, bool] = {}
    if settings_file.is_file():
        try:
            enabled = json.loads(settings_file.read_text(encoding="utf-8")) \
                .get("enabledPlugins", {}) or {}
        except (OSError, json.JSONDecodeError):
            enabled = {}
    installed = {}
    if installed_file.is_file():
        try:
            installed = json.loads(installed_file.read_text(encoding="utf-8")) \
                .get("plugins", {}) or {}
        except (OSError, json.JSONDecodeError):
            installed = {}

    plugins = []
    if cache.is_dir():
        for marketplace in sorted(cache.iterdir()):
            if not marketplace.is_dir():
                continue
            for plugin_dir in sorted(marketplace.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                versions = sorted(p for p in plugin_dir.iterdir() if p.is_dir())
                if not versions:
                    continue
                version_dir = versions[-1]
                key = f"{plugin_dir.name}@{marketplace.name}"
                skills = []
                for skill_md in sorted(version_dir.rglob("SKILL.md")):
                    # ONE read, then parse the text in hand. This was two reads
                    # of the same path, so it carried two race windows, and the
                    # second one failed in the silent direction: on a vanished
                    # file `_meta.parse_frontmatter` returns `({}, "read
                    # failed: ...")` and the reason was assigned to `_err` and
                    # dropped, so the row was still appended -- counted, with an
                    # empty description and zero bytes.
                    text = _read_counted_source(skill_md)
                    fm = parse_frontmatter_strict(text)[0] or {}
                    desc = str(fm.get("description", "") or "")
                    skills.append({
                        "skill": fm.get("name") or skill_md.parent.name,
                        "invocation": f"{plugin_dir.name}:"
                                      f"{fm.get('name') or skill_md.parent.name}",
                        "path": str(skill_md),
                        "description_bytes": len(desc.encode("utf-8")),
                        "allowed_tools": fm.get("allowed-tools", None),
                        "disable_model_invocation":
                            fm.get("disable-model-invocation") is True,
                        "size_bytes": len(text.encode("utf-8")),
                        "size_lines": len(text.splitlines()),
                    })
                plugins.append({
                    "plugin": plugin_dir.name,
                    "marketplace": marketplace.name,
                    "version": version_dir.name,
                    "enabled": bool(enabled.get(key, False)),
                    "installed_record": key in installed,
                    "skill_count": len(skills),
                    "description_bytes": sum(s["description_bytes"] for s in skills),
                    "skills": skills,
                })

    user_skills = []
    user_dir = home / ".claude" / "skills"
    if user_dir.is_dir():
        for skill_md in sorted(user_dir.rglob("SKILL.md")):
            # Same treatment as the plugin loop above, and the same reason: this
            # is an inventory, not an offender hunt. `size_bytes` also came off a
            # separate `stat()` call here while the loop above measured the text
            # it had read, so one function reported one field two ways over two
            # more race windows. One read answers both.
            text = _read_counted_source(skill_md)
            fm = parse_frontmatter_strict(text)[0] or {}
            desc = str(fm.get("description", "") or "")
            user_skills.append({
                "skill": fm.get("name") or skill_md.parent.name,
                "path": str(skill_md),
                "description_bytes": len(desc.encode("utf-8")),
                "size_bytes": len(text.encode("utf-8")),
            })

    enabled_rows = [p for p in plugins if p["enabled"]]
    return {
        "plugins": plugins,
        "user_skills": user_skills,
        "enabled_plugin_count": len(enabled_rows),
        "enabled_skill_count": sum(p["skill_count"] for p in enabled_rows),
        "enabled_description_bytes": sum(p["description_bytes"] for p in enabled_rows),
        "disabled_in_settings": sorted(
            p["plugin"] for p in plugins if not p["enabled"]
        ),
        "cache_present": cache.is_dir(),
    }


# ============================================================
# Population shape
# ============================================================

def population_shape(rows: list[dict]) -> dict:
    auto = [r for r in rows if r["auto_routable"]]
    manual = [r for r in rows if not r["auto_routable"]]

    script_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for script in row["workspace_scripts"]:
            script_map[script].append(row["name"])

    reference_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for ref in row["workspace_references"]:
            reference_map[ref].append(row["name"])

    agent_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for agent in row["agents"]:
            agent_map[agent].append(row["name"])

    mcp_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for server in row["mcp_servers"]:
            mcp_map[server].append(row["name"])

    trigger_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for trigger in row["routing_triggers"]:
            key = re.sub(r"\s+", " ", trigger.strip().lower())
            if key and key != "manual":
                trigger_map[key].append(row["name"])

    buckets = {"<=100": 0, "101-250": 0, "251-400": 0, "401-500": 0, ">500": 0}
    for row in rows:
        n = row["size_lines"]
        if n <= 100:
            buckets["<=100"] += 1
        elif n <= 250:
            buckets["101-250"] += 1
        elif n <= 400:
            buckets["251-400"] += 1
        elif n <= 500:
            buckets["401-500"] += 1
        else:
            buckets[">500"] += 1

    by_category: dict[str, int] = defaultdict(int)
    for row in rows:
        by_category[row["category"] or "(none)"] += 1

    def shared(mapping):
        return {k: sorted(set(v)) for k, v in sorted(mapping.items())
                if len(set(v)) > 1}

    # Defects the capture can NAME on its own evidence. Nothing here is a
    # judgement call: each row is a disagreement between two things already in
    # the corpus, so it survives a re-run without anyone re-deciding it. A
    # hand-written findings list beside a generated table is the second source of
    # truth this whole script exists to avoid.
    defects: list[dict] = []
    for row in rows:
        if (row["router"] == "manual" and not row["disable_model_invocation"]
                and not row["disable_model_invocation_declared"]
                and row["name"] not in CALLABLE_FROM_OUTSIDE):
            defects.append({
                "skill": row["name"],
                "kind": "router-says-manual-but-the-harness-will-still-list-it",
                "detail": "x-heading-routing.router is 'manual', which "
                          ".claude/rules/skill-router.md says the harness "
                          "enforces with disable-model-invocation. That flag is "
                          "absent, so the skill IS in the session listing and "
                          "the model CAN invoke it from a message. Declare it, "
                          "`true` or a deliberate `false`.",
            })
        if row["size_status"] != "OK":
            defects.append({
                "skill": row["name"],
                "kind": f"size-budget-{row['size_status']}",
                "detail": f"{row['size_lines']} lines, {row['size_bytes']} bytes "
                          f"(warn at 16384, hard at 500 lines / 18432 bytes).",
            })
        if row["auto_routable"] and not row["triggers_json"]["present"]:
            defects.append({
                "skill": row["name"],
                "kind": "auto-routable-without-a-triggers-corpus",
                "detail": f"triggers_status={row['triggers_json']['status']}",
            })
        if row["parse_error"]:
            defects.append({"skill": row["name"], "kind": "frontmatter",
                            "detail": row["parse_error"]})
    ceo_only_claims = sorted(
        r["name"] for r in rows
        if "ceo-only" in r["description"].lower()
        or "not synced to exec" in r["description"].lower()
    )

    return {
        "skills": len(rows),
        "auto_routable": len(auto),
        "defects": sorted(defects, key=lambda d: (d["kind"], d["skill"])),
        "ceo_only_by_prose_only": ceo_only_claims,
        "explicit_only": len(manual),
        "by_category": dict(sorted(by_category.items())),
        "classification_counts": dict(sorted(
            (k, sum(1 for r in rows if r["classification"] == k))
            for k in {r["classification"] for r in rows}
        )),
        "size_buckets": buckets,
        "size_status_counts": dict(sorted(
            (k, sum(1 for r in rows if r["size_status"] == k))
            for k in {r["size_status"] for r in rows}
        )),
        "total_skill_md_bytes": sum(r["size_bytes"] for r in rows),
        "total_description_bytes": sum(r["description_bytes"] for r in rows),
        "auto_routable_description_bytes":
            sum(r["description_bytes"] for r in auto),
        "explicit_only_description_bytes":
            sum(r["description_bytes"] for r in manual),
        "total_reference_bytes": sum(r["reference_bytes"] for r in rows),
        "reference_file_count": sum(len(r["references"]) for r in rows),
        "skills_with_references": sum(1 for r in rows if r["references"]),
        "skills_with_triggers_json":
            sum(1 for r in rows if r["triggers_json"]["present"]),
        "shared_scripts": shared(script_map),
        "shared_references": shared(reference_map),
        "shared_agents": {k: sorted(set(v)) for k, v in sorted(agent_map.items())},
        "mcp_dependents": {k: sorted(set(v)) for k, v in sorted(mcp_map.items())},
        "trigger_collisions": shared(trigger_map),
        "composition_hubs": sorted(
            ((r["name"], len(r["composed_by"])) for r in rows if r["composed_by"]),
            key=lambda t: (-t[1], t[0]),
        ),
        "explicit_only_names": sorted(r["name"] for r in manual),
    }


def capture(root: Path, home: Path | None = None) -> dict:
    rows = scan_local(root)
    rules = load_rules(root)
    return {
        "captured": datetime.now(get_default_tz()).date().isoformat(),
        "root": str(root),
        "corpus_floor": CORPUS_FLOOR,
        "local_skills": rows,
        "shape": population_shape(rows),
        "rules": {
            "always_on": rules["always_on"],
            "path_scoped": rules["path_scoped"],
            "always_on_bytes": sum(r["bytes"] for r in rules["always_on"]),
            "path_scoped_bytes": sum(r["bytes"] for r in rules["path_scoped"]),
        },
        "plugins": scan_plugins(root, home),
    }


# ============================================================
# Rendering
# ============================================================

def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def render_markdown(data: dict) -> str:
    shape = data["shape"]
    day = data["captured"]
    out: list[str] = []
    w = out.append

    w(f"# Skill inventory, captured\n")
    w(f"Captured by `scripts/dev/audit-skills.py` on **MEASURED {day}** from "
      f"`{data['root']}`. Every figure on this page was read off the corpus by "
      f"the run that rendered it; nothing here is typed. Re-run the script to "
      f"re-derive it.\n")
    w("`allowed-tools` below is the GRANT the harness honours, not a limit the "
      "skill imposes on itself. A skill can decline to use a tool it is granted; "
      "nothing stops it using one it is granted.\n")

    w("## Population shape\n")
    w(_table(["measure", "value"], [
        ["local skills", shape["skills"]],
        ["auto-routable", shape["auto_routable"]],
        ["explicit-only (never routed from a message)", shape["explicit_only"]],
        ["skills with a `references/` directory", shape["skills_with_references"]],
        ["reference files under skills", shape["reference_file_count"]],
        ["skills with `triggers.json`", shape["skills_with_triggers_json"]],
        ["total SKILL.md bytes", shape["total_skill_md_bytes"]],
        ["total description bytes", shape["total_description_bytes"]],
        ["description bytes, auto-routable only",
         shape["auto_routable_description_bytes"]],
        ["description bytes, explicit-only",
         shape["explicit_only_description_bytes"]],
        ["total bytes under skills' `references/`", shape["total_reference_bytes"]],
    ]))
    w("")
    w(f"By category: {shape['by_category']}\n")
    w(f"By classification (`config/routing-map.yaml`): "
      f"{shape['classification_counts']}\n")
    w(f"SKILL.md line-count distribution: {shape['size_buckets']} "
      f"(budget: 500 lines AND 18432 bytes hard). "
      f"Budget verdicts: {shape['size_status_counts']}\n")

    w("### Explicit-only skills\n")
    w("`disable-model-invocation: true`, or `router: manual`. These never fire "
      "from a natural-language message.\n")
    w(", ".join(f"`/{n}`" for n in shape["explicit_only_names"]) + "\n")

    w("### Trigger-phrase collisions\n")
    w("Exact phrase, normalised for case and whitespace, appearing in two or "
      "more skills' `x-heading-routing.triggers`.\n")
    if shape["trigger_collisions"]:
        w(_table(["phrase", "skills"],
                 [[f"`{k}`", ", ".join(v)]
                  for k, v in shape["trigger_collisions"].items()]))
    else:
        w("None.")
    w("")
    w("This finds identical phrases only. Two skills whose triggers merely "
      "overlap in meaning (\"research\" and \"dig into\") do not appear here, "
      "and that is the larger population; the router's Exclusions column is "
      "where those are resolved.\n")

    w("### Most-shared scripts\n")
    top = sorted(shape["shared_scripts"].items(),
                 key=lambda kv: (-len(kv[1]), kv[0]))[:20]
    w(_table(["script", "skills", "which"],
             [[f"`{k}`", len(v), ", ".join(v)] for k, v in top]) if top
      else "None.")
    w("")

    w("### Most-shared workspace reference files\n")
    top_ref = sorted(shape["shared_references"].items(),
                     key=lambda kv: (-len(kv[1]), kv[0]))[:20]
    w(_table(["reference", "skills", "which"],
             [[f"`{k}`", len(v), ", ".join(v)] for k, v in top_ref]) if top_ref
      else "None.")
    w("")

    w("### Agents dispatched\n")
    w(_table(["agent", "skills"],
             [[f"`{k}`", ", ".join(v)]
              for k, v in shape["shared_agents"].items()]) or "None.")
    w("")

    w("### MCP servers named in skill bodies\n")
    w(_table(["server", "skills"],
             [[f"`{k}`", ", ".join(v)]
              for k, v in shape["mcp_dependents"].items()])
      if shape["mcp_dependents"] else "None.")
    w("")

    w("### Composition hubs (in-degree)\n")
    hubs = [t for t in shape["composition_hubs"] if t[1] > 1]
    w(_table(["skill", "composed by N skills"],
             [[f"`/{n}`", c] for n, c in hubs]) if hubs else "None.")
    w("")

    w("## Defects this capture can name\n")
    w("Each row is a disagreement between two things already in the corpus, not "
      "a judgement, so it survives a re-run without being re-decided. This is "
      "not a complete defect list: anything needing a reader's opinion is out of "
      "scope by construction.\n")
    if shape["defects"]:
        w(_table(["skill", "kind", "detail"],
                 [[f"`/{d['skill']}`", d["kind"], d["detail"]]
                  for d in shape["defects"]]))
    else:
        w("None.")
    w("")
    w("### Skills whose own description claims CEO-only\n")
    w(f"{len(shape['ceo_only_by_prose_only'])} skills say \"CEO-only\" or \"not "
      f"synced to exec\" in their `description`. A skill ships in the public "
      f"engine repository, so the claim is prose that nothing enforces; the "
      f"classification counts above say where `config/routing-map.yaml` "
      f"actually places them.\n")
    w(", ".join(f"`/{n}`" for n in shape["ceo_only_by_prose_only"]) + "\n")

    w("## Rules\n")
    w(f"Always-on: {len(data['rules']['always_on'])} files, "
      f"{data['rules']['always_on_bytes']} bytes. "
      f"Path-scoped: {len(data['rules']['path_scoped'])} files, "
      f"{data['rules']['path_scoped_bytes']} bytes.\n")
    w(_table(["always-on rule", "bytes"],
             [[f"`{r['rule']}`", r["bytes"]] for r in data["rules"]["always_on"]]))
    w("")

    w("## Plugin skills (a separate population)\n")
    plug = data["plugins"]
    w("A plugin skill carries none of this workspace's `x-heading-*` metadata, "
      "is versioned by someone else, and is enabled per project path in "
      "`.claude/settings.json` rather than classified by "
      "`config/routing-map.yaml`. The two populations are never merged in this "
      "report, because a rule that holds over one holds over neither by "
      "default.\n")
    w(_table(["plugin", "version", "enabled", "skills", "description bytes"],
             [[p["plugin"], p["version"], "yes" if p["enabled"] else "no",
               p["skill_count"], p["description_bytes"]]
              for p in plug["plugins"]]))
    w("")
    w(f"Enabled plugins: {plug['enabled_plugin_count']}; their skills: "
      f"{plug['enabled_skill_count']}; their description bytes: "
      f"{plug['enabled_description_bytes']}.\n")
    if plug["user_skills"]:
        w(_table(["user-level skill", "description bytes", "SKILL.md bytes"],
                 [[s["skill"], s["description_bytes"], s["size_bytes"]]
                  for s in plug["user_skills"]]))
        w("")

    w("## The full inventory\n")
    w(_table(
        ["skill", "cat", "route", "class", "lines", "bytes", "desc B",
         "refs", "scripts", "agents",
         "path rules", "triggers.json"],
        [[f"`/{r['name']}`", r["category"] or "-",
          "auto" if r["auto_routable"] else "manual",
          r["classification"], r["size_lines"], r["size_bytes"],
          r["description_bytes"], len(r["references"]),
          len(r["workspace_scripts"]),
          ", ".join(r["agents"]) or "-",
          ", ".join(r["path_scoped_rules"]) or "-",
          "yes" if r["triggers_json"]["present"] else "no"]
         for r in data["local_skills"]]))
    w("")

    w("### allowed-tools, verbatim\n")
    w(_table(["skill", "allowed-tools"],
             [[f"`/{r['name']}`",
               f"`{r['allowed_tools']}`" if r["allowed_tools"] else "(absent)"]
              for r in data["local_skills"]]))
    w("")

    w("### Composition edges\n")
    w(_table(["skill", "composes", "composed by"],
             [[f"`/{r['name']}`",
               ", ".join(f"/{c}" for c in r["composes"]) or "-",
               ", ".join(f"/{c}" for c in r["composed_by"]) or "-"]
              for r in data["local_skills"]
              if r["composes"] or r["composed_by"]]))
    w("")

    w("## What this capture does not establish\n")
    w("- It reads SKILL.md bodies only. A skill that names a script, an agent or "
      "another skill solely inside its own `references/` prose shows an empty "
      "cell here. Those files are inventoried by name and size, not scanned.\n"
      "- The composition classifier reads one LINE at a time and sorts a "
      "`/skill` mention into compose / exclude / bare mention by the verbs "
      "around it. A composition spread over two sentences on two lines is read "
      "as a bare mention.\n"
      "- Trigger collisions are exact-phrase only. Semantic overlap, which is "
      "the larger risk, is not measured here.\n"
      "- Path-scoped rule matching is by `fnmatch`, whose `*` crosses a slash. "
      "It over-reports.\n"
      "- Nothing here observes the harness's skill LISTING. The description "
      "bytes above are the corpus; how much of it survives the listing budget "
      "is a harness behaviour no file on disk records.\n")
    return "\n".join(out) + "\n"


# ============================================================
# CLI
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture the skill population as JSON + markdown."
    )
    parser.add_argument("--json", type=Path, help="write the capture as JSON")
    parser.add_argument("--markdown", type=Path, help="write the rendered table")
    parser.add_argument("--out-dir", type=Path,
                        help="write audit.json and audit.md into this directory")
    parser.add_argument("--outputs", action="store_true",
                        help="write both under the registry name `skill-audit`, "
                             "in a per-run <date>/ subdirectory")
    args = parser.parse_args()

    data = capture(ROOT)

    out_dir = args.out_dir
    if args.outputs:
        # The registry places the parent; each run gets its own <date>/.
        from scripts.utils.data_path import data_path
        out_dir = data_path("skill-audit") / data["captured"]

    targets: list[tuple[Path, str]] = []
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        targets.append((out_dir / "audit.json", json.dumps(data, indent=2) + "\n"))
        targets.append((out_dir / "audit.md", render_markdown(data)))
    if args.json:
        targets.append((args.json, json.dumps(data, indent=2) + "\n"))
    if args.markdown:
        targets.append((args.markdown, render_markdown(data)))

    for path, text in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        print(f"wrote {path}")

    if not targets:
        shape = data["shape"]
        print(f"{shape['skills']} local skills "
              f"({shape['auto_routable']} auto-routable, "
              f"{shape['explicit_only']} explicit-only), "
              f"{len(data['rules']['always_on'])} always-on rules, "
              f"{data['plugins']['enabled_skill_count']} skills from "
              f"{data['plugins']['enabled_plugin_count']} enabled plugins. "
              f"MEASURED {data['captured']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
