"""The YAML spec block of a prompt expansion: schema v1, presets, and a form check.

WHAT THE SPEC IS (HOS-366). Every archived prompt expansion carries one fenced
```` ```yaml ```` block whose mapping has `spec_version: 1`. It states the task
in fields a program can check: the objective, every one of the operator's asks
quoted from his words, the scope, the acceptance checks, and an
`open_questions` list that must be empty before execution starts.
`scripts/prompt-archive.py save` and `check` run `verdict()` on the expansion.

WHY. MEASURED 2026-11-17 on 100 archived prompts: 73 of 100 left a decision to
the executor, 27 of 100 dropped something the operator asked for, and 50 of 65
code tasks had no stop condition. A missing sentence in prose is visible to
nobody; an empty required field or a non-empty `open_questions` is visible to
this module.

TWO TIERS. `minimal` for every task: spec_version, tier, task, objective, asks,
scope, acceptance, open_questions. `full` for a task that goes to a yard or
changes code, which adds deliverables, constraints (only the keys that differ
from the standing rules), decisions, steps, preconditions and stop_if.

PRESETS. A routine task uses `{spec_version: 1, preset: <name>, task, asks,
params}` instead of a hand-written block. Step 1 ships exactly two,
`backup-push` and `merge-delete-yard`; their literal content is `PRESETS`. In
step 1 nothing fills their `{yard}` and `{branch}` placeholders: a preset block
is checked through its params schema only.

`run` IS NEVER EXECUTED HERE. An acceptance entry's `run` is stored and checked
for shape; running it is a later step, from an allowlist with list arguments.

Shape is checked with `jsonschema.Draft202012Validator`, the cross-field rules
in Python. YAML is read with `yaml.safe_load` only. The validation order and
every reason string are fixed by the plan of 2026-11-17 (registry name `plans`,
`2026-11-17-yaml-spec-step1`), section "Exact specification (schema v1)".
"""

from __future__ import annotations

import re
import unicodedata

import yaml
from jsonschema import Draft202012Validator

_STR = {"type": "string", "minLength": 1}


def _str_list(min_items: int = 0) -> dict:
    schema: dict = {"type": "array", "items": _STR}
    if min_items:
        schema["minItems"] = min_items
    return schema


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_ASKS = {"type": "array", "minItems": 1,
         "items": _obj({"said": {"type": "string", "minLength": 1, "pattern": "\\S"},
                        "where": _STR},
                       ["said", "where"])}

_COMMON = {
    "spec_version": {"const": 1},
    "tier": {"enum": ["minimal", "full"]},
    "task": {"type": "string", "pattern": "^([A-Z][A-Z0-9]+-[0-9]+|none)$"},
    "objective": _STR,
    "asks": _ASKS,
    "scope": _obj({"in": _str_list(1), "out": _str_list()}, ["in", "out"]),
    "acceptance": {
        "type": "array", "minItems": 1,
        "items": _obj({"check": _STR, "by": {"enum": ["program", "human"]},
                       "run": _STR, "expect": _STR},
                      ["check", "by", "expect"]),
    },
    "open_questions": _str_list(),
}

_FULL_ONLY = {
    "deliverables": {
        "type": "array", "minItems": 1,
        "items": _obj({"kind": {"enum": ["file", "commit", "message-draft", "report",
                                         "lific", "answer", "other"]},
                       "where": _STR},
                      ["kind", "where"]),
    },
    "constraints": _obj({
        "commit": {"enum": ["forbidden", "on_word", "relay_only"]},
        "push": {"enum": ["forbidden", "on_word"]},
        "send": {"enum": ["forbidden", "draft_only", "on_word"]},
        "tests": _STR,
        "live": {"enum": ["forbidden", "helm_only"]},
        "delete": {"enum": ["forbidden", "on_word"]},
    }, []),
    "decisions": {
        "type": "array",
        "items": _obj({"question": _STR, "chosen": _STR,
                       "by": {"enum": ["operator", "helm_proposed"]}},
                      ["question", "chosen", "by"]),
    },
    "steps": _str_list(1),
    "preconditions": _str_list(),
    "stop_if": _str_list(),
}

_COMMON_REQUIRED = ["spec_version", "tier", "task", "objective", "asks", "scope",
                    "acceptance", "open_questions"]

SCHEMA_MINIMAL = _obj({**_COMMON, **_FULL_ONLY}, list(_COMMON_REQUIRED))
SCHEMA_FULL = _obj({**_COMMON, **_FULL_ONLY}, _COMMON_REQUIRED + list(_FULL_ONLY))

SCHEMA_PRESET = _obj({
    "spec_version": {"const": 1},
    "preset": {"enum": ["backup-push", "merge-delete-yard"]},
    "task": _COMMON["task"],
    "asks": _ASKS,
    "params": {"type": "object"},
}, ["spec_version", "preset", "task", "asks", "params"])

#: The params schema of each preset, merged into `SCHEMA_PRESET` for that preset.
PRESET_PARAMS = {
    "backup-push": _obj({}, []),
    "merge-delete-yard": _obj({"yard": {"type": "string", "pattern": "^w[0-9A-Za-z]+$"},
                               "branch": _STR},
                              ["yard", "branch"]),
}

#: The literal content of each preset. `{yard}` and `{branch}` are placeholders
#: nothing fills in step 1.
PRESETS = {
    "backup-push": {
        "objective": ("commit every pending change and push both repositories to "
                      "their private origin/main through scripts/push-all.py"),
        "scope": {"in": ["engine repository", "data repository"],
                  "out": ["changes not already pending"]},
        "acceptance": [
            {"check": "both repositories in sync with origin/main", "by": "program",
             "run": "git rev-list --count origin/main..main",
             "expect": "0 in both repositories"},
        ],
        "constraints": {"commit": "on_word", "push": "on_word"},
    },
    "merge-delete-yard": {
        "objective": ("merge branch {branch} of yard {yard} into main after the "
                      "M030.1 gates, then delete the yard"),
        "scope": {"in": ["engine main", "yard {yard}"], "out": ["push"]},
        "acceptance": [
            {"check": "review-branch finds nothing blocking", "by": "program",
             "run": ".venv/bin/python scripts/review-branch.py {branch} --intent-file <brief>",
             "expect": "0 findings need fixing"},
            {"check": "branch merged into main", "by": "program",
             "run": "git branch --merged main", "expect": "lists {branch}"},
        ],
        "constraints": {"commit": "relay_only", "push": "forbidden"},
    },
}

_SPEC_LINE = re.compile(r"^spec_version\s*:", re.MULTILINE)


def extract(expansion: str) -> str | None:
    """The raw text of the spec block, or None when the expansion has none.

    The first fenced block whose opening line is exactly ```` ```yaml ```` and
    whose raw text has a line starting `spec_version:`. It ends at the first
    later line that is exactly ```` ``` ```` after trailing whitespace is
    removed, or at the end of the expansion. Selected on raw text, parsed later,
    so a block that does not parse is still the spec block.
    """
    lines = (expansion or "").splitlines()
    i = 0
    while i < len(lines):
        if lines[i] != "```yaml":
            i += 1
            continue
        end = i + 1
        while end < len(lines) and lines[end].rstrip() != "```":
            end += 1
        block = "\n".join(lines[i + 1:end])
        if _SPEC_LINE.search(block):
            return block
        i = end + 1
    return None


def norm(text: str) -> str:
    """NFC, every whitespace run one space, trimmed. Case and quotes are kept."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def _path(error) -> str:
    parts = []
    for part in error.absolute_path:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        else:
            parts.append(("." if parts else "") + str(part))
    return "".join(parts) or "(top)"


def _schema_reasons(schema: dict, data: dict) -> list[str]:
    errors = [(_path(e), e.message) for e in Draft202012Validator(schema).iter_errors(data)]
    return [f"{path}: {message}" for path, message in sorted(errors)]


def _asks_reasons(data: dict, original: str) -> list[str]:
    haystack = norm(original or "")
    return [f"asks[{i}].said not in the operator's words"
            for i, ask in enumerate(data["asks"])
            if norm(ask["said"]) not in haystack]


def validate(block_text: str, original: str) -> tuple[str, list[str]]:
    """`("valid", [])` or `("invalid", reasons)`, in the plan's fixed order."""
    # Every exception safe_load raises is a parse failure, not only YAMLError:
    # `objective: 2026-99-99` raises ValueError from the timestamp constructor
    # (MEASURED 2026-11-17), and a crash here would stop `save` archiving.
    try:
        data = yaml.safe_load(block_text)
    except Exception as exc:  # noqa: BLE001 - reported as the invalid verdict's reason
        first = (str(exc).splitlines() or [type(exc).__name__])[0]
        return "invalid", [f"yaml: {first}"]
    if not isinstance(data, dict):
        return "invalid", ["the spec block is not a mapping"]
    version = data.get("spec_version")
    if type(version) is not int or version != 1:
        return "invalid", ["spec_version must be 1"]

    if "preset" in data:
        name = data["preset"]
        if not isinstance(name, str) or name not in PRESETS:
            return "invalid", [f"unknown preset {name}"]
        schema = dict(SCHEMA_PRESET)
        schema["properties"] = {**SCHEMA_PRESET["properties"], "params": PRESET_PARAMS[name]}
        reasons = _schema_reasons(schema, data)
        if reasons:
            return "invalid", reasons
        reasons = _asks_reasons(data, original)
        return ("invalid", reasons) if reasons else ("valid", [])

    tier = data.get("tier")
    if not isinstance(tier, str) or tier not in ("minimal", "full"):
        return "invalid", ["tier: must be minimal or full"]
    reasons = _schema_reasons(SCHEMA_FULL if tier == "full" else SCHEMA_MINIMAL, data)
    if reasons:
        return "invalid", reasons

    if data["open_questions"]:
        reasons.append("open_questions must be empty")
    for i, entry in enumerate(data["acceptance"]):
        if entry["by"] == "program" and "run" not in entry:
            reasons.append(f"acceptance[{i}].run required when by is program")
        if entry["by"] == "human" and "run" in entry:
            reasons.append(f"acceptance[{i}].run must be absent when by is human")
    reasons.extend(_asks_reasons(data, original))
    return ("invalid", reasons) if reasons else ("valid", [])


def verdict(expansion: str, original: str) -> tuple[str, list[str]]:
    """`("none", [])` when the expansion has no spec block, otherwise `validate`."""
    block = extract(expansion)
    if block is None:
        return "none", []
    return validate(block, original)
