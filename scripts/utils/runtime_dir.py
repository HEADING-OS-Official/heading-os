"""The runtime store: its vocabulary, its root, and the root's creation.

    from scripts.utils.data_path import data_path, data_write_dir
    log_dir = data_write_dir("runtime:sync-exchange/logs")  # created, store root at 0700
    cache = data_path("runtime:docparse/cache")               # the same address, nothing created

A writer never asks this module where its junk goes; it asks the front door,
`scripts/utils/data_path.py`, which reads this module's vocabulary. Until HOS-5
§ 6 (2026-09-25) `get_runtime_dir(service, kind)` answered here as a second door
beside that one and created the directory as a side effect of being asked; it
was deleted then, on the operator's word of that day, "old way should go out and
do not stay in the workspace". The two refusals it carried are in
`data_path._runtime_dir`; the creation it did is `data_write_dir`, which calls
`ensure_store_root` below.

Runtime junk is what running the machine again would rewrite: a log, a sync
dump, a cache, a lock, a temp file, a queue the daemon drains, an index rebuilt
from disk. A RECORD is what a person reads or what cannot be re-run, and records
never come here; they have a family and a leaf under HEADING OS 2. Until
2026-09-14 the engine spelled the junk idea six ways (`outputs/_sync`, `.sync`,  # leak-guard: ok (measurement: the address as it was on the day measured)
`_tmp`, `.tmp`, `.cache`, `_derived`) across 69 writer sites (MEASURED that day
in the writers inventory), most of them TRACKED by the private overlay, so every
push carried a day of calendar and inbox into history. This module replaces
the six spellings with one tree, placed by the registry's `runtime_store:` key:

    <data root>/<runtime_store>/
      README.md                what this is; nothing here is a record
      <service>/
        logs/    append-only jsonl and text logs: durable, tracked
        cache/   rebuildable from a source: ignored
        state/   queues, cursors, locks, run records: durable, tracked
        tmp/     scratch the service itself deletes or overwrites

Two refusals, both at the call and never later, raised by the front door as
this module's classes and re-dressed as `DataPathError`:

  * `kind` outside {logs, cache, state, tmp} is `UnknownKind`.
  * `service` outside the registry's `services:` block is `UnknownService`.
    The list is spelled out there with one line per service, so a typo in a
    writer is a refusal and not a new folder that nobody owns.

## Where the list lives, and why it moved

`<data root>/config/data-paths.yaml`, the `services:` block, since 2026-09-19.
It was `<engine>/config/runtime-services.yaml` until that day, and it moved on
the operator's decision (HOS-5 § 7.2 item 3, which had left the choice to him and
named the cost of the other answer: two registries, and § 7.1's chain of
constants wrong as written). After the fold the engine holds exactly ONE
hard-coded data address, `REGISTRY_REL` in `scripts/utils/data_path.py`, and this
module asks that module where the registry is rather than spelling a second one.

The file is PARSED there -- one file, one parser, one cache, one version check --
and the service rows are VALIDATED here, because what a service name may look
like is this module's rule and a second copy of it is the defect the fold exists
to avoid.

The risk the plan named is that a public clone has no service list at all. It is
paid safely, and the payment is the seam below rather than an argument: the root
is `require_writable_data_root()`, the same seam `crm_autolog` uses, and
`services_path()` resolves against the SAME root. So on a clone with no data
repository, where the data root is only the empty `examples` sentinel, the call
raises `DataRootError` before any list is consulted: it refuses rather than
answering from an empty list.

`scripts/runtime-services.example.yaml` is the template. It is what a fresh data
root is seeded from (`scripts/init-data.py`) and what `load_services` answers
from on a WRITABLE root that carries no `services:` block yet. It is never the
live registry: a tree that has the block always wins. `load_services` states
the three cases and why each is what it is.

Not `private_cache_dir()`'s engine-tree fallback, because `<engine>/.cache` is
not ignored by the engine's own .gitignore (MEASURED 2026-09-14: `git
check-ignore .cache/x` prints nothing) and the engine is public.

The store's `cache/` and `tmp/` kinds are gitignored and its `state/` and
`logs/` kinds are tracked (durable operational state is not disposable; the
lines the engine ships are in `scripts/create-data-repo.py`, the live overlay's
file is HELM's to edit); the whole store is held out of the memory index (`deny_prefixes` in config/memory-index.yaml), and the HO2 family rules (a
`skip` row in scripts/heading_os_2/families.py), and routed `private` by an
explicit row in config/routing-map.yaml. Each of those is a stated rule; before
this module the same exclusion held only because no indexer's allowlist happened
to reach the six spellings.

`import scripts.utils.data_path` is deliberately NOT at the top of this file:
that module imports this one, so the only import that does not deadlock at load
is the one inside `services_path()`, taken at call time when both modules are
already built.

Guard: `scripts/utils/runtime_write_guard.py` walks the engine's Python with the
AST and refuses a runtime-spelled write built from a data-root helper that does
not go through this module. That guard still runs. Tests: none live. This
module's own were taken by the 2026-09-21 test-suite split.
"""
from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from scripts.utils.paths import require_outside_engine_clone, require_writable_data_root
from scripts.utils.workspace import get_workspace_root

# The store's directory name is NOT a constant here any more. It was
# `STORE_DIRNAME = "runtime-junk"` on the operator's ruling of 2026-09-14, and on
# 2026-09-25 he reversed it ("yes, move it", translated, HOS-5 F1): the name is the
# registry's `runtime_store:` key, read by `store_dirname()` below, and the
# engine's default registry answers `runtime-junk` for a tree with no entry.


#: The four kinds, and nothing else. A fifth is a design change, not a call.
KINDS: tuple[str, ...] = ("logs", "cache", "state", "tmp")

#: The template a fresh data root is seeded from, under the ENGINE root. It is
#: not the registry and nothing reads it at runtime: `scripts/create-data-repo.py`
#: copies its rows into a new data root, and `scripts/utils/synthetic_data_root.py`
#: seeds a throwaway one from the same bytes. An engine path, not a data address,
#: which is why it may be a literal here at all.
EXAMPLE_SERVICES_REL = ("scripts", "runtime-services.example.yaml")

README_NAME = "README.md"

README_TEXT = """\
# The runtime store

Everything the machine writes and overwrites while running: logs, sync dumps,
caches, locks, temp files, queues a daemon drains, indexes rebuilt from disk.
One folder per service, daemon, hook or script, spelled as its installer spells
it, each with up to four kinds: `logs/`, `cache/`, `state/`, `tmp/`.

Nothing here is a record. A file is a record when a person reads it as one or
when losing it loses something that cannot be re-run; those live at the
addresses `config/data-paths.yaml` names and never here. Machine state that must
survive a fresh clone (reminders, acknowledgements, the crunch flag) is a record
too, under its own registry name, not here.

Git ignores `cache/` and `tmp/` and tracks `state/` and `logs/`, except the
paths the registry group `git-ignored` holds back for their content. A queue, a
dead-letter entry, a disposition or approval log, a cursor, a quarantined
wreck, a migration backup, an audit trail: placing them here does not make
them rebuildable, and losing one loses an instruction or a trail, so they are
under `state/` or `logs/` and never under `cache/` or `tmp/` (HELM ruling
2026-09-14, after outside review). What sits under `cache/` and `tmp/` is by
definition rebuildable by running the machine again. The whole store is out of
the memory index: a calendar dump is not a memory.

This folder's own place is the `runtime_store:` key of that registry. Writers reach this tree through the engine's front door,
`data_write_dir("runtime:<service>/<kind>")` in `scripts/utils/data_path.py`;
the service list is the `services:` block of `config/data-paths.yaml` in this
data repository.
"""


class RuntimeDirError(ValueError):
    """A refusal at the call: the caller named something this store has no place for."""


class UnknownKind(RuntimeDirError):
    """`kind` is not one of `KINDS`."""


class UnknownService(RuntimeDirError):
    """`service` has no row in the registry's `services:` block."""


class ServicesConfigMalformed(RuntimeDirError):
    """The registry is missing, unparseable, or holds no usable `services:` block."""


def example_services_path() -> Path:
    """The engine-shipped TEMPLATE, for a message that has to say where to look.

    Resolved from `get_workspace_root()` so a worktree names its own copy, never
    a constant and never the common git directory.
    """
    return get_workspace_root().joinpath(*EXAMPLE_SERVICES_REL)


def services_path(data_root: Path | None = None) -> Path:
    """The registry file, under the DATA root, resolved by asking the front door.

    `scripts.utils.data_path` owns the one constant that says where the registry
    sits inside the data root, so this asks it rather than spelling a second
    copy. The import is local because that module imports this one at load; at
    call time both are built and there is no cycle.

    The default root is `require_writable_data_root()`, exactly as `runtime_root`
    below resolves it, and that is the whole safety story of the fold: the two
    halves of a call -- may this service write, and where does it write -- now
    resolve against the SAME root, so a tree with no writable overlay refuses
    both rather than answering one of them from an empty list. An explicit
    `data_root` bypasses that check the same way `runtime_root` does, for the
    mover and for tests over a scratch tree.

    Not the engine root it used to be: an engine-side registry answered for
    the wrong tree and every writer fell back silently (MEASURED 2026-09-14).
    """
    from scripts.utils.data_path import registry_path

    return registry_path(_registry_root(data_root))


def _registry_root(data_root: Path | None) -> Path:
    """The data root the registry is read from. One resolution, two callers.

    Spelled once so `services_path()` and `load_services()` can never answer
    about different trees, and so nothing has to walk back UP from the registry
    path to recover the root it came from: `REGISTRY_REL` may gain a directory
    tomorrow and a `.parent.parent` would then name the wrong tree, silently.
    """
    return Path(data_root) if data_root is not None else require_writable_data_root()


def _valid_service_name(name: str) -> bool:
    if not name or name[0] in "_.-" or name[-1] == "-":
        return False
    return all(ch.islower() or ch.isdigit() or ch == "-" for ch in name if ch.isascii()) \
        and name.isascii()


def _validate_services(raw: dict, path: Path) -> dict[str, str]:
    """The rows this store will accept, or the first refusal one of them earns.

    One implementation for the live registry and for the template, because they
    hold the same block and a rule enforced on one of them only is a rule that
    lets a bad row reach a fresh data root by way of a seed.
    """
    out: dict[str, str] = {}
    for name, description in raw.items():
        if not isinstance(name, str) or not _valid_service_name(name):
            raise ServicesConfigMalformed(
                f"{path}: service name {name!r} must be lowercase letters, digits and "
                "hyphens, and must not start with `_` or `.`")
        if not isinstance(description, str) or not description.strip():
            raise ServicesConfigMalformed(
                f"{path}: service {name!r} needs one line saying what it is")
        out[name] = description.strip()
    return out


def example_services_text() -> str:
    """The template's bytes, or a refusal naming where it should be."""
    path = example_services_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ServicesConfigMalformed(
            f"{path}: the engine's service template is unreadable ({exc}). It is "
            f"what a fresh data root is seeded from and what a static guard falls "
            f"back to when there is no data root at all.") from exc


def example_services_block() -> str:
    """Just the `services:` section of the template, for a caller appending it.

    Refuses rather than returning "" when the marker is absent: an empty block
    appended to a registry is a registry that names no service, which is the
    empty-answer shape this module refuses everywhere else.
    """
    text = example_services_text()
    marker = "\nservices:\n"
    at = text.find(marker)
    if at < 0 and not text.startswith("services:\n"):
        raise ServicesConfigMalformed(
            f"{example_services_path()}: no top-level `services:` block to copy")
    return text[at + 1:] if at >= 0 else text


def template_services() -> dict[str, str]:
    """The services the ENGINE ships, read from the template and not the registry.

    For a reader that has no data root and legitimately still has a question:
    `scripts/utils/runtime_write_guard.py` walks ENGINE source in CI and on a
    public clone, where `load_services()` cannot answer at all. This is not a
    fallback for a writer -- a writer with no data root must refuse, and does.
    """
    from scripts.utils.data_path import SERVICES_KEY
    from scripts.utils import yamlio

    path = example_services_path()
    data = yamlio.safe_load(example_services_text())
    raw = data.get(SERVICES_KEY) if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise ServicesConfigMalformed(
            f"{path}: holds no non-empty `{SERVICES_KEY}:` mapping. This is the "
            f"default `load_services` falls back to on a tree with no registry, "
            f"so an empty one here would be the empty service list that refuses "
            f"every honest writer. Refusing instead.")
    return _validate_services(raw, path)


def seed_services(data_root: Path) -> Path:
    """Copy the template into a data root that has no registry yet; return its path.

    Never overwrites. A data root that already carries a registry has residue
    names in it, and replacing that file with the template would delete them --
    so an existing registry is left exactly as it is and this returns its path
    unchanged. Adding the block to a registry that has one but no services is
    a hand edit, deliberately: it is a merge, and a merge that guesses is how a
    registry loses a line nobody noticed.
    """
    from scripts.utils.data_path import registry_path

    path = registry_path(Path(data_root))
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    # The template's own header says it is never the live registry, which the
    # seeded copy IS; it gets a header that says so (external review 2026-10-26).
    text = example_services_text()
    start = text.find("\nversion:")
    body = text[start + 1:] if start >= 0 else text
    path.write_text(SEEDED_REGISTRY_HEADER + body, encoding="utf-8")
    return path


SEEDED_REGISTRY_HEADER = """\
# The data-address registry of this data repository: the LIVE one.
# Seeded from the engine's scripts/runtime-services.example.yaml when this
# repository was created, and yours to edit from then on.
#
# `services:` lists every service that may write under the runtime store,
# `<runtime_store>/<service>/<kind>`, kinds logs, cache, state and tmp. A writer
# naming a service not listed here is refused. Adding a row registers a service;
# removing one refuses every writer still naming it.
#
# A `names:` block is optional: without one, the engine answers every data
# address from its own scripts/data-names.example.yaml.

"""


def load_services(data_root: Path | None = None) -> dict[str, str]:
    """The `services:` block as `{service: one-line description}`, or a refusal.

    The YAML is parsed and cached by `scripts.utils.data_path`, keyed on the
    file's TEXT -- one file, one parser. What is validated HERE is what this
    module owns: that every key is a service name this store will accept and
    every value says in one line what the service is.

    THREE ANSWERS, and the difference between them is the whole of this
    function's judgement.

    1. NO WRITABLE DATA ROOT is a refusal, raised by `_registry_root` before
       anything is read. That is the public-clone case and the one the fold's
       risk was about: the list is data, a clone with no data has no list, and a
       clone with no data also may not write. Both halves refuse together.

    2. A REGISTRY THAT EXISTS AND WILL NOT PARSE, or whose `services:` block
       holds a row this store cannot accept, is a refusal. A registry that
       cannot be trusted is not read optimistically.

    3. A WRITABLE ROOT WITH NO `services:` BLOCK falls back to the engine's
       shipped template, and that is deliberate rather than lenient. Absent is
       not broken: a fresh overlay, a scratch tree a test just made, and an
       overlay seeded before this block existed all legitimately have no
       registry, and the honest default for them is the vocabulary the engine
       ships, which is the same list they would be seeded with.

    The failure this must NOT become is `load_exec_registry` in
    `scripts/utils/workspace.py` (removed 2026-09-25), which read the wrong path until 2026-08-23 and
    whose `exists()` guard turned the miss into an EMPTY registry, so every
    caller silently saw a fleet of zero executives. Case 3 is not that shape and
    the distinction is exact: an empty answer would refuse every honest writer
    while looking well-formed, and this returns the rows the template names.
    Nothing here can ever answer with zero services -- `_validate_services` over
    an empty mapping would, so the two callers of it both guarantee non-empty
    first, and `template_services()` refuses rather than returning `{}`.

    A tree that HAS a `services:` block always wins over the template. That is
    the property the fold bought. It was asserted until the 2026-09-21
    test-suite split, and the precedence now rests on the branch below.
    """
    from scripts.utils.data_path import (RegistryMalformed, SERVICES_KEY,
                                         load_registry_document, registry_path)

    base = _registry_root(data_root)
    path = registry_path(base)
    try:
        document = load_registry_document(base)
    except RegistryMalformed as exc:
        raise ServicesConfigMalformed(str(exc)) from exc
    services = document.get(SERVICES_KEY) if document is not None else None
    # `not services`, not `services is None`: an empty mapping reaching here
    # would answer with zero services, and zero services refuses every honest
    # writer while looking well-formed. The parser already refuses a
    # present-but-empty block, so this is the second lock on the same door and
    # it is the one that does not depend on the parser staying that way.
    if not services:
        return template_services()
    return _validate_services(services, path)


def known_services(data_root: Path | None = None) -> tuple[str, ...]:
    return tuple(load_services(data_root))


#: A test session's redirect of ONE data root's store, spelled
#: `<absolute data root>::<absolute store>`. Set by `tests/conftest.py` and by
#: nothing else. HOS-5 section 7, 2026-09-25: the logs and hook state that used to
#: land in the engine checkout, where the suite redirected them into
#: `.logs/_pytest/`, now land in this store, so a test driving a guard would
#: append its fixtures to the operator's real denial log. Keyed by the data root
#: rather than global on purpose: a child a test starts over its OWN scratch data
#: root inherits the variable, and must still write under that scratch root.
ENV_REDIRECT = "HEADING_OS_RUNTIME_REDIRECT"


def _redirected(base: Path) -> Path | None:
    """The store the redirect names for `base`, or None when it names another root.

    A malformed or relative value is refused loudly rather than ignored: a test
    session that believes it is isolated and is not would write its fixtures into
    the operator's store, which is the defect the variable exists to prevent.
    """
    raw = os.environ.get(ENV_REDIRECT)
    if not raw:
        return None
    root, sep, store = raw.partition("::")
    if not sep or not Path(root).is_absolute() or not Path(store).is_absolute():
        raise RuntimeDirError(
            f"{ENV_REDIRECT}={raw!r} is not `<absolute data root>::<absolute store>`")
    if Path(root).resolve() != Path(base).resolve():
        return None
    return Path(store)


def test_session_store(engine_root: Path, no_data_repo: Path) -> tuple[Path, str]:
    """(a test session's own runtime directory, the `ENV_REDIRECT` value for it).

    For `tests/conftest.py`, which sets the variable before any test runs. The
    directory is THIS checkout's scratch in the data repository, so it is the
    same for a pytest and every child pytest it spawns and they share one rate
    counter. On a tree with no data repository there is no store to redirect
    (the value is empty) and the session's hook state goes to a directory named
    for the checkout under `no_data_repo`, which the TEST HARNESS supplies: it
    is pytest's own temp handling, on a public clone or CI the only place left
    that is not the checkout, and engine code does not choose the system
    temporary directory (`scripts/dev/outside-workspace-writes.py`).
    """
    from scripts.utils.data_path import DataPathError, checkout_scratch_dir
    from scripts.utils.paths import DataRootError

    try:
        session = checkout_scratch_dir(engine_root) / "pytest"
        data_root = require_writable_data_root().resolve()
    except (DataPathError, DataRootError) as exc:
        print(f"[runtime-dir] no checkout scratch in a data repository ({exc}); "
              f"the test session's state goes under {no_data_repo}")
        return Path(no_data_repo) / f"heading-os-pytest-{engine_root.name}", ""
    return session, f"{data_root}::{session / store_dirname(data_root)}"


def store_dirname(data_root: Path | None = None) -> str:
    """The store's directory name under the data root, from the registry's `runtime_store:`.

    Resolved against the same root `runtime_root` uses, so a writer's store and
    the name it is spelled by always come from one tree. A registry that cannot
    be trusted is re-dressed as this module's `ServicesConfigMalformed`, the
    class `load_services` raises for the same file.
    """
    from scripts.utils.data_path import RegistryMalformed, store_dirname as _answer

    try:
        return _answer(_registry_root(data_root))
    except RegistryMalformed as exc:
        raise ServicesConfigMalformed(str(exc)) from exc


def default_store_dirname() -> str:
    """The name the ENGINE's default registry gives the store: `runtime-junk`, unless the default changes."""
    from scripts.utils.data_path import RegistryMalformed, template_store_dirname

    try:
        return template_store_dirname()
    except RegistryMalformed as exc:
        raise ServicesConfigMalformed(str(exc)) from exc


def store_ignore_problems(data_root: Path, store: str) -> list[str]:
    """Why the data root's `.gitignore` does not yet cover a store named `store`; empty when it does.

    The relocation hazard HOS-5 F1 named: the overlay's `.gitignore` spells the
    store's directory in every line that keeps a cache, a lock or a browser
    profile out of git, so a registry edit alone would move the store out from
    under all of them and the next commit would carry what they held. The
    required lines and the reading of the file are
    `registry_bootstrap.ignore_problems_in`, the one copy the overlay guard also
    applies before it records into a relocated store.

    What it cannot see: a store moved a second time, from one non-default name
    to another, leaves rules under the first name that nothing here knows to
    look for; and a specific rule dropped on the way (one queue file, one
    service's logs) is found only when it still names the default. The lines to
    carry are every line under the old name, rewritten; that is HELM's edit.
    """
    from scripts.utils.registry_bootstrap import ignore_problems_in

    path = Path(data_root) / ".gitignore"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path} cannot be read ({exc}), so nothing ignores the store's "
                f"caches, temp files and locks"]
    return ignore_problems_in(text, str(path), store, default_store_dirname())


def runtime_root(data_root: Path | None = None) -> Path:
    """`<data root>/<store>`, NOT created; `<store>` is the registry's `runtime_store:`.

    `data_root` is for the mover and for tests over a scratch tree; a writer
    passes nothing and gets the writable overlay, or `DataRootError`. Under a
    test session `ENV_REDIRECT` may name another directory for one root.

    A store the registry has moved away from the default name is REFUSED here,
    for every writer and reader at once, until the data root's `.gitignore`
    covers it (`store_ignore_problems`). Loud on purpose: the alternative is a
    commit that quietly carries every cache and lock the old rules held back.
    """
    base = Path(data_root) if data_root is not None else require_writable_data_root()
    redirected = _redirected(base)
    if redirected is not None:
        store = redirected
    else:
        name = store_dirname(base)
        if name != default_store_dirname():  # only a relocated store pays for the read
            problems = store_ignore_problems(base, name)
            if problems:
                raise RuntimeDirError(
                    f"the registry moves the runtime store to `{name}/`, and its "
                    f"ignore rules did not follow: " + "; ".join(problems)
                    + ". Carry every rule over to the new name, then retry.")
        store = base / name
    # Never inside the engine checkout, whoever named the root. An EXPLICIT
    # `data_root` skips `require_writable_data_root`, and on a public clone the
    # data root a caller holds is the `examples` sentinel inside the clone, so
    # without this a cache handed that root would create the store in the
    # public repository. HOS-5 section 7, 2026-09-25.
    require_outside_engine_clone(store, "The runtime store")
    return store


def ensure_store_root(data_root: Path | None = None) -> Path:
    """Create the store root once, private to the owner, with its README; return it.

    0700 because the junk includes browser cookies, whole drafted email bodies
    in the action queue and calendar dumps. Only applied at creation; an
    operator who loosened it on purpose is not fought.

    Called by `data_write_dir`, the front door's write operation, before it
    makes a runtime directory, and by nothing that resolves an address: this
    creates the one directory whose mode matters and answers no question about
    where anything goes.
    """
    root = runtime_root(data_root)
    if root.is_dir():
        return root
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # a filesystem without POSIX modes; the directory still exists
        os.chmod(root, stat.S_IRWXU)
    readme = root / README_NAME
    if not readme.exists():
        readme.write_text(README_TEXT, encoding="utf-8")
    return root


__all__ = [
    "EXAMPLE_SERVICES_REL", "KINDS",
    "README_NAME", "README_TEXT", "RuntimeDirError",
    "ServicesConfigMalformed", "UnknownKind", "UnknownService",
    "default_store_dirname", "example_services_block", "ensure_store_root",
    "example_services_path", "example_services_text", "known_services",
    "load_services", "runtime_root", "seed_services",
    "services_path", "store_dirname", "store_ignore_problems", "template_services",
]
