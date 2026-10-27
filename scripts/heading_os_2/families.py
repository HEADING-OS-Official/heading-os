#!/usr/bin/env python3
"""The HEADING OS 2 family/leaf vocabulary, and the rules that map today's tree onto it.

This module is DATA, not behaviour: it holds the target taxonomy and an ordered
list of path rules. `manifest.py` applies it. Keeping the two apart is what lets
the rules be reviewed as a table rather than read out of control flow.

## Nine families, and sensitivity is not one of them

Operator directive, 2026-09-12: nothing on this machine is excluded from the
index or from memory, so there is no sealed family. Sensitivity is an axis:
private material takes the ordinary family that answers its own question and
carries `sensitivity: ceo-only`. Three sensitivity values exist.

## Why a file has exactly one family

The organising question is "what question does this file answer". Anything else
about the file -- its format, where it came from, which process made it -- is a
label. A category name must complete the sentence "this file answers a question
about ___". `principle` completes it; `structured data` does not, because nobody
has ever asked a question about a file format.

## Rules are ordered, longest prefix first

`RULES` is applied in order and the FIRST match wins, so a more specific prefix
must appear before the general one that contains it. `check_rule_order` asserts
that rather than trusting it, because a rule silently shadowed by an earlier one
is invisible by reading a table this long.

A rule's `method` records how the answer was reached, and it is reported rather
than decorative:

    rule        - decided by path alone, no file read. Mechanical, cheap, safe.
    frontmatter - decided by a field already in the file. Verifiable: the old
                  field exists, the new value derives from it, disagreement shows.
    judgement   - needs a human or a model to read the content. NOT decided here.
    skip        - deliberately not migrated (caches, temporaries, sync logs).

`skip` is never silent. Every skipped path is counted and listed, because a file
that quietly fails to migrate is indistinguishable from one that was never there.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

#: family -> its leaves. A leaf exists when it changes what the system does:
#: it has its own write trigger, it changes what is permitted, or it is asked
#: about by name. A leaf that merely sounds different is not a leaf.
FAMILIES: dict[str, tuple[str, ...]] = {
    "canon":    ("principle", "position", "constraint", "preference",
                 "definition", "reference"),
    "ledger":   ("episode", "decision", "measurement", "incident",
                 "interaction"),
    # `status`, not `state`. Operator decision 2026-09-13: `/state/` is a line in
    # the data overlay's .gitignore (per-machine daemon runtime), so a family by
    # that name would have put live business matters where git cannot see them.
    # The legacy spelling still arrives from the 2026-09-12 manifest and is read
    # as `status` by `vocabulary_family` below; nothing else in the engine says it.
    "status":   ("commitment", "matter", "entity", "open-question",
                 "machine", "queue"),
    "research": ("dossier", "market", "competitor", "survey", "evaluation",
                 "consultation", "digest"),
    "work":     ("deliverable", "manuscript", "draft", "design"),
    "inbound":  ("agreement", "document", "correspondence", "capture"),
    "build":    ("spec", "runbook", "code", "config", "dataset"),
    "trace":    ("handoff", "chronicle", "report", "scratch"),
    "asset":    ("brand", "image", "media", "bundle"),
}

#: LEGACY family name (as the 2026-09-12 manifest writes it) -> the vocabulary
#: name, which is also the directory it is written to. One entry: `state` became
#: `status` on 2026-09-13, see the comment in `FAMILIES`. A dict rather than a
#: constant so the next one, if there ever is one, is a line rather than a
#: refactor. Lived in `move.py` until HOS-161 moved it here, beside the
#: vocabulary it renames, so live readers need not import the mover.
FAMILY_RENAMES: dict[str, str] = {"state": "status"}


def vocabulary_family(family: str) -> str:
    """The name this module knows a family by.

    The legacy `state` from the 2026-09-12 manifest resolves to `status`, and
    `status` resolves to itself. `move.target_family` answers the sibling
    question ("where is it written"); the two are separate functions because
    they were answered differently while the rename lived only in the mover.
    """
    return FAMILY_RENAMES.get(family, family)

#: The sensitivity ladder, least to most restricted. A fourth value sat above
#: these and left on 2026-09-12 with the family it existed for; see the module
#: docstring.
#:
#: `internal` is the default for a record that declares none. Never `open`: a
#: record whose sensitivity nobody stated is not thereby public.
SENSITIVITIES: tuple[str, ...] = ("open", "internal", "ceo-only")

#: There is deliberately NO default sensitivity constant here, and no list of
#: "private" prefixes. Both existed until 2026-09-13 and both were mine: a
#: `DEFAULT_SENSITIVITY = "internal"` and a hand-written segment plus three
#: directory prefixes, standing beside `config/routing-map.yaml` as a second
#: answer to a question that already had an authority. MEASURED the day they
#: were deleted: they disagreed with the routing map on 10,008 of 11,857 files,
#: always in the unsafe direction, and a downstream gate built on them sent
#: private material to a third-party model.
#:
#: A default is what made it dangerous rather than merely wrong. A file no rule
#: named fell to `internal` and looked deliberate. `sensitivity_for()` now asks
#: the resolver, which fails closed to `private` on its own, so there is
#: nothing left here to default to. Do not reintroduce one.

# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

#: (path_prefix, family, leaf, method). First match wins; order matters.
#: A `leaf` of None means the rule fixes the family and a second pass decides
#: the leaf -- used where one of today's directories splits in two.
#:
#: EVERY PREFIX BELOW IS A COMPARISON KEY, never a path. It is tested with
#: `str.startswith` against a path already relative to the data root, and it is
#: never joined to anything. The `get_*_dir()` seam answers "where does the data
#: live"; this table answers "which record is this", which is a different
#: question the seam cannot be asked. That is why each line carries the
#: sanctioned inline suppression rather than a helper call: `leak-guard.py`
#: names "comparison keys" and "prefix patterns" as precisely this case, and
#: adding this file to that guard's own allowlist to pass this commit is the
#: move the workspace forbids outright.
RULES: tuple[tuple[str, str | None, str | None, str], ...] = (
    # --- deliberately not migrated -----------------------------------------
    # Caches, scratch and sync bookkeeping. Counted and listed, never silent.
    ("outputs/_sync/",     None, None, "skip"),  # leak-guard: ok (prefix key)
    ("outputs/_tmp/",      None, None, "skip"),  # leak-guard: ok (prefix key)
    ("outputs/clipboard/", None, None, "skip"),  # leak-guard: ok (prefix key)
    (".sync/",             None, None, "skip"),  # leak-guard: ok (prefix key)
    ("_derived/",          None, None, "skip"),  # leak-guard: ok (prefix key)
    # The one store the five spellings above are folding into
    # (scripts/utils/runtime_dir.py). Junk by construction; never a family.
    ("runtime-junk/",      None, None, "skip"),  # leak-guard: ok (prefix key)
    # The data-address registry (HOS-5, phase 1). Not junk, and not a record
    # either: it is the ONE address the engine holds as a constant, and every
    # other address is read out of it. The rule below it, `("config/", "build",
    # "config", "rule")`, would classify it `build/config`, whose manifest row
    # is `moved` -- so the migration would carry the registry to
    # `build/config/data-paths.yaml` and the constant pointing at it would  # leak-guard: ok (migration vocabulary: where the move rule would have sent the registry)
    # resolve to nothing, taking every address with it. Skipping is not a
    # judgement that the file is unimportant; it is the statement that its
    # address is fixed by the task rather than chosen by the classifier. The
    # check that held this row in place was parked in the 2026-09-21 test-suite
    # split, so deleting the row would look like tidying and would take every
    # logical address with it.
    ("config/data-paths.yaml", None, None, "skip"),  # leak-guard: ok (prefix key)

    # --- knowledge: the brain splits by its own subdirectory ----------------
    # The clearest case in the tree of a directory that already answers the
    # family question correctly. Flattening these four into one leaf would
    # destroy a distinction somebody made deliberately.
    # A recorded CONFLICT between two principles is not a principle. It is a
    # question somebody has not settled, and `status/open-question` is the leaf
    # that exists for exactly that. Filing it under canon would make the system
    # answer with one side of an argument as though it were settled.
    ("knowledge/odin-brain/conflicts/", "status", "open-question", "rule"),  # leak-guard: ok (prefix key)
    ("knowledge/odin-brain/reference/", "canon", "reference", "rule"),  # leak-guard: ok (prefix key)
    ("knowledge/odin-brain/principles/", "canon", "principle", "rule"),  # leak-guard: ok (prefix key)
    ("knowledge/odin-brain/positions/", "canon", "position", "rule"),  # leak-guard: ok (prefix key)
    ("knowledge/odin-brain/sources/", "research", "survey", "rule"),  # leak-guard: ok (prefix key)
    ("knowledge/odin-brain/episodes/", "ledger", "episode", "rule"),  # leak-guard: ok (prefix key)
    ("knowledge/odin-brain/", "canon", None, "judgement"),  # leak-guard: ok (prefix key)
    ("knowledge/", "canon", None, "judgement"),  # leak-guard: ok (prefix key)

    # --- auto-memory: the leaf comes from the frontmatter `type:` -----------
    ("auto-memory/", None, None, "frontmatter"),  # leak-guard: ok (prefix key)

    # --- CRM and threads: the shape is right, the content needs reading -----
    ("crm/.migration-backup/", "trace", "scratch", "rule"),  # leak-guard: ok (prefix key)
    ("crm/contacts/", "status", "entity", "rule"),  # leak-guard: ok (prefix key)
    ("crm/address-book/", "status", "entity", "rule"),  # leak-guard: ok (prefix key)
    ("crm/", "status", None, "judgement"),  # leak-guard: ok (prefix key)
    ("threads/", "status", "matter", "judgement"),  # leak-guard: ok (prefix key)

    # --- chronicle: the machine talking about itself ------------------------
    ("chronicle/", "trace", "chronicle", "rule"),  # leak-guard: ok (prefix key)

    # --- outputs ------------------------------------------------------------
    # An audit is a report somebody may want to read again, which is the whole
    # distinction `trace/report` carries against `trace/scratch`.
    ("outputs/events/", "inbound", "capture", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/heading-audit/", "trace", "report", "rule"),  # leak-guard: ok (prefix key)
    # Thinking-out-loud files are working material by definition. They are
    # searchable and never quoted in an answer, which is what `trace` means.
    ("outputs/thinking/", "trace", "scratch", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/drafts/", "work", "draft", "rule"),  # leak-guard: ok (prefix key)
    # Anything written to be sent to a person is a draft until it is sent, and
    # this workspace gates every send on the operator's own click, so nothing
    # here is ever further along than that.
    ("outputs/communications/", "work", "draft", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/messages/", "work", "draft", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/presentations/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    # A project folder holds what was produced FOR that project. Named work
    # rather than research: a project is something being delivered, and the
    # research that fed it lives under its own roots.
    ("outputs/projects/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/documentation/", "build", "spec", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/exports/", "asset", "bundle", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/operations/handoff-archive/", "trace", "handoff", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/operations/", "trace", None, "rule"),  # leak-guard: ok (prefix key)
    ("outputs/content/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/intel/", "research", "digest", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/browser/", "inbound", "capture", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/deliverables/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/documents/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/research/", "research", "survey", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/legal/", "inbound", "agreement", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/books/", "work", "manuscript", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/email-drafts/", "work", "draft", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/comms/", "work", "draft", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/design/", "work", "design", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/negotiations/", "research", "consultation", "rule"),  # leak-guard: ok (prefix key)
    ("outputs/", None, None, "judgement"),  # leak-guard: ok (prefix key)

    # --- datastore ----------------------------------------------------------
    # An event folder is mostly what was COLLECTED there: contact lists, notes,
    # material handed over by other people. That is `inbound/capture`. Slides
    # the operator presented are his own work, but they are the minority and a
    # single rule that is right about the bulk beats a model reading each file.
    # The imprecision is recorded here rather than hidden.
    ("datastore/events/", "inbound", "capture", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/corporate/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/intelligence/", "research", "dossier", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/brand/", "asset", "brand", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/products/", "canon", "reference", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/content/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/books/", "inbound", "document", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/operations/", "build", "runbook", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/projects/", "research", "dossier", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/investment/", "research", "dossier", "rule"),  # leak-guard: ok (prefix key)
    ("datastore/", None, None, "judgement"),  # leak-guard: ok (prefix key)

    # --- the system's own engineering ---------------------------------------
    ("docs/", "build", "spec", "rule"),  # leak-guard: ok (prefix key)
    ("plans/", "build", "spec", "rule"),  # leak-guard: ok (prefix key)
    # `admin/apps/` holds small standalone programs, which is code however short
    # they are. Named before `admin/` so they are not swept into runbooks.
    ("admin/apps/", "build", "code", "rule"),  # leak-guard: ok (prefix key)
    ("admin/guides/", "build", "runbook", "rule"),  # leak-guard: ok (prefix key)
    # Provisioning packets are setup material: the runbook for standing one
    # workspace up, plus what it needs.
    ("admin/provision/", "build", "runbook", "rule"),  # leak-guard: ok (prefix key)
    ("admin/", "build", "runbook", "judgement"),  # leak-guard: ok (prefix key)
    ("templates/", "build", "config", "rule"),  # leak-guard: ok (prefix key)
    ("config/", "build", "config", "rule"),  # leak-guard: ok (prefix key)
    ("deploy/", "build", "runbook", "rule"),  # leak-guard: ok (prefix key)
    ("scripts/", "build", "code", "rule"),  # leak-guard: ok (prefix key)
    ("tests/", "build", "dataset", "rule"),  # leak-guard: ok (prefix key)
    ("_archive/", "build", "runbook", "rule"),  # leak-guard: ok (prefix key)

    # --- what is left -------------------------------------------------------
    ("reference/", "canon", "reference", "rule"),  # leak-guard: ok (prefix key)
    ("context/", "status", None, "judgement"),  # leak-guard: ok (prefix key)
    ("corporate/", "work", "deliverable", "rule"),  # leak-guard: ok (prefix key)
    # The PREFIX stays `state/`: that is today's on-disk runtime directory (the
    # gitignored one `scripts/inbox_pulse/` writes), not the family. Only the
    # family cell was renamed.
    ("state/", "status", None, "judgement"),  # leak-guard: ok (prefix key)
)

#: `auto-memory/` frontmatter `type:` -> family/leaf. This is the mapping the
#: 2026-09-10 resident-index measurement used. Nothing is
#: routed to `ledger/incident`: telling a post-mortem from a rule needs
#: judgement, and a judgement made silently inside a harness is worse than none.
AUTO_MEMORY_TYPE_MAP: dict[str, tuple[str, str]] = {
    "feedback":  ("canon", "constraint"),
    "user":      ("canon", "preference"),
    "project":   ("ledger", "decision"),
    "reference": ("canon", "reference"),
}

#: Splits the legacy `outputs/operations/` records: a report someone may want  # leak-guard: ok (migration vocabulary: the legacy directory these rules classify)
#: to read again is `trace/report`, anything else is `trace/scratch`. The rule
#: is deliberately cheap: `trace` is never quoted in an answer, so a wrong call
#: costs ranking, not correctness.
OPERATIONS_REPORT_HINTS: tuple[str, ...] = (
    "report", "audit", "review", "brief", "summary", "findings",
    "state-model", "runbook", "measurement", "analysis",
)

#: Extensions that make a file an `asset` whatever directory it sits in, unless
#: an earlier rule already claimed it. The only family where the file format
#: legitimately IS the point.
ASSET_EXTENSIONS: dict[str, str] = {
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
    "svg": "image", "webp": "image", "ico": "image",
    "mp4": "media", "mov": "media", "mp3": "media", "wav": "media",
    "zip": "bundle", "gz": "bundle", "tar": "bundle", "7z": "bundle",
}


#: Filename of the operator's own rules, read from the DATA overlay.
#:
#: Why this file exists at all, and why it is not a section of the table above.
#: The table above is GENERIC: it maps the directory names any HEADING OS
#: workspace has. An operator's own tree goes deeper than that, and the deeper it
#: goes the more specific to him it is -- a named course, a named clinic, a named
#: property. Three things make those rules belong in the overlay rather than here:
#:
#:   * THIS REPOSITORY IS PUBLIC. A rule naming a real private subtree publishes
#:     the fact that the subtree exists, which is the leak the engine/data split
#:     is for. The workspace's own instruction is to use invented names here and
#:     load the real lists from the overlay.
#:   * A PreToolUse hook refuses a personal path written into a non-personal
#:     file, and it is right to. MEASURED 2026-09-12: it refused exactly that
#:     during this migration's first hour. Composing the literal from a variable
#:     to get past it would satisfy the hook and defeat its purpose.
#:   * They change on HIS schedule, not the engine's. A new course or a new
#:     property is not an engine release.
#:
#: Absent, nothing happens and the generic table stands alone, which is the
#: normal state of a public clone with no overlay.
LOCAL_RULES_FILENAME = "rules-local.tsv"


def load_local_rules(path) -> tuple[tuple[str, str | None, str | None, str], ...]:
    """The operator's own deeper rules, read from a TSV in the data overlay.

    Columns: prefix, family, leaf, method. A `-` in family or leaf means None,
    matching the generic table's convention for "a second pass decides this".

    Returned rules are PREPENDED to `RULES` by the caller, because they are
    strictly more specific than the generic ones they sit under: a rule for one
    named subtree must beat the rule for the directory containing it, and first
    match wins.

    A malformed row REFUSES rather than being skipped. A silently dropped rule
    sends a whole subtree to the wrong family, and it does it quietly, which is
    the failure this entire migration is built to prevent.
    """
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return ()

    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            raise ValueError(
                f"{p}:{n}: expected 4 tab-separated columns "
                f"(prefix, family, leaf, method), found {len(parts)}: {line!r}")
        prefix, family, leaf, method = (s.strip() for s in parts)
        if prefix in ("prefix", ""):      # the header row, or a blank key
            continue
        family = None if family in ("-", "") else family
        leaf = None if leaf in ("-", "") else leaf
        if method not in ("rule", "frontmatter", "judgement", "skip"):
            raise ValueError(f"{p}:{n}: unknown method {method!r}")
        if family and leaf and not is_valid(family, leaf):
            raise ValueError(f"{p}:{n}: {family}/{leaf} is not a defined pair")
        out.append((prefix, family, leaf, method))
    return tuple(out)


def store_rules(data_root: Path) -> tuple[tuple[str, str | None, str | None, str], ...]:
    """The `skip` row for the runtime store as `data_root`'s registry names it.

    `RULES` keeps its `runtime-junk/` row, the default name and the one every
    tree without a `runtime_store:` entry uses. A tree whose registry moves the
    store (HOS-5 F1, 2026-09-25) needs the row under the new name too, and the
    callers that assemble a rule list for a tree put this in front of `RULES`.
    """
    from scripts.utils.runtime_dir import store_dirname

    return ((f"{store_dirname(Path(data_root))}/", None, None, "skip"),)


def family_roots() -> tuple[str, ...]:
    """The nine top-level directory names the new tree uses, in vocabulary order.

    For a GUARD or a BOUNDARY: a hook that redirects writes into the data tree,
    a lint that refuses a data-path literal in engine code, a scanner that
    decides which files it may read. Each of those names `outputs`,
    `knowledge/` or `crm/` today, and after the move the same records sit under
    these names instead; a boundary that names only the old roots is a wall
    with the new tree on the wrong side of it. Derived from `FAMILIES`, never
    typed, so a family added there is inside every boundary the same day.

    Importing this module reads no file, so a hook that runs on every tool
    call can call this without paying for the manifest.
    """
    return tuple(FAMILIES)


def subject_roots(data_root: Path | None = None) -> tuple[str, ...]:
    """The C3 subject-tree roots, for the same GUARDS `family_roots()` serves.

    A boundary that knows the family roots and the legacy roots but not these
    has the moved batches on the wrong side of it. Every guard asking "is this
    a data root" takes both calls, never one.

    Read from the registry's `subject_roots:` line (HOS-234, 2026-10-13: the
    registry is the only address book), so a root added or renamed there is
    inside every guard the same moment, with no engine edit. The read is one
    file and a regular expression, `registry_bootstrap.subject_roots_from_files`,
    never a YAML parse, because the guards that ask run on every tool call.

    FAIL-CLOSED fallback, chosen because every caller is a wall: a tree whose
    registry lists no roots, a tree with no registry (a public clone, a test
    root) and a machine whose data root cannot be determined are all answered
    from the engine's default file, which lists the full generic subject tree.
    So is a registry that spells the line wrongly or names a root that shadows
    an engine directory, said on stderr: the resolver refuses that registry
    loudly on its own (`data_path._parse_roots`), and a wall must keep standing
    meanwhile. A wall that falls back to the full set refuses a little more
    than it must; one that fell back to an empty set would stop refusing at all.
    Only a broken engine default raises.
    """
    import sys

    from scripts.utils.paths import DataRootError, get_data_root, get_workspace_root
    from scripts.utils.registry_bootstrap import RootsKeyError, subject_roots_from_files

    engine = get_workspace_root()
    if data_root is None:
        try:
            data_root = get_data_root()
        except DataRootError as exc:
            print(f"[families] no data root ({exc}); subject roots from the engine's "
                  f"default registry", file=sys.stderr)
    try:
        return subject_roots_from_files(data_root, engine)
    except RootsKeyError as exc:
        if data_root is None:
            raise
        print(f"[families] {exc}; subject roots from the engine's default registry",
              file=sys.stderr)
        return subject_roots_from_files(None, engine)


# A regex fragment for the scanners that match `<root>/` in engine TEXT, placed
# straight after the root's slash. `people/me` and `people/c<digits>` (with  # leak-guard: ok (guard vocabulary: People API resource names the fragment excludes)
# `people/cXXX` as the placeholder) are Google People API resource names, the
# contacts connector's vocabulary, never a data address. MEASURED 2026-10-13:
# they were 16 of the 28 hits the subject roots added across the three scanners.
NOT_A_RESOURCE_NAME = r"(?!(?<=people/)(?:me|c[0-9X]+)\b)"


def address_kind(rel: str) -> tuple[str, str] | None:
    """The kind a path DECLARES by sitting at `<family>/<leaf>/...`, or None.

    After the move the address is not a hint about the record, it IS the
    record's classification: the mover put the file there because the manifest
    said that was its kind, so reading the first two segments recovers the
    decision rather than guessing at it.

    Three answers, and the middle one is the one worth stating:

        ("trace", "handoff")  the top segment is a family and the second is a
                              leaf that family declares.
        ("canon", "")         the top segment is a family and the second is
                              not a leaf of it: a family README, or a
                              directory nobody has placed. The FAMILY is
                              settled and the leaf is not, which is a smaller
                              question than "what is this file", and the
                              caller reports it as one rather than as nothing.
                              A bare `canon` with no second segment answers
                              this too; it cannot occur while the directory of
                              that name exists, and the family is still the
                              honest half of the answer if it ever does.
        None                  the top segment is not one of the nine families.
                              Every legacy address lands here and falls
                              through to `RULES` unchanged.

    Derived from `FAMILIES` and nothing else, so it cannot drift from the
    vocabulary: a family or a leaf added there is readable by this function the
    same day, with no second table to update. That matters more than it sounds
    -- the whole reason this function exists is that `RULES` is a hand-written
    list of the addresses of 2026-09-12, and MEASURED 2026-09-19 on the
    operator's overlay, every one of the 9,523 files that had since moved under
    a family root matched no rule in it at all.

    A legacy top-level directory is deliberately NOT accepted here, including
    `state/`, which `vocabulary_family` maps onto the `status` FAMILY. The
    directory `state/` is not the family root `status/`; treating it as one
    would classify the gitignored runtime tree as migrated records.
    """
    top, _, rest = rel.partition("/")
    leaves = FAMILIES.get(top)
    if leaves is None:
        return None
    second, _, tail = rest.partition("/")
    # `tail` is what makes `second` a DIRECTORY rather than a filename. Without
    # it, `canon/principle` is a file the operator happened to name after a
    # leaf, and answering `canon/principle` for it would file one file as a
    # whole kind.
    if tail and second in leaves:
        return top, second
    return top, ""


def is_valid(family: str, leaf: str) -> bool:
    """True when `family/leaf` is a pair this vocabulary actually defines.

    The manifest generator calls this on its own output, so a typo in the rules
    table is refused here rather than travelling into the tree.
    """
    return leaf in FAMILIES.get(family, ())


def check_rule_order() -> list[str]:
    """Return the rules shadowed by an earlier, broader prefix.

    An empty list means every rule is reachable. A non-empty one names a rule
    that can never fire -- invisible by reading, in a table this long.
    """
    shadowed = []
    for i, (prefix, _, _, _) in enumerate(RULES):
        for earlier_prefix, _, _, _ in RULES[:i]:
            if prefix.startswith(earlier_prefix):
                shadowed.append(f"{prefix!r} is shadowed by {earlier_prefix!r}")
                break
    return shadowed


#: How the workspace's one classification input maps onto this ladder.
#:
#:   private   -> the operator's own. Never published anywhere, and the value
#:                `get_classification()` already collapses to `ceo-only`.
#:   corporate -> retired 2026-09-25 (HOS-132); the resolver no longer
#:                answers it, so it is not in the table and would raise.
#:   engine    -> ships in the public engine repository, so it is already public.
#:
#: An unrecognised destination is NOT mapped. It raises, because a destination
#: this table has not been taught is a change somebody made to the routing map,
#: and guessing at it is how a new value silently becomes the least restrictive.
#: `engine` maps to `ceo-only`, not to `open`, and the reason is the tree this
#: function classifies. Every path it sees lives in the DATA repository, and
#: the operator's ruling of 2026-09-13 is that nothing there can ever be
#: `engine` or `open`. The routing map's engine rules are PATH-shaped
#: (`scripts/`, `tests/`, `docs/`, `.gitignore`) and match a data-tree file
#: with the same relative path just as well: MEASURED 2026-09-13 on the
#: reissued manifest, 19 files of the data repository resolved `engine`, among
#: them private scripts that live in the overlay precisely because they carry
#: a roster. A rule written for the public tree is not a decision to publish a
#: file that happens to share its path, so the answer is clamped closed here,
#: at the one place HEADING OS 2 asks.
_DESTINATION_TO_SENSITIVITY = {
    "private": "ceo-only",
    "engine": "ceo-only",
}


def sensitivity_for(rel_path: str) -> str:
    """The sensitivity a path carries, ASKED OF THE ROUTING MAP.

    `config/routing-map.yaml` is this workspace's SINGLE classification input,
    resolved by `get_routing_destination()`. This function does not decide
    anything; it translates that answer onto the three-value ladder.

    ## Why this is not a list of prefixes any more

    It was, until 2026-09-13, and the list was mine: one path segment and three
    directory prefixes I wrote down from memory instead of asking the resolver
    that already existed. MEASURED the day it was replaced, over a real
    workspace of 11,857 files: the routing map called 10,008 of them `private`
    while that hand-written list called them `internal`. Eighty-four per cent of
    the repository, wrong in the UNSAFE direction.

    That was not a near miss. A downstream task used this column as the gate
    deciding which files could be sent to an outside model for classification,
    and the gate was empty: it looked strict, it refused the handful of paths my
    list happened to name, and it waved through nearly everything else. Files
    the routing map calls private reached a third-party provider before the
    disagreement was measured, and that cannot be undone.

    The failure has a name in this repository and it is the dominant one: a
    second copy of a decision that already had one authority. The fix is not a
    better list. It is to delete the list and ask.

    ## Fail closed, in both directions

    `load_routing_map()` already fails closed to `private` when it cannot read
    the file, so an unreadable map yields `ceo-only` here rather than an
    optimistic default. And a destination this module does not recognise raises
    instead of falling back, because the cost of guessing is the paragraph
    above.

    A record stating its own sensitivity in frontmatter may override this
    downward only where the operator has said so; nothing here does that on its
    own.
    """
    from scripts.utils.workspace import get_routing_destination

    # `tree="data"`: every path this function sees lives in the data repository,
    # so an engine-shaped name in the overlay (`scripts/x.py`) clamps closed.
    destination = get_routing_destination(rel_path, tree="data")
    try:
        return _DESTINATION_TO_SENSITIVITY[destination]
    except KeyError:
        raise ValueError(
            f"routing-map.yaml resolved {rel_path!r} to the destination "
            f"{destination!r}, which this module has not been taught. Refusing "
            "to guess a sensitivity: add it to _DESTINATION_TO_SENSITIVITY "
            "deliberately, and decide which way it fails."
        ) from None
