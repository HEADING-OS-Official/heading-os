"""Workspace path resolution and .env loading.

One workspace shape: the operator's engine clone plus its private data
overlay. `.workspace-identity.json` at the workspace root still names the type
(`ceo-master`); the second type, an executive's two-layer workspace, was
removed with the executive model on 2026-09-25 (HOS-130). The readers of that
file here, `get_workspace_identity`, `is_ceo_workspace` and `is_admin`, had no
caller left and were removed on 2026-09-28 (HOS-162); `scripts/setup.py` and
`scripts/apply-wizard-answers.py` read the file themselves.

Root resolution and .env loading live in scripts/utils/paths.py (the single
source of truth). They are re-exported here so the long-standing
`from scripts.utils.workspace import get_workspace_root` import keeps working
unchanged. `home` is re-exported too. The helpers that wrote into the engine
checkout (`data_dir`, `state_dir`, `log_dir`, `private_cache_dir`) were deleted
on 2026-09-25 (HOS-5 section 7): every artifact now goes to the data
repository through `scripts/utils/data_path.py`.
"""

import functools
import os
import sys
from pathlib import Path

# Re-export the canonical root resolver and helpers from paths.py.
# Backward compatibility: existing imports of get_workspace_root / load_env
# from this module resolve to the hardened implementations.
#
# The MODULE is imported as well as the names, and `routing_tree_for` is why: it
# has to ask paths.py what root IT resolved, which is a different question from
# what the (patchable) name below answers. See the coherence check there.
from scripts.utils import paths as _paths
from scripts.utils.paths import (  # noqa: F401
    DATA_SCHEMA_VERSION,
    DataRootError,
    check_schema_compatible,
    data_overlay_present,
    data_root_is_demo,
    engine_siblings_root,
    env_data_root,
    get_data_root,
    get_workspace_root,
    home,
    iter_env_pairs,
    load_env,
    parse_env_line,
    read_data_schema_version,
    read_env_value,
    require_outside_engine_clone,
    require_writable_data_root,
    resolve_tz_name,
)


_TZ_ENV_LOADED = False


def get_default_tz_name() -> str:
    """Per-instance local timezone NAME (IANA). Defaults to UTC; the live
    instance sets HEADING_OS_TZ (e.g. America/New_York) in its gitignored .env.
    Externalized so the engine ships no operating-location signal.

    Loads that .env itself, once per process. HEADING_OS_TZ reaches os.environ
    only through load_env() and nothing exports it into the shell, so reading
    the environment alone answered UTC for every caller that did not separately
    call load_env() -- 61 of the 83 files that import this helper. Precedence is
    load_env's, unchanged: it uses setdefault, so an explicitly exported zone
    still wins over the file.

    The name itself comes from paths.resolve_tz_name(), which owns the BLANK
    case for both readers. This function read the variable directly until
    2026-09-01 and answered "" for a `.env` carrying a bare `HEADING_OS_TZ=`,
    so get_default_tz() raised ValueError out of a helper documented to default
    to UTC, while `python -m scripts.utils.paths tz` answered UTC for the same
    file. A fix that landed in one of two readers. A test held both readers to
    the same answer until the 2026-09-21 test-suite split; check them together
    by hand now."""
    global _TZ_ENV_LOADED
    if not _TZ_ENV_LOADED:
        load_env()
        _TZ_ENV_LOADED = True
    return resolve_tz_name()[0]


def get_default_tz():
    """Per-instance local timezone as a ZoneInfo. See get_default_tz_name()."""
    from zoneinfo import ZoneInfo
    return ZoneInfo(get_default_tz_name())


def make_run_clock():
    """Return a `_clock()` for one module: its single wall-clock reading per run.

    The returned function reads `datetime.now(get_default_tz())` the first time
    it is called and returns that same instant on every later call. Each call to
    `make_run_clock` makes an independent memo, so every module that builds one
    keeps its own reading, exactly as the four private copies it replaced did
    (`crm-health.py`, `generate-crm-dashboard.py`, `generate-dashboard.py`,
    `knowledge-health.py`; consolidated 2026-09-28, HOS-162).

    Why first use and not import: those modules held module-level constants
    built by `datetime.now(get_default_tz())` until 2026-09-24. `get_default_tz()`
    reaches `get_default_tz_name()`, which calls `load_env()`, so evaluating the
    constant wrote the whole `.env` into `os.environ` as a side effect of
    IMPORTING the module; and the instant was frozen at import, so a process
    that imported before midnight and ran after reported yesterday.

    Why memoised: each of those modules reads the date or time in several
    places (the output directory name, the page title, every age comparison),
    and reading the clock at each would let a run starting at 23:59:58 file a
    page under one date and title it with another. One reading per run.
    """
    from datetime import datetime

    reading = None

    def _clock() -> datetime:
        nonlocal reading
        if reading is None:
            reading = datetime.now(get_default_tz())
        return reading

    return _clock


def display_path(path) -> str:
    """Human-readable relative path for display, manifests, and logs.

    After the engine/data split a workspace file may live under the ENGINE root,
    or the DATA root. A bare ``path.relative_to(<one root>)``
    raises ``ValueError`` whenever the file actually lives under a *different*
    root -- the "data-root seam" bug that hit knowledge-health,
    capture-design-exemplars, and odin-skill-proposal. This resolver tries each
    known root in turn (data, engine) and degrades to the absolute path rather
    than raise. Separators are normalised to '/'.

    Use this anywhere a workspace path is turned into a string for a human or a
    manifest. Do NOT use it where a path must be relative to one specific root
    (those callers should keep their explicit ``relative_to``).
    """
    p = Path(path)
    for getter in (get_data_root, get_workspace_root):
        try:
            base = getter()
        except DataRootError:
            # No resolvable data root in this environment; try the next base.
            # This helper turns paths into strings for humans, manifests and
            # LOGS, and the docstring above promises it degrades to the
            # absolute path rather than raise.
            continue
        try:
            return str(p.relative_to(base)).replace("\\", "/")
        except ValueError:
            continue  # path is not under this base; try the next one
    return str(p).replace("\\", "/")


def get_knowledge_dir() -> Path:
    """Get the personal knowledge directory."""
    return get_data_root() / "knowledge"


def get_reference_dir() -> Path:
    """Get the reference directory: ENGINE content, under the workspace root.

    It answered the corporate clone's `reference/` for an executive workspace
    until 2026-09-23; both that layer and the executive model are gone (HOS-130).
    One live caller: `find_search_domains_file()` in `scripts/firecrawl.py`.
    """
    return get_workspace_root() / "reference"


def get_auto_memory_dir() -> Path:
    """Durable canonical auto-memory fact store in the DATA overlay."""
    return get_data_root() / "auto-memory"


def get_templates_dir() -> Path:
    """Get the templates directory (shared-doc source of truth — private CEO data).

    templates/ routes `private` (config/routing-map.yaml), so it lives under the
    data overlay (.heading-os-data/templates for the CEO), NOT the engine root.
    The sync-docs.py PostToolUse hook copies templates/ -> docs/ for distribution.
    Resolving under the engine root (the pre-data-seam behaviour) made the health
    check report every shared doc as "missing" — these files are on the data side.

    Through `moved_home` since 2026-09-18. MEASURED that day on the operator's
    overlay: `<data>/templates` was ABSENT and its 12 records sat at the
    address now named `templates`, so this helper answered a directory that
    does not exist. Nothing raised;
    `workspace-health.py::check_docs_sync` read the absence as "no data overlay",
    printed "templates/ is not present (no data overlay); 0 file pairs compared"
    and returned ZERO issues, and `check_doc_versions` degraded the same way
    behind the same helper. AFTER: the `templates` address, 12 files.

    `moved_home`, not `write_dir_or_legacy`: the kind's records fan out over
    three legacy directories (admin, since removed, `config/` and `templates/` all land under
    `build/config`), so the write seam answers `build/config` — one directory
    too high — while `moved_home` derives THIS directory's new home from the
    records that were in it. It falls back to `legacy` before the move, on a
    public clone, on an exec overlay and on any tree with no manifest, so this
    is a fix and not a relocation.

    Through the registry name `templates` since 2026-09-25, when HOS-5 section
    6 deleted `moved_home`. MEASURED that day: the same path as the seam call
    on the live overlay, on an empty data root and on the bundled `examples/`
    tree, the last two being the legacy directory, as before.
    """
    from scripts.utils.data_path import data_path  # a module-level import is a cycle
    return data_path("templates")


def get_data_config_dir() -> Path:
    """Get the config directory for *instance config-DATA* (not engine config).

    A handful of config/ files carry real per-instance data, not shareable engine
    logic: email-triage-rules.yaml and service-manifest.json (and
    x-pulse-accounts.yaml until HOS-166 removed /x-pulse on 2026-09-28). These
    resolve under the DATA
    root (.heading-os-data/config for the CEO), NOT the engine root -- so a
    data-less engine clone reads them from the data sibling instead of finding
    them absent. The engine ships generic examples; the real files live in the
    data overlay (routed private).

    Distinct from the engine's own `config/` under the workspace root, which
    holds genuinely shareable config (routing-map.yaml, schemas/, tool-risk.json,
    wizard-*, llm_fallback.yaml, memory-index.yaml).

    Through `moved_home` since 2026-09-18, for the same reason as
    `get_templates_dir` above and with the same fallback. MEASURED that day on
    the operator's overlay: `<data>/config` was ABSENT and its 18 records sat at
    the address now named `data-config`. Nothing raised, and the callers are written to treat an absent file
    as a normal empty answer: `resolve_config_with_example()` two frames below
    would have handed every caller the engine's generic `*.example.*` instead of
    the operator's real private config. AFTER: the `data-config` address,
    18 files.

    Through the registry name `data-config` since 2026-09-25, on the same
    measurement as `get_templates_dir` above.
    """
    from scripts.utils.data_path import data_path  # a module-level import is a cycle
    return data_path("data-config")


def resolve_config_with_example(filename: str, example: Path) -> Path:
    """Resolve an instance config-DATA file with an engine-example fallback.

    Returns the real file under the data-config dir when it exists, else the
    engine-shipped example. This lets a data-less engine clone run on bundled
    defaults while a real deployment uses its private config in the data overlay.
    The standard pattern for any "code ships an example, real config is private
    data" file (sentinel, etc.).
    """
    real = get_data_config_dir() / filename
    return real if real.exists() else example


# ============================================================
# The HEADING OS 2 migration seam: DELETED 2026-09-25
# ============================================================
#
# HOS-5 section 6: "remove the live seam, the fallback to old addresses, the
# migration redirects and the wrappers that are no longer needed." `moved_home`,
# `moved_homes`, `moved_dir`, `moved_file` and `_registry_path` stood here and
# answered, for a pre-move address, where the HEADING OS 2 move had put it,
# falling back to the pre-move address wherever nothing answered. Every caller
# now asks the front door, `scripts/utils/data_path.py`, by registry name, and
# a registry name with a `record:` base and an `under:` line degrades to the
# same pre-move address on a tree with no manifest, which is the one behaviour
# the seam existed to keep. The bare `record:<family>/<leaf>` scheme form does
# not: it carries no pre-move address, and it refuses there.
#
# MEASURED 2026-09-25, per converted site, the new expression against the seam
# call it replaced, on the live overlay, on an empty data root and on the
# bundled `examples/` tree. Every difference found is named at its site; the
# only one that is not the deleted fallback itself is
# `scripts/odin-skill-proposal.py`, an address change for a directory that
# holds nothing at either address. The seam's own reasoning, including the
# measurements behind `moved_homes`' tree walk that `heading_os_2.kinds.
# homes_under` replaced, is in the git history of this file.


@functools.lru_cache(maxsize=4)
def _load_routing_map_cached(path: str, mtime_ns: int, size: int) -> dict:
    """Parse one routing map. Keyed on file IDENTITY (path + mtime + size), never bare.

    A bare ``lru_cache`` on ``load_routing_map()`` would make a long-running daemon
    (sentinel, as the bridge daemon was until HOS-7) blind to an edit of routing-map.yaml — and that file is
    the classifier deciding what counts as private data, so blindness there is a leak
    path, not a staleness annoyance. Keying on mtime_ns + size means an edited map is
    a cache MISS and gets re-parsed on the next call.

    Fails closed exactly as the uncached loader did: any read/parse error yields
    default 'private'.
    """
    import yaml

    from scripts.utils import yamlio

    try:
        with open(path, encoding="utf-8") as fh:
            data = yamlio.safe_load(fh) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        # UnicodeDecodeError is a ValueError, NOT an OSError and not a
        # YAMLError, so one invalid byte in the map escaped both clauses and
        # crashed the classifier instead of failing closed. Measured 2026-08-30
        # with `default: \xffprivate` in routing-map.yaml:
        # `get_routing_destination("crm/x.md")` raised UnicodeDecodeError. This  # leak-guard: ok (measurement: the address as it was on the day measured)
        # resolver is called once per tracked file by the push wall and the
        # engine-tree-clean pre-commit hook, and the docstring above promises
        # "any read/parse error yields default 'private'" precisely so a
        # corrupted map cannot take the leak wall down.
        return {"default": "private", "data_default": "private", "rules": {}}
    return _routing_map_from_data(data)


def routing_map_from_text(text: str) -> dict:
    """Parse routing-map YAML held in memory, failing closed exactly as a file does.

    For a caller that holds a map which is not the one on disk: the push wall
    reads the map as it stood at `origin/main` through `git show`. One parser,
    so the version in history and the version on disk are judged by the same
    fail-closed rules; a second copy of them is how the two would drift.
    """
    import yaml

    from scripts.utils import yamlio

    try:
        data = yamlio.safe_load(text) or {}
    except yaml.YAMLError:
        return {"default": "private", "data_default": "private", "rules": {}}
    return _routing_map_from_data(data)


def route_by_map(rel: str, routing_map: dict) -> str:
    """The destination of an ENGINE-tree relative path under `routing_map`.

    Longest matching key wins, as in `matched_routing_rule`; unmatched falls to
    the map's `default`. No tree inference and no overlay lookup: the caller
    hands in the complete rule set it means to judge by.
    """
    norm = rel.replace("\\", "/").lstrip("/")
    best_key = None
    for key in routing_map["rules"]:
        k = key.rstrip("/")
        matches = norm == k or norm.startswith(k + "/")
        if matches and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return routing_map["default"]
    return routing_map["rules"][best_key]


#: The destinations a routing map may declare. `corporate` was a third until
#: 2026-09-25: it named content shared down to the executive workspaces, and
#: the executive model left the engine that day (HOS-130). HOS-132 retired the
#: destination. A map that still declares it is REFUSED, never mapped to a
#: live meaning: the loaders read the rule as `private` and say so on stderr,
#: and `scripts/classification-health.py` fails on it.
LEGAL_DESTINATIONS = frozenset({"engine", "private"})
RETIRED_DESTINATIONS = frozenset({"corporate"})


def _retired_note(value) -> str:
    """The clause naming a retired destination, or nothing for any other value."""
    if isinstance(value, str) and value in RETIRED_DESTINATIONS:
        return (f" (the {value!r} destination was retired on 2026-09-25, "
                f"HOS-132)")
    return ""


def _routing_map_from_data(data) -> dict:
    """Validate a parsed routing map. The shared, fail-closed half of both loaders."""
    # Valid YAML of the WRONG SHAPE is a parse error by any reading the
    # docstring above supports, and it used to raise instead. A `rules:` block
    # written as a list (a stray `-`, the commonest YAML slip) reached
    # `rules.items()` and threw AttributeError out of a resolver that every
    # classification call sits on. Found 2026-08-26 while testing the coercion
    # below; the crash predates it. Fail closed, like every other bad read here.
    if not isinstance(data, dict):
        return {"default": "private", "data_default": "private", "rules": {}}
    default = data.get("default", "private")
    rules = data.get("rules") or {}
    if not isinstance(rules, dict):
        print(f"[workspace] routing-map `rules:` is a {type(rules).__name__}, "
              f"not a mapping; failing closed to 'private' for every path",
              file=sys.stderr)
        return {"default": "private", "data_default": "private", "rules": {}}
    legal = LEGAL_DESTINATIONS
    # A destination LEAF has to be a string before it can be asked about. Both
    # membership tests below hash the candidate against `legal`, and a list- or
    # dict-valued leaf is unhashable, so it raised TypeError straight out of
    # this loader. Measured 2026-08-31: `rules: {crm/: [private]}` and
    # `default: [private]` each raised `TypeError: unhashable type: 'list'`,
    # and `get_routing_destination` has no handler for it while its own
    # docstring promises "Fails closed: load_routing_map() already defaults to
    # 'private' on error". Same YAML slip as the `rules:`-written-as-a-list case
    # guarded fifteen lines up, one level further down the tree and missed
    # there. This resolver is called once per tracked file by the push wall and
    # the engine-tree-clean hook, so it must answer rather than raise.
    if not isinstance(default, str):
        print(f"[workspace] routing-map `default:` is a {type(default).__name__}, "
              f"not a destination name; failing closed to 'private'",
              file=sys.stderr)
        return {"default": "private", "data_default": "private", "rules": {}}
    if default not in legal:
        print(f"[workspace] routing-map `default:` is {default!r}, which is not "
              f"one of {sorted(legal)}{_retired_note(default)}; failing closed "
              f"to 'private'", file=sys.stderr)
        default = "private"

    # The DATA overlay's own default, added 2026-09-13 with the two-tree model
    # (see the header of config/routing-map.yaml). Three ways it can be wrong and
    # all three land on 'private', because this key exists to make the overlay
    # fail closed and a broken spelling of it must not undo that:
    #   absent      -> the map predates the two-tree model, and the operator's
    #                  directive governs a map that has not been updated too.
    #   not a str   -> same YAML slip the `default:` guard above was written for.
    #   not legal   -> a typo, which for the `rules:` values below was measured
    #                  on 2026-08-26 to silently reclassify a whole CEO subtree.
    data_default = data.get("data_default", "private")
    if not isinstance(data_default, str) or data_default not in legal:
        if data_default != "private":
            print(f"[workspace] routing-map `data_default:` is {data_default!r}, "
                  f"which is not one of {sorted(legal)}"
                  f"{_retired_note(data_default)}; failing closed to "
                  f"'private' for unruled paths under the data overlay",
                  file=sys.stderr)
        data_default = "private"

    # An illegal destination on a RULE now fails CLOSED to 'private'. It used to
    # be dropped from the map entirely, and a dropped rule is not a neutral act:
    # the path it governed falls through to `default`, and this workspace's real
    # `config/routing-map.yaml` reads `default: engine` — the PUBLIC repository.
    #
    # Reproduced 2026-08-26 against a scratch map holding `default: engine` and
    # rules {"outputs/": "privat", "crm/": "private"}, one character wrong on the
    # first: `load_routing_map()` returned only the crm rule,
    # `get_routing_destination("outputs/secret.md")` answered 'engine', and  # leak-guard: ok (measurement: the address as it was on the day measured)
    # nothing was printed. So a typo in one value silently reclassified a whole
    # CEO-data subtree as shareable, and the loader's own docstring promises the
    # opposite: "any read/parse error yields default 'private'". A misspelled
    # value is a parse-level defect and this is the direction it must fail in.
    coerced = {}
    for key, value in rules.items():
        if not isinstance(value, str):
            print(f"[workspace] routing-map rule {key!r} has a "
                  f"{type(value).__name__} where a destination name belongs; "
                  f"treating {key!r} as 'private' rather than letting it fall "
                  f"through to the '{default}' default", file=sys.stderr)
            coerced[key] = "private"
            continue
        if value in legal:
            coerced[key] = value
            continue
        print(f"[workspace] routing-map rule {key!r} has destination {value!r}, "
              f"which is not one of {sorted(legal)}{_retired_note(value)}; "
              f"treating it as 'private' "
              f"rather than letting {key!r} fall through to the '{default}' "
              f"default", file=sys.stderr)
        coerced[key] = "private"
    return {"default": default, "data_default": data_default, "rules": coerced}


# ------------------------------------------------------------
# PRIVACY BY REPOSITORY, since HOS-232 (2026-10-12). The operator's words that
# day, translated: "everything in .heading-os-data is mine, nobody else sees it
# and nobody receives it ... .heading-os is the engine, and nothing that MUST be
# only in .heading-os-data may ever get into it." So the question "what is this file" has four answers
# and the per-path map of the data repository is no longer read:
#
#   in the data repository                     -> private
#   in the engine AND in the engine allowlist  -> engine
#   in the engine and NOT in the allowlist     -> RoutingRefused('boundary')
#   outside both, unresolvable or ambiguous    -> RoutingRefused(<why>)
#
# The data repository's own rule file carried 217 rules on 2026-10-12, every
# one `private`, and its header said that removing any of them changed no
# answer once `corporate` was retired (HOS-130/132). Re-measured by the yard
# that made this change over every file of both trees before and after: no
# tracked engine file and no data file changed its answer; the differences are
# listed in the closing note of HOS-232.
#
# WHICH FILE SAYS WHAT THE ENGINE IS. `config/engine-allowlist.yaml`, read
# through `scripts/utils/engine_allowlist.py`: it is the operator's definition
# (HOS-131), per file with a reason, and the gate that refuses at commit and
# push (`scripts/check-engine-only.py`) reads it with the same matcher. This
# file's `rules:` stay a prefix map other tools read, and they can only CLOSE:
# a path answers `engine` when the allowlist covers it AND the longest rule
# here says `engine`. Two lists that must both agree cannot drift into a leak;
# they can only drift into a refusal, which is loud.
#
# `data_root=` is still accepted by the resolvers below, for a caller that
# holds an overlay root: it decides where an absolute path or the tree
# inference looks for the data repository. It can never open anything, because
# nothing in the data repository answers anything but `private`.
# ------------------------------------------------------------


class RoutingRefused(ValueError):
    """The routing contract's third answer: neither `engine` nor `private`.

    `reason` is one of:
      boundary          an engine path the allowlist does not cover
      unresolvable      a path that exists in neither tree and no allowlist
                        entry or data-only first segment places
      ambiguous         a bare relative path that exists in BOTH trees
      escape            `..` leaves the tree it was asked about
      outside           an absolute path under neither root, or a link whose
                        target is outside the engine
      symlink-into-data an engine path whose real location is in the data root
      data-in-engine    an engine path the data repository also holds (the
                        data-to-engine tripwire)
      nested-repo       an engine path inside a nested git repository
      no-allowlist      the engine allowlist is absent or unreadable

    A ValueError so a caller that already refuses on a bad question refuses on
    this one too. Never read it as `private`: the point of the third answer is
    that a boundary violation is reported as one.
    """

    def __init__(self, path: str, reason: str, detail: str):
        self.path, self.reason = path, reason
        super().__init__(f"{path!r} refused ({reason}): {detail}")


#: Repository plumbing both trees carry at the same relative path, each its
#: own repository's file. MEASURED 2026-10-12 over every tracked engine file
#: and every data file: three same-path pairs, these two and the untracked
#: `.claude/settings.local.json`, which is each checkout's own runtime file
#: and stays refused. Any other pair trips `data-in-engine`.
_SHARED_PLUMBING = frozenset({".gitignore", ".gitattributes"})


def load_routing_map(*, data_root: Path | None = None) -> dict:
    """Load the engine routing map. Returns {default, data_default, rules}.

    ONE FILE SINCE HOS-232: `config/routing-map.yaml` in the engine checkout.
    The data repository's rule file is no longer read; see the block above.
    `data_root` is accepted for the callers that pass it and changes nothing
    here.

    Fails closed: on any error, returns a map whose default is 'private' and
    which carries no rules, so no path can answer 'engine' from it.

    Cached on file identity (2026-08-20). Before the cache this re-parsed the
    YAML on EVERY get_routing_destination() call, measured 6.4 ms per call.
    Returns a fresh copy per call so a caller mutating `rules` cannot corrupt
    the shared cached map for the rest of the process.
    """
    path = get_workspace_root() / "config" / "routing-map.yaml"
    try:
        st = path.stat()
        mtime_ns, size = st.st_mtime_ns, st.st_size
    except OSError:
        # No file / unreadable stat: fail closed, and do not poison the cache.
        return {"default": "private", "data_default": "private", "rules": {}}
    m = _load_routing_map_cached(str(path), mtime_ns, size)
    return {"default": m["default"], "data_default": m["data_default"],
            "rules": dict(m["rules"])}


def matched_routing_rule(file_path: str, *, tree: str | None = None,
                         data_root: Path | None = None) -> str | None:
    """The engine routing-map key that governs `file_path`, or None if none does.

    `file_path` and `tree` mean what they mean for `get_routing_destination`:
    an absolute path is located in its tree and matched RELATIVE to that root.
    A path known to be in the DATA tree has no governing key since HOS-232: the
    data repository is private by repository, not by rule.

    Split out of `get_routing_destination` on 2026-08-24 so a caller can tell
    "this path has an explicit rule" from "this path fell through to the map
    default", which `classification-health.py --unclassified` needs.

    Most-specific (longest matching) key wins. A key ending in '/' matches the
    path as a directory prefix; a key without a trailing '/' matches either that
    exact file or that path as a prefix.
    """
    norm, known_tree = _rule_path(file_path, tree, data_root=data_root)
    if known_tree == "data":
        return None
    return _longest_rule(norm, load_routing_map()["rules"])


def _longest_rule(norm: str, rules: dict) -> str | None:
    best_key = None
    for key in rules:
        k = key.rstrip("/")
        if norm == k or norm.startswith(k + "/"):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    return best_key


@functools.lru_cache(maxsize=8)
def _top_level_names(root_str: str, mtime_ns: int) -> frozenset:
    """The names directly under `root_str`, keyed on that directory's mtime.

    mtime_ns is in the key and not read here on purpose: creating or removing a
    top-level entry bumps the directory's mtime, so a new family root is a cache
    MISS on the next call rather than an answer from before it existed.
    """
    try:
        return frozenset(p.name for p in Path(root_str).iterdir())
    except OSError:
        return frozenset()


def _dir_mtime_ns(root: Path) -> int:
    try:
        return root.stat().st_mtime_ns
    except OSError:
        return -1


def _overlay_root(data_root: Path | None) -> Path | None:
    """The data repository to compare against, or None when there is none.

    None on a public clone (`data_overlay_present()` False: there is no second
    tree) and when the two roots come from different resolutions: tests
    redirect the module-level `get_workspace_root` seam at a scratch tree while
    `get_data_root()` derives from `paths.get_workspace_root()`, and comparing a
    scratch engine with the operator's real overlay answers nothing true
    (MEASURED 2026-09-13: that mismatch turned five correct tests red). A root
    the CALLER supplied is used as given.
    """
    if data_root is not None:
        return Path(data_root)
    if not data_overlay_present():
        return None
    if _paths.get_workspace_root() != get_workspace_root():
        return None
    return get_data_root()


def routing_tree_for(file_path: str, *, data_root: Path | None = None) -> str:
    """Which tree a bare RELATIVE path belongs to: 'data' or 'engine'.

    Every caller hands this resolver relative paths and the string does not say
    which repository it means, so this is an INFERENCE, written out:

      0. No data repository to compare against (`_overlay_root` is None) ->
         'engine'.
      1. The path exists in BOTH trees -> RoutingRefused('ambiguous'), except
         the repository plumbing in `_SHARED_PLUMBING`, which is 'engine'. A
         caller that knows which file it means passes `tree=` or an absolute
         path. Until HOS-232 the engine won this tie silently.
      2. Exists in the engine only -> 'engine'; in the data tree only -> 'data'.
      3. Exists in neither and its FIRST SEGMENT is a directory the data root
         has and the engine root does not -> 'data'. This answers for a record
         about to be written under a family root only the data tree has.
      4. Else -> 'engine', where the engine allowlist decides: a deleted or
         not-yet-written engine file the allowlist covers answers 'engine',
         anything else is refused. `scripts/push-all.py` asks about files a
         later commit deleted, so this step must not refuse by itself.
    """
    norm = file_path.replace("\\", "/").lstrip("/")
    overlay = _overlay_root(data_root)
    if overlay is None or not norm:
        return "engine"
    engine_root = get_workspace_root()
    in_engine = os.path.lexists(engine_root / norm)
    in_data = os.path.lexists(overlay / norm)
    if in_engine and in_data:
        if norm in _SHARED_PLUMBING:
            return "engine"
        raise RoutingRefused(file_path, "ambiguous",
                             "the path exists in both the engine and the data "
                             "repository; pass tree= or an absolute path")
    if in_engine:
        return "engine"
    if in_data:
        return "data"
    first = norm.split("/", 1)[0]
    data_only = (first in _top_level_names(str(overlay), _dir_mtime_ns(overlay))
                 and first not in _top_level_names(str(engine_root),
                                                   _dir_mtime_ns(engine_root)))
    return "data" if data_only else "engine"


_ROUTING_TREES = ("data", "engine")


def _relative_to_a_tree(abs_norm: str,
                        data_root: Path | None = None) -> tuple[str, str] | None:
    """Locate an absolute POSIX path under the data root or the engine root.

    Returns `(relative_path, tree)` or None when it lies under neither. The
    data root is tried FIRST and only when a separate overlay exists
    (`data_overlay_present()`): in demo mode the "data root" is the engine's own
    `examples/`, which is engine-tracked and must keep answering as the engine.
    When an explicit `HEADING_OS_DATA` points INSIDE the engine clone, data-first
    is the closed order: the nested tree is the private one.

    The plain string comparison runs first and `.resolve()` only if it fails,
    because this is called once per file by gates that walk ten thousand of them.

    `data_root`, when given, is the overlay an absolute path is located under,
    in place of the ambient one. See the block above `load_routing_map`.
    """
    candidates: list[tuple[str, Path]] = []
    if data_root is not None:
        candidates.append(("data", Path(data_root)))
    elif data_overlay_present():
        candidates.append(("data", get_data_root()))
    candidates.append(("engine", get_workspace_root()))
    for resolve in (False, True):
        try:
            target = Path(abs_norm).resolve() if resolve else Path(os.path.normpath(abs_norm))
        except OSError:
            return None
        for tree, root in candidates:
            root_p = root.resolve() if resolve else Path(os.path.normpath(str(root)))
            if target == root_p:
                return "", tree
            if target.is_relative_to(root_p):
                return target.relative_to(root_p).as_posix(), tree
    return None


def _rule_path(file_path: str, tree: str | None,
               data_root: Path | None = None) -> tuple[str, str | None]:
    """The normalised relative path, and the tree it is known to be in.

      * an ABSOLUTE path under the data root or the engine root -> its path
        RELATIVE to that root, and that root's tree. A `tree` argument that
        contradicts where the path actually is raises: the caller has asked a
        question with a false premise.
      * an absolute path under NEITHER root -> RoutingRefused('outside'). Until
        HOS-232 it was read as a stray slash on a relative path, which let
        `/elsewhere/scripts/x.py` answer as the engine's `scripts/x.py`.
      * a relative path -> normalised (`a/./b`, `a//b`, `a/x/../b` -> `a/b`)
        with `tree` as given or None. One whose `..` leaves the tree is
        RoutingRefused('escape'): `scripts/../../<elsewhere>` started with
        `scripts/` and answered 'engine' until HOS-232.
    """
    import posixpath

    if tree is not None and tree not in _ROUTING_TREES:
        raise ValueError(f"tree must be one of {_ROUTING_TREES} or None, got {tree!r}")
    norm = file_path.replace("\\", "/")
    if norm.startswith("/"):
        located = _relative_to_a_tree(norm, data_root)
        if located is None:
            raise RoutingRefused(file_path, "outside",
                                 "an absolute path under neither the engine "
                                 "nor the data root")
        rel, found = located
        if tree is not None and tree != found:
            raise ValueError(
                f"{file_path!r} lies under the {found} root but the caller "
                f"said tree={tree!r}")
        return rel, found
    rel = posixpath.normpath(norm) if norm else ""
    if rel == ".":
        rel = ""
    if rel == ".." or rel.startswith("../"):
        raise RoutingRefused(file_path, "escape",
                             "`..` leaves the tree the path was asked about")
    return rel, tree


def _engine_destination(rel: str, original: str,
                        data_root: Path | None) -> str:
    """'engine' for an allowlisted engine path, else RoutingRefused. See the
    contract block above `RoutingRefused`."""
    from scripts.utils import engine_allowlist

    engine_root = get_workspace_root()
    if not rel:
        raise RoutingRefused(original, "unresolvable",
                             "the engine root itself is not a file")
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        if os.path.lexists(os.path.join(engine_root, *parts[:i], ".git")):
            raise RoutingRefused(original, "nested-repo",
                                 f"{'/'.join(parts[:i])} is a git repository "
                                 f"of its own inside the engine")
    overlay = _overlay_root(data_root)
    real = Path(os.path.realpath(engine_root / rel))
    if overlay is not None and real.is_relative_to(Path(os.path.realpath(overlay))):
        raise RoutingRefused(original, "symlink-into-data",
                             "its real location is inside the data root")
    if not real.is_relative_to(Path(os.path.realpath(engine_root))):
        raise RoutingRefused(original, "outside",
                             "its real location is outside the engine")
    if (overlay is not None and rel not in _SHARED_PLUMBING
            and os.path.lexists(overlay / rel)):
        raise RoutingRefused(original, "data-in-engine",
                             "the data repository holds the same path; data "
                             "never becomes engine")
    covered = engine_allowlist.covers(engine_root, rel)
    if covered is None:
        raise RoutingRefused(original, "no-allowlist",
                             f"{engine_allowlist.ALLOWLIST_REL} is absent or "
                             f"unreadable, so nothing can be confirmed as engine")
    rules = load_routing_map()["rules"]
    key = _longest_rule(rel, rules)
    if covered and key is not None and rules[key] == "engine":
        return "engine"
    why = ("not covered by " + engine_allowlist.ALLOWLIST_REL if not covered
           else "config/routing-map.yaml does not route it engine")
    if os.path.lexists(engine_root / rel):
        raise RoutingRefused(original, "boundary", f"an engine path {why}")
    raise RoutingRefused(original, "unresolvable",
                         f"in neither tree, and {why}")


def get_routing_destination(file_path: str, *, tree: str | None = None,
                            data_root: Path | None = None) -> str:
    """Resolve a path to 'engine' | 'private', or raise RoutingRefused.

    The contract (HOS-232), stated in full in the block above
    `RoutingRefused`: a data-repository path is 'private'; an engine path is
    'engine' when `config/engine-allowlist.yaml` covers it and
    `config/routing-map.yaml` routes it engine, and is refused otherwise;
    anything outside both, unresolvable or ambiguous is refused.

    `tree='data'` / `tree='engine'`, or an ABSOLUTE path, says which repository
    is meant. A bare relative path with no `tree` is placed by
    `routing_tree_for`, whose docstring says what that inference does.

    Fails closed: a broken routing map carries no rules and a broken allowlist
    covers nothing, so neither can make a path answer 'engine'.
    """
    rel, known_tree = _rule_path(file_path, tree, data_root=data_root)
    if known_tree is None:
        known_tree = routing_tree_for(rel, data_root=data_root)
    if known_tree == "data":
        return "private"
    return _engine_destination(rel, file_path, data_root)


def routing_verdict(file_path: str, *, tree: str | None = None,
                    data_root: Path | None = None) -> str:
    """`get_routing_destination`, with a refusal returned as 'refused'.

    For a gate that compares the answer: `!= "engine"` keeps meaning "may not
    ship", and a report can name the third answer instead of counting it as
    private. The refusal reason is lost here; a caller that prints one asks
    `get_routing_destination` and reads `RoutingRefused.reason`.
    """
    try:
        return get_routing_destination(file_path, tree=tree, data_root=data_root)
    except RoutingRefused:
        return "refused"


def get_classification(file_path: str, *, tree: str | None = None) -> str:
    """Resolve the two-value classification for a file path.

    `tree` and absolute paths mean what they mean for `get_routing_destination`:
    pass `tree='data'` or the absolute path for a file in the data overlay, so
    that an engine-shaped name in the overlay collapses to 'ceo-only'.

    HEADING OS step 7: this is a thin relabelling of the routing map
    (`config/routing-map.yaml`), the single classification input. The two-value
    question is "is this CEO-private?":

      routing 'private'   -> 'ceo-only'   (CEO data overlay, never shared)
      routing 'engine'    -> 'corporate'  (engine code is not private: it is the
                                           public repository)

    The label 'corporate' is kept because the memory index stores it per
    record; it no longer has a routing destination of the same name, which
    HOS-132 retired on 2026-09-25.

    Since HOS-232 every data-repository path is 'private' (-> 'ceo-only') by
    repository, and an engine path is 'engine' (-> 'corporate') only when the
    engine allowlist covers it. A path the resolver REFUSES raises
    RoutingRefused here too: a label is not a way round the third answer, and
    a caller that indexes it decides at its own call site what to do.
    """
    dest = get_routing_destination(file_path, tree=tree)
    return "ceo-only" if dest == "private" else "corporate"

