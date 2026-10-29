#!/usr/bin/env python3
"""Generate the skill router from each SKILL.md's x-heading-routing frontmatter.

The router is a build artifact. Each skill owns its routing row in its own SKILL.md
frontmatter under ``x-heading-routing`` (category, triggers[], exclusions[],
router, optional label). This script renders two layers:

  1. The always-on ``.claude/rules/skill-router.md``, WHOLE: a compact routing
     policy (``POLICY_TEMPLATE`` below) plus a collision map, one line per
     overlap cluster in ``scripts.utils.router_payload.OVERLAP_CLUSTERS``, derived
     from the members' exclusions (``collision_line``). No per-skill table: the
     harness already lists every model-invocable skill's description, and the
     table repeated it on every session (2026-10-29, measured by the competitive
     mode of ``skill-trigger-test.py``).
  2. Per-category detail files ``reference/skill-router/<category>.md`` carrying the
     full Skill | Triggers | Exclusions table for EVERY skill, auto and manual
     alike, read on demand for disambiguation.

The policy prose lives in this file, so editing the rule means editing
``POLICY_TEMPLATE`` and regenerating; a hand edit of the rule is drift.
``--check`` regenerates BOTH layers in memory and diffs against disk, failing on
any content drift, so the rule can no longer disagree with its skills.

Usage:
    python scripts/generate-skill-router.py            # default: write the rule + per-category files
    python scripts/generate-skill-router.py --check    # regen both layers -> diff; exit 1 on drift (CI / pre-commit)
    python scripts/generate-skill-router.py --split-by-category   # explicit synonym of the default write
    python scripts/generate-skill-router.py --flat     # print the legacy flat monolith to stdout (debug / semantics proof); no write
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import RED, GREEN, CYAN, YELLOW, RESET  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402
from scripts.utils import markdown as md  # noqa: E402
from scripts.utils.markdown import parse_frontmatter_strict  # noqa: E402
from scripts.utils.repo_files import read_sources  # noqa: E402
from scripts.utils.router_payload import OVERLAP_CLUSTERS  # noqa: E402

# ============================================================
# Configuration
# ============================================================

ROOT = get_workspace_root()
SKILLS_DIR = ROOT / ".claude" / "skills"
ROUTER_FILE = ROOT / ".claude" / "rules" / "skill-router.md"
# The per-category detail tables live here (engine-routed, shareable), read on
# demand; the always-on rule carries the policy and the collision map only.
CATEGORY_FILE_DIR = ROOT / "reference" / "skill-router"

# Skill subdirs that are not actual skills (archived, internal).
SKIP_SUBDIRS = {"archive", "_archive", ".cache"}

# Fixed category order in the rendered registry (matches the hand-written order today).
CATEGORY_ORDER = ["Intel", "Communication", "Content", "CRM", "Design", "Strategy", "Operations"]

# Cell separators. The migration splits on exactly these; the generator joins on exactly
# these, so join(sep, split(sep, cell)) reproduces the cell modulo separator whitespace.
TRIGGER_SEP = ", "
EXCL_SEP = "; "

ROUTING_KEY = "x-heading-routing"

TABLE_HEADER = "| Skill | Triggers | Exclusions |"
TABLE_SEP = "|---|---|---|"

FIX_IT_SNIPPET = """\
x-heading-routing:
  category: <Intel|Communication|Content|CRM|Design|Strategy|Operations>
  triggers: ["<trigger phrase>", "<another>"]
  exclusions: ["<signal> -> /<other-skill>"]   # or ["N/A"]
  router: auto                                   # or manual (NEVER auto-trigger skills)
  # label: "/name [args]"                        # only when the Skill cell is not the plain /name"""


# ============================================================
# Frontmatter parsing
# ============================================================

def _existing_text(path: Path) -> str | None:
    """The file's text, or None when it is not there.

    Replaces `path.read_text(...) if path.exists() else None` and the
    `if not path.exists(): ... else: read_text(...)` pair below it. Both spelled
    the same TOCTOU: a file present at the `exists()` call and gone by the read
    raised FileNotFoundError out of a gate that already HAS a meaning for
    "missing" -- write it at one site, report it as drift at the other.

    Asking once and letting the answer be the absence removes the window
    entirely, rather than narrowing it. Every other read error still raises: a
    file that is there and cannot be read is a real fault, and this gate must
    not quietly regenerate over it.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def parse_frontmatter(skill_md: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, error_message); error_message is empty on success.

    A thin wrapper over ``scripts.utils.markdown.parse_frontmatter_strict``,
    which is the diagnostic parser the previous version of this docstring asked
    for. The error string is this gate's whole output - ``load_routing_rows``
    prints ``{rel}: {err}`` and CI fails on it - so the classification comes from
    the shared parser and the WORDING stays here, unchanged.

    Its own copy was already fence-line anchored, and carried one defect of its
    own: it computed the block as ``text[4:...]``, assuming the opening fence is
    exactly four characters. MEASURED 2026-08-28, an opening fence written
    ``---\\t\\t`` left a tab at the start of the block and PyYAML refused it with
    "found character '\\t' that cannot start any token", on a file whose YAML was
    perfectly good. The shared splitter computes the offset from the first line.

    The docstring this replaces claimed to mirror
    ``skill-metadata-check.py::parse_frontmatter`` "for consistency". It had not
    mirrored it since 2026-08-20, when the fence-line fix landed HERE and not
    there, and the two gates then disagreed about any SKILL.md whose frontmatter
    contained ` --- ` inside a scalar. Both are wrappers now, so the claim is
    true again by construction.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # `UnicodeDecodeError` is a ValueError, so `except OSError` never caught
        # it, and a SKILL.md carrying one stray Latin-1 byte (a paste, an editor
        # that saved cp1252) took the whole generator down with a traceback -
        # in CI and in pre-commit, where `--check` runs. This function's entire
        # contract is to hand `load_routing_rows` a `(data, error)` pair so the
        # gate can print the curated `{rel}: {err}` line naming the file, which
        # is the one thing a traceback does not do. MEASURED 2026-09-01 on a
        # scratch skill tree: `UnicodeDecodeError: 'utf-8' codec can't decode
        # byte 0xe9` out of `load_routing_rows`, with no file named.
        return {}, f"unreadable: {exc}"
    data, kind, detail = parse_frontmatter_strict(text)
    if kind == md.FM_OK:
        return data, ""
    return {}, {
        md.FM_NO_OPENING: "no frontmatter (missing opening ---)",
        md.FM_NO_CLOSING: "malformed frontmatter (missing closing ---)",
        md.FM_INVALID_YAML: f"invalid YAML frontmatter: {detail}",
        md.FM_EMPTY: "empty frontmatter",
        md.FM_NOT_MAPPING: f"frontmatter must be a mapping, got {detail}",
    }[kind]


# ============================================================
# Row loading
# ============================================================

# A markdown table row is ONE line. These two characters end it, so a cell
# holding either produces a half row plus an orphan fragment on the next line -
# in an always-on rule, and in a detail file where the remaining columns then
# describe the wrong skill. Deterministic, so `--check` regenerates the same
# corruption and passes.
#
# A tab is deliberately NOT here. It renders as whitespace inside a cell, so
# refusing it would fail a working SKILL.md over a symptom no reader can see.
FORBIDDEN_IN_CELL = {"\n": "newline", "\r": "carriage return"}

# `label` alone is rendered INSIDE a backtick code span, by `render_row`:
# `| `{label}` | ...`. So a backtick in it closes the span
# early and the Skill cell of an always-on rule renders mangled - the same
# deterministic corruption `--check` regenerates and passes that the two
# characters above are refused for. Triggers and exclusions are NOT in
# a code span and backticks there are ordinary house style: measured on the live
# corpus 2026-09-02, 30 of the 94 skills carry one, so refusing the character
# everywhere would fail a third of the tree over nothing.
FORBIDDEN_IN_CODE_SPAN = {"`": "backtick"}


def _as_cell(value, *, field: str, code_span: bool = False) -> str:
    """One string, safe to place in a markdown table cell.

    `_as_list` below guards the item TYPE and the container TYPE. Cell CONTENT
    was the hole both left: a trigger written as a folded scalar, which is the house style for
    `x-heading-capability` in this same frontmatter, arrives with a trailing
    newline and splits the row.

    `label` reaches this function for a second reason. It was read raw three
    lines below the `name` check and its explaining comment, so `label: 7`
    raised an uncaught `TypeError` out of `escape_pipes` instead of the curated
    `{rel}: {err}` line this gate exists to print, and `label: no` - the YAML
    boolean False, which is falsy - vanished into the `or f"/{name}"` default
    with no word to the author who set it.

    `code_span=True` adds the backtick refusal, and only `label` passes it,
    because only `label` is rendered inside a code span. See
    `FORBIDDEN_IN_CODE_SPAN`.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"{field} is {value!r} ({type(value).__name__}); it must be a "
            f"string. An unquoted YAML scalar like `7` or `no` is not.")
    for char, name in FORBIDDEN_IN_CELL.items():
        if char in value:
            raise ValueError(
                f"{field}: contains a {name}, which ends the markdown table "
                f"row: {value[:60]!r}. Write the value on one line - a folded "
                f"scalar (`- >`) adds a trailing newline.")
    if code_span:
        for char, name in FORBIDDEN_IN_CODE_SPAN.items():
            if char in value:
                raise ValueError(
                    f"{field}: contains a {name}, and this cell is rendered "
                    f"inside a code span: {value[:60]!r}. The span closes on the "
                    f"first one and the Skill column renders mangled. Remove it.")
    # An empty cell is refused rather than defaulted. `label: ""` renders an
    # empty code span in the Skill column, which is a broken row; and the
    # alternative - falling back to `/{name}` - is the same silent-ignore this
    # function was written to end for `label: no`. The author wrote something,
    # so they hear about it. Measured on the live corpus 2026-08-27: no
    # trigger, exclusion or label is empty, so nothing regresses.
    if not value.strip():
        raise ValueError(
            f"{field} is empty (or only whitespace). An empty markdown cell "
            f"renders as a blank column; write a value or remove the key.")
    return value


def _as_list(value, *, field: str) -> list[str]:
    """Coerce a triggers/exclusions frontmatter value to a list of strings.

    Raises ``ValueError`` on an entry that is not a string. That case is not
    hypothetical and the coercion is what made it dangerous: an unquoted
    ``colon-space`` inside a YAML list item makes the whole item parse as a
    MAPPING, and the old ``str(v)`` rendered the mapping's Python ``repr`` -
    braces, quoted key, quoted value - straight into the generated router row.

    Measured 2026-08-20 on `.claude/skills/checkpoint/SKILL.md`: one such
    sentence put a Python dict literal into `.claude/rules/skill-router.md`, an
    ALWAYS-ON rule injected into every session, and grew that row from 804 to
    867 characters. Both gates stayed green through it - `--check` compared a
    corrupt generation against a corrupt file and found them equal, and
    `skill-metadata-check.py` never inspects the item type. A coercion that can
    turn a structural mistake into valid-looking output is a gate that reports
    on nothing.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        # A MAPPING iterates its keys, and every key is a string, so
        # `triggers: {alpha: 1, beta: 2}` was coerced into the plausible-looking
        # list `[alpha, beta]` with the values silently dropped, and both gates
        # stayed green. The docstring above says this function exists to stop a
        # structural mistake becoming valid-looking output; the non-str ITEM
        # case was guarded and the non-list CONTAINER case was not.
        raise ValueError(
            f"{field}: value is a {type(value).__name__}, not a list: "
            f"{str(value)[:80]!r}. Write it as a YAML list of strings."
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"{field}: entry is a {type(item).__name__}, not a string: "
                f"{str(item)[:80]!r}. A YAML list item containing an unquoted "
                f"'colon space' parses as a mapping - quote the whole item, or "
                f"replace the colon with a dash."
            )
        out.append(_as_cell(item, field=field))
    return out


def load_routing_rows() -> tuple[list[dict], list[str]]:
    """Read every skill's x-heading-routing block.

    Returns (rows, errors). Each row is a dict with keys name, category, label,
    triggers, exclusions, router, model_invocable. errors is a list of human-readable
    strings (missing block, bad category, ...); a non-empty errors list means the
    registry must not be generated.

    Advisory warnings go straight to stderr and never block. They exist so a
    disagreement between a field and the prose it describes is VISIBLE without
    a night-shift decision to make it fatal.
    """
    rows: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    if not SKILLS_DIR.exists():
        return rows, [f"skills dir not found: {SKILLS_DIR}"]

    for child in sorted(SKILLS_DIR.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name in SKIP_SUBDIRS:
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        fm, err = parse_frontmatter(skill_md)
        rel = skill_md.relative_to(ROOT)
        if err:
            errors.append(f"{rel}: {err}")
            continue
        # Validated like every other field. `name` was taken raw, so a YAML
        # `name: 7` produced an int that reached `sorted(key=lambda r: r["name"])`
        # and raised `TypeError: '<' not supported between 'str' and 'int'` --
        # an uncaught traceback instead of the curated `{rel}: {err}` line this
        # gate exists to print. `name: 0` and `name: false` were quieter still:
        # both are falsy, so `or child.name` swallowed them and the directory
        # name silently stood in for a value the author had set on purpose.
        raw_name = fm.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            errors.append(
                f"{rel}: 'name' is {raw_name!r} ({type(raw_name).__name__}); "
                f"it must be a string. An unquoted YAML scalar like `name: 7` "
                f"or `name: no` is not.")
            continue
        name = raw_name or child.name
        routing = fm.get(ROUTING_KEY)
        if not isinstance(routing, dict):
            errors.append(
                f"{rel}: missing '{ROUTING_KEY}' block. Add it under the frontmatter:\n"
                + "\n".join("      " + ln for ln in FIX_IT_SNIPPET.splitlines())
            )
            continue
        category = routing.get("category")
        if category not in CATEGORY_ORDER:
            errors.append(
                f"{rel}: '{ROUTING_KEY}.category' is {category!r}; must be one of {CATEGORY_ORDER}"
            )
            continue
        # `label` joins this block rather than staying a raw `.get()` three
        # lines below: it is a cell value like the others, and the `name` fix
        # above documents exactly what a raw read of one costs.
        raw_label = routing.get("label")
        try:
            triggers = _as_list(routing.get("triggers"), field=f"{ROUTING_KEY}.triggers")
            exclusions = _as_list(routing.get("exclusions"), field=f"{ROUTING_KEY}.exclusions")
            # The DEFAULT is checked too, not only an authored `label`. It is
            # built from `name`, which falls back to the directory name, and both
            # reach the same code span.
            label = _as_cell(
                raw_label if raw_label is not None else f"/{name}",
                field=f"{ROUTING_KEY}.label", code_span=True)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        router = routing.get("router", "auto")
        if router not in ("auto", "manual"):
            errors.append(f"{rel}: {ROUTING_KEY}.router must be 'auto' or 'manual', "
                          f"got {router!r}")
            continue
        # The `router` field was LOADED and never read again: not validated, not
        # rendered, not used to filter. `FIX_IT_SNIPPET` documents it as
        # `manual (NEVER auto-trigger skills)`, so an author who set it believed
        # they had switched a safety control that did nothing. It is read now,
        # in two steps of very different strength.
        #
        # HARD: the value must be one of the two documented ones. A typo used to
        # pass silently.
        #
        # SOFT: a warning when the field disagrees with the trigger cell, which
        # is what the always-on rule actually shows the model. Measured across
        # all 94 skills on 2026-08-24: 23 manual, all 23 already saying "NEVER
        # auto-trigger", and no auto skill saying it. So the convention holds
        # everywhere today -- but it is a convention about ENGLISH PROSE in a
        # free-form list, and a future author writing "explicit invocation only"
        # would be correct and still fail a hard gate. Turning this into an
        # error is a change to how skills are authored, which is the operator's
        # call, not a night-shift one.
        #
        # NOT tied to `disable-model-invocation`, the harness-enforced flag that
        # is the real control: `brain-audit` is `router: manual` and
        # deliberately does NOT set it, because composing skills invoke it
        # through the Skill tool and the flag would block them. One measured
        # exception is enough to say that link is not an invariant either.
        says_never = "never auto-trigger" in " ".join(triggers).lower()
        if (router == "manual") != says_never:
            warnings.append(
                f"{rel}: {ROUTING_KEY}.router is {router!r} but the triggers "
                f"{'do not say' if router == 'manual' else 'say'} "
                f"'NEVER auto-trigger'. The trigger cell is what the always-on "
                f"router rule shows the model; the two should agree.")
        rows.append(
            {
                "name": name,
                "category": category,
                "label": label,
                "triggers": triggers,
                "exclusions": exclusions,
                "router": router,
                # The harness flag, read for the command-only roster: a manual
                # skill WITHOUT it is still listed to the model.
                "model_invocable": fm.get("disable-model-invocation") is not True,
            }
        )
    for warning in warnings:
        print(f"{CYAN}note{RESET}: {warning}", file=sys.stderr)
    return rows, errors


# ============================================================
# Rendering
# ============================================================

def escape_pipes(text: str) -> str:
    """Escape a raw ``|`` as ``\\|`` for markdown-table safety, leaving an already
    escaped ``\\|`` untouched.

    Parity-aware: a pipe counts as already escaped only when the backslash run
    before it is ODD. A plain negative lookbehind treated ANY preceding backslash
    as an escape, so an EVEN run - a literal backslash that is DATA, written
    ``C:\\\\|foo`` - left its pipe unescaped and split the table cell into a
    spurious column. Parity escapes that one: ``C:\\\\|foo`` renders
    ``C:\\\\\\|foo``.

    ``C:\\|foo`` was named as the motivating example here until 2026-09-02, and
    it is the case parity does NOT repair: one backslash is an odd run, so both
    the old lookbehind and the parity rule read it as an existing escape and
    leave the pipe bare. MEASURED that day, the two spellings agree on that
    input and differ only on the even one. See ``unescape_pipes`` for what the
    odd run costs on the way back.
    """
    def _fix(match: "re.Match") -> str:
        slashes = match.group(1)
        return match.group(0) if len(slashes) % 2 else slashes + "\\|"

    return re.sub(r"(\\*)\|", _fix, text)


def unescape_pipes(text: str) -> str:
    """Drop the ONE backslash ``escape_pipes`` added. NOT its exact inverse.

    Parity-aware like the forward function: a pipe is unescaped only when the
    backslash run before it is ODD. Known limit (2026-09-02): a pipe after an
    odd run that was DATA is passed through by ``escape_pipes`` and loses a
    backslash here; repairing it would double every hand-escaped pipe in
    SKILL.md cells, which do exist.
    """
    def _unfix(match: "re.Match") -> str:
        slashes = match.group(1)
        return slashes[:-1] + "|" if len(slashes) % 2 else match.group(0)

    return re.sub(r"(\\*)\|", _unfix, text)


def render_row(row: dict) -> str:
    # The Skill column is backtick-wrapped code: `/name` or `/name [args]`.
    label = escape_pipes(row["label"])
    triggers = escape_pipes(TRIGGER_SEP.join(row["triggers"]))
    exclusions = escape_pipes(EXCL_SEP.join(row["exclusions"]))
    return f"| `{label}` | {triggers} | {exclusions} |"


def render_registry(rows: list[dict]) -> str:
    """Render the seven category tables as the content that lives between the markers.

    Deterministic ordering: fixed category order, then skill name ascending within a
    category. Blocks are separated by a blank line; no trailing newline.
    """
    blocks: list[str] = []
    for category in CATEGORY_ORDER:
        members = sorted(
            (r for r in rows if r["category"] == category), key=lambda r: r["name"]
        )
        lines = [f"### {category}", "", TABLE_HEADER, TABLE_SEP]
        lines.extend(render_row(r) for r in members)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def category_slug(category: str) -> str:
    """Filename slug for a category's detail file. 'CRM' -> 'crm', 'Intel' -> 'intel'."""
    return category.lower()


# The always-on rule, whole. `{detail_files}` and `{collision_map}` are filled
# from the skills; everything else is the policy itself. Kept small on purpose:
# the harness lists every model-invocable skill's description already, so this
# file carries only what a description cannot (precedence, the command-only
# rule, the gates, the collisions). Target 3,500 bytes.
POLICY_TEMPLATE = """\
<!-- version: 2.1.0 | last-updated: 2026-10-29 -->
---
paths: []
always_active: true
---

# Skill Router

Last Updated: 2026-10-29
Last Verified: 2026-10-29

Generated by `scripts/generate-skill-router.py`; never hand-edit. Rationale:
`skill-router-notes.md` under registry name `reference-docs`.

A typed `/slash-command` bypasses the router. Otherwise, when a skill plausibly
applies, invoke it before responding and announce it ("Using /osint for this");
if it turns out wrong once loaded, say so and continue without it. Hold ordinary
conversation only when no skill plausibly applies. Operator decision 2026-09-27
to keep the superpowers plugin and its invoke-first stance.

**Corporate documents route first**, by `.claude/rules/corporate-docs.md`, even
when no template is named.

**VPN pre-flight.** Any skill reaching a public site that blocks datacenter IPs
(`/yt-pulse`, `/playwright` on YouTube, `/osint`, anything calling
`scripts/firecrawl.py`) runs the gate in `reference/vpn-preflight.md` before its
first network call and waits for an explicit answer; proceeding unanswered is a
protocol violation. Resident because the obligation has no path signal.

**Command-only skills** (`router: manual`) run on the typed command, never from
a message; {callable_from_outside} deliberately leave
`disable-model-invocation` off so other sessions and hooks can call them.

When 2-3 skills plausibly apply, read the category Detail file:
{detail_files}. If it does not settle it, offer a numbered menu and wait.

## Two fallbacks

**Local skill not in the registry.** If nothing matches but the intent clearly
maps to a slash command in `.claude/skills/`, invoke it anyway, then note: "This
skill isn't in the router registry yet. It should be added through its
`x-heading-routing` frontmatter and `scripts/generate-skill-router.py`."

**Plugin-namespaced skills (`plugin:skill`) are NEVER auto-routed from natural
language.** Invoke one by its namespaced name through the Skill tool when its own
metadata clearly applies, or when the operator types `/plugin:skill`. On a
bare-name collision the local `.claude/skills/` entry wins. Why, and the installed
roster with costs: `skill-router-notes.md` under `reference-docs`.

## Collision map

{collision_map}
"""

# `/token` on the right of an exclusion's `->`: the skill it points at.
_TARGET_RE = re.compile(r"/([a-z0-9-]+)")


def parse_exclusion(text: str) -> tuple[str, str] | None:
    """(SIGNAL, TARGET) of one exclusion string, or None when it names no skill.

    Split at the FIRST `->`. TARGET is the first `/token` on the right, anything
    after it ignored, so `/odin recall (brain-scoped)` targets `odin`. SIGNAL is
    the left part, trimmed, with escaped and surrounding double quotes removed
    from each ` / ` alternative and the alternation kept as written. An exclusion
    with no `/token` on the right (`Grep.`, `just answer`) is None.
    """
    if "->" not in text:
        return None
    left, right = text.split("->", 1)
    match = _TARGET_RE.search(right)
    if not match:
        return None
    parts = []
    for alt in left.replace('\\"', '"').split(" / "):
        alt = alt.strip()
        if len(alt) >= 2 and alt.startswith('"') and alt.endswith('"'):
            alt = alt[1:-1]
        parts.append(alt)
    return " / ".join(parts).strip(), match.group(1)


def collision_line(cluster: str, members: tuple[str, ...],
                   by_name: dict[str, dict], fallbacks: list[str]) -> str | None:
    """One collision-map line: each member with the signals its siblings route to it.

    A command-only member prints `/X typed only`. A member no sibling points at
    falls back to its own first trigger, and is appended to `fallbacks` so the
    caller can name it. Members absent from the tree (a public build that does
    not ship one) are skipped; a cluster with none present is None.
    """
    present = [m for m in members if m in by_name]
    if not present:
        return None
    cells = []
    for name in present:
        row = by_name[name]
        if row["router"] == "manual":
            cells.append(f"/{name} typed only")
            continue
        signals: list[str] = []
        for other in present:
            if other == name:
                continue
            for excl in by_name[other]["exclusions"]:
                parsed = parse_exclusion(excl)
                if parsed and parsed[1] == name and parsed[0] not in signals:
                    signals.append(parsed[0])
        if not signals:
            fallbacks.append(name)
            signals = row["triggers"][:1]
        cells.append(f"/{name} for {'; '.join(signals)}")
    return f"- {cluster}: " + " | ".join(cells)


def render_policy(rows: list[dict], fallbacks: list[str] | None = None) -> str:
    """The whole always-on rule: POLICY_TEMPLATE with the detail-file list and the
    collision map filled in. Deterministic; trailing newline."""
    by_name = {r["name"]: r for r in rows}
    used = fallbacks if fallbacks is not None else []
    lines = [line for cluster, members in OVERLAP_CLUSTERS.items()
             if (line := collision_line(cluster, members, by_name, used))]
    detail = ("`reference/skill-router/{"
              + ",".join(category_slug(c) for c in CATEGORY_ORDER) + "}.md`")
    return POLICY_TEMPLATE.format(detail_files=detail,
                                  collision_map="\n".join(lines),
                                  callable_from_outside=callable_roster(rows))


def callable_roster(rows: list[dict]) -> str:
    """`/a`, `/b` and `/c`: every `router: manual` skill that does NOT carry
    `disable-model-invocation: true`, so the sentence naming them cannot fall
    behind a new one. The three were written by hand until 2026-10-29."""
    names = [f"`/{r['name']}`" for r in sorted(rows, key=lambda r: r["name"])
             if r["router"] == "manual" and r["model_invocable"]]
    if not names:
        return "no skill"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def render_category_file(category: str, rows: list[dict]) -> str:
    """Render one whole reference/skill-router/<slug>.md detail file.

    H1 + one-line description + a generated-by note (no volatile date, so the file
    is byte-identical every run and --check stays deterministic), then the full
    3-column table via the shared render_row. Trailing newline (whole-file).
    """
    members = sorted(
        (r for r in rows if r["category"] == category), key=lambda r: r["name"]
    )
    lines = [
        f"# Skill Router — {category}",
        "",
        f"Full routing detail (triggers, exclusions) for the "
        f"{category} skill category.",
        "",
        "Generated by scripts/generate-skill-router.py from each SKILL.md "
        "x-heading-routing block; do not edit by hand. "
        "Consumed by .claude/rules/skill-router.md.",
        "",
        TABLE_HEADER,
        TABLE_SEP,
    ]
    lines.extend(render_row(r) for r in members)
    return "\n".join(lines) + "\n"


# ============================================================
# Commands
# ============================================================

def _report_errors(errors: list[str]) -> None:
    print(f"{RED}FAIL{RESET}: {len(errors)} skill(s) cannot be rendered:", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)


def cmd_split_write(rows: list[dict]) -> int:
    """Write the always-on rule AND the 7 category detail files."""
    router_text = _existing_text(ROUTER_FILE)
    fallbacks: list[str] = []
    new_text = render_policy(rows, fallbacks)

    # DETAIL FILES FIRST, the rule last. Neither order is atomic, but the
    # order decides what a reader sees if a write fails mid-way: the router
    # rule is the always-on layer the model reads every session, so it is the
    # one that must never point at detail files that have not caught up. This
    # ran the other way round, so a permissions or disk failure inside the loop
    # left the always-on index describing frontmatter the detail files did not.
    wrote_any = False
    CATEGORY_FILE_DIR.mkdir(parents=True, exist_ok=True)

    # Orphans are REMOVED, not merely reported. `cmd_split_check` calls any
    # *.md here that no current category backs an ORPHAN and counts it as
    # drift, then tells the operator to run this command -- which only ever
    # wrote the seven expected files and deleted nothing. So one stray file (a
    # renamed category committed earlier, a leftover from a reverted branch, a
    # hand-written note) made `--check` fail forever, with CI and pre-commit
    # unresolvable by the documented path and an undocumented `rm` as the only
    # way out. Reproduced 2026-08-25.
    #
    # Every removal is named. This directory is generated in full by this
    # function, so deleting what does not belong is within its remit -- but a
    # tool that deletes a file the operator wrote must say which one.
    for stray in sorted(CATEGORY_FILE_DIR.glob("*.md")):
        if stray.name in {f"{category_slug(c)}.md" for c in CATEGORY_ORDER}:
            continue
        print(f"{YELLOW}removed orphan{RESET}: "
              f"{stray.relative_to(ROOT)} (no category backs it)")
        stray.unlink()
        wrote_any = True

    for category in CATEGORY_ORDER:
        path = CATEGORY_FILE_DIR / f"{category_slug(category)}.md"
        content = render_category_file(category, rows)
        existing = _existing_text(path)
        if existing != content:
            path.write_text(content, encoding="utf-8")
            wrote_any = True

    if new_text != router_text:
        ROUTER_FILE.write_text(new_text, encoding="utf-8")
        wrote_any = True

    if wrote_any:
        print(f"{GREEN}WROTE{RESET}: regenerated router rule + "
              f"{len(CATEGORY_ORDER)} category files ({len(rows)} skills).")
    else:
        print(f"{GREEN}OK{RESET}: router rule + category files already current "
              f"({len(rows)} skills).")
    if fallbacks:
        print(f"{CYAN}note{RESET}: collision map fell back to the first trigger "
              f"for {', '.join(fallbacks)} (no sibling exclusion points at them)")
    return 0


def cmd_split_check(rows: list[dict]) -> int:
    """Verify both layers (the rule + every category file) for content idempotency."""
    router_text = ROUTER_FILE.read_text(encoding="utf-8")
    new_text = render_policy(rows)

    # (name, on_disk_text, regenerated_text) for each drifted/missing/orphan artifact.
    drift: list[tuple[str, str, str]] = []
    if new_text != router_text:
        drift.append((".claude/rules/skill-router.md", router_text, new_text))

    for category in CATEGORY_ORDER:
        slug = category_slug(category)
        path = CATEGORY_FILE_DIR / f"{slug}.md"
        content = render_category_file(category, rows)
        existing = _existing_text(path)
        if existing is None:
            drift.append((f"reference/skill-router/{slug}.md (MISSING)", "", content))
        elif existing != content:
            drift.append((f"reference/skill-router/{slug}.md", existing, content))

    # Orphan detail files (a *.md not backed by a current category) are drift too.
    if CATEGORY_FILE_DIR.exists():
        expected = {f"{category_slug(c)}.md" for c in CATEGORY_ORDER}
        orphans = [f for f in sorted(CATEGORY_FILE_DIR.glob("*.md"))
                   if f.name not in expected]
        # Through `read_sources`, and SKIPPING is right here. An orphan that
        # disappeared between the glob and the read is an orphan that no longer
        # exists, so declining to report it is the correct answer rather than a
        # narrowed one -- and the alternative was a FileNotFoundError out of a
        # pre-commit gate whose only finding had just deleted itself. The
        # regenerated side is the empty string for every orphan (the file should
        # not be there at all), so nothing here is a checksum a skip could bend.
        for f, text in read_sources(orphans):
            drift.append((f"reference/skill-router/{f.name} (ORPHAN)", text, ""))

    if not drift:
        print(f"{GREEN}OK{RESET}: router rule + category files in sync with "
              f"SKILL.md frontmatter ({len(rows)} skills).")
        return 0

    print(
        f"{RED}DRIFT{RESET}: {len(drift)} artifact(s) differ from the SKILL.md "
        f"frontmatter. Run {CYAN}python scripts/generate-skill-router.py{RESET} and commit.",
        file=sys.stderr,
    )
    for name, old, new in drift:
        print(f"  - {name}", file=sys.stderr)
        diff = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"{name} (on disk)", tofile=f"{name} (regenerated)", n=2,
        )
        sys.stderr.write("".join(diff))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true",
                     help="Write the router rule + per-category files in place (default).")
    mode.add_argument("--check", action="store_true",
                     help="Regenerate both layers and diff; exit 1 on drift (CI / pre-commit).")
    # INSIDE the group. `--check --flat` used to be accepted, print the flat
    # monolith, and exit 0 WITHOUT running the drift check -- a CI invocation
    # with a typo'd flag combination going green while checking nothing.
    # `--write --flat` silently discarded the write the same way.
    mode.add_argument("--flat", action="store_true",
                      help="Print the legacy flat monolith to stdout (debug + semantics proof); no write.")
    mode.add_argument(
        "--split-by-category", action="store_true",
        help="Explicit synonym of the default write (router rule + per-category files).",
    )
    args = parser.parse_args()

    if not ROUTER_FILE.exists():
        print(f"{RED}ERROR{RESET}: {ROUTER_FILE} not found", file=sys.stderr)
        return 2

    rows, errors = load_routing_rows()
    if errors:
        _report_errors(errors)
        return 1

    # --flat is a stdout-only debug/proof action: print the legacy flat monolith,
    # never write. (Used by the semantics-preservation test.)
    if args.flat:
        print(render_registry(rows))
        return 0

    # Split is the canonical layout. --split-by-category is an explicit synonym of
    # the default write; --check verifies both layers.
    if args.check:
        return cmd_split_check(rows)
    return cmd_split_write(rows)


if __name__ == "__main__":
    sys.exit(main())
