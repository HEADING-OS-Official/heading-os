"""The one front door to a data address. It dispatches; it resolves nothing itself.

    from scripts.utils.data_path import data_path, data_read, data_read_required

    data_path("operations-reports")       # a name the registry places
    data_path("runtime:browser/cache")    # <data root>/<runtime store>/browser/cache
    data_path("crm-config")               # a name the registry places
    data_path("crm-contact", person="x")  # a name with a parameter

HOS-5 asks for ONE place the engine gets a data address from. This module is
that place, and the whole of its design is in what it does NOT contain: there is
no path arithmetic here, no table of directories, no fallback to a legacy
address, no second answer to a question something else already answers.

## Why a facade and not a resolver

This workspace already had a resolver when HOS-5 was written. It had two.

  * `scripts/heading_os_2/resolve.py` answers for RECORDS, over the manifest
    tables: `write_dir(family, leaf)` says where a new record of a kind goes,
    `read_scope()` and `read_files()` say where the existing ones are. Its
    vocabulary is the nine families and their leaves.
  * `scripts/utils/runtime_dir.py` owned RUNTIME STATE, over the
    `services:` block of this same registry: `get_runtime_dir(service, kind)`
    gave `<data root>/<runtime store>/<service>/<kind>`, refusing a service with
    no row and a kind outside the four. That helper was deleted under HOS-5
    § 6 on 2026-09-25; its two refusals moved into `_runtime_dir` below, and
    the module keeps the vocabulary and the store root.

Writing a third resolver beside those two is this repository's named dominant
defect: one decision in N copies. It has been measured here rather than
asserted (MEASURED 2026-09-18, recorded in
`scripts/utils/runtime_write_guard.py`): moving four writers onto a new seam
dropped the guard's visibility to zero, and its test went red on the files
that had just been repaired. A third vocabulary would do the same to every guard keyed on the
first two.

So each of the three name forms below hands the question to whoever already owns
it, and the answer comes back unaltered:

    record:<family>/<leaf>     -> resolve.write_dir / read_scope
    runtime:<service>/<kind>   -> _runtime_dir, over runtime_dir's vocabulary
    <a registry name>          -> `config/data-paths.yaml` under the data root

The first two forms are DERIVED, never copied: every family/leaf pair the
manifest places and every service/kind the `services:` block names is answerable
the day it is added, with nothing edited here and no line written under `names:`.
That was held entry by entry against both vocabularies, over the real overlay
and over a synthetic one, until the 2026-09-21 test-suite split. Nothing
re-derives it now, so a family the manifest gains is only answerable here if
the derivation below still reaches it.

THE RECORD FORM IS RETIRED FOR CALLERS (HOS-240, 2026-10-13). C3 split most
kinds across several subject roots, so a kind no longer has one home: no engine
site asks for `record:<family>/<leaf>` any more, and no entry of the engine's
default registry carries a `record:` base. The dispatch stays, so a tree whose
own registry still carries a `record:` entry keeps answering until that entry
becomes a `path:`; a new caller asks a registry name.

## What the registry file is for, and what it is not

`<data root>/config/data-paths.yaml` carries two blocks, and they answer
different questions.

`names:` holds the RESIDUE: the addresses neither vocabulary names, because
neither is built to. A vocabulary of KINDS cannot address one named file
(the `crm-config` file), and it cannot address a family of files parameterised by a
person or a date. Those are what `names:` carries, and nothing else. Copying the
manifest pairs or the services under `names:` would recreate the defect this
module exists to avoid, one file lower down.

A tree whose registry carries no `names:` block at all answers from the
ENGINE'S OWN default set, `scripts/data-names.example.yaml`, exactly as
`runtime_dir.load_services` answers from the shipped service template. What that
file may hold, and the two halves a bare tree needs, are in `template_names()`
and in `_record_write_dir` below.

`services:` is the runtime VOCABULARY itself, folded in from the engine's
`config/runtime-services.yaml` on 2026-09-19 (operator decision that day; HOS-5
§ 7.2 item 3). It is parsed here because one file has one parser, and it is
VALIDATED in `scripts/utils/runtime_dir.py`, which owns what a service name may
look like. That split is the whole of the coupling: this module never decides
whether a service exists, and that module never reads the file.

Two more top-level keys since HOS-234 (2026-10-13), so that the registry is the
only address book. `subject_roots:` lists the subject tree's top-level
directories, read by every guard through `families.subject_roots()`; `groups:`
names sets of names that a reader walks together (`load_groups`,
`group_names`), and feeds `scripts/generate-data-files.py`, which writes the
files that cannot read a name. Both fall back to the engine's default file on a
tree that declares none, exactly as `names:` does.

The registry's own address is the single constant `REGISTRY_REL`, and it is the
last hard-coded data path in the engine by design (HOS-5 § 7.1): the environment
says where the data root is, this constant says where the registry is inside it,
and the registry says where everything else is. `scripts/heading_os_2/families.py`
carries a `skip` row for that address so the classifier cannot move the file the
constant points at. A test held that `skip` row in place until the 2026-09-21
test-suite split; the row is still there, and nothing now notices if it goes.

## Four refusals, and two operations that create

An unknown name, a missing or extra parameter, a registry that will not parse,
and an address that resolves outside the data repository are each refused by
name, at the call, before anything touches the disk.

`data_path`, `data_read` and `data_read_required` create nothing at the
addresses they answer: enumerating the vocabulary must not materialise a
directory per service in the operator's live overlay just for asking a
question. The one thing a lookup may write is the registry's own parse cache
under `<store>/data-path/cache/` (HOS-173), the `cache` kind every store's
`.gitignore` covers; see `_cache_write`. `data_write_dir` and
`data_write_file` are the two operations that create, the directory a name
places and the directory holding the file a name places, and they are separate
so that creating is something a caller says rather than something an answer
does (HOS-5 § 4). For the runtime store each makes the store root at 0700
first, which is why they exist as operations rather than as a `mkdir` each
writer spells for itself.

## Reading: the mandatory source is a SEPARATE operation

`data_read()` answers where to look and is content with finding nothing, which
is right for a source that legitimately may not exist yet.
`data_read_required()` is the paired operation for a source whose absence is a
defect: it refuses, naming every address it looked at, rather than returning an
empty answer that a caller reads as "there is nothing there".

The distinction is the caller's to make, at the call, and it is not a guess
inside this module. The precedent was in this tree: `load_exec_registry` in
`scripts/utils/workspace.py` (removed 2026-09-25 with the executive model,
HOS-130) read the wrong path until 2026-08-23, and its
`exists()` guard turned that miss into an empty registry rather than an error, so
every caller silently saw a fleet of zero executives. An absent OPTIONAL source
returning empty is correct; an absent MANDATORY source returning empty is that
defect, and only the caller knows which of the two it asked for.
"""
from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path, PurePosixPath

try:  # the builtin, not `hashlib`: MEASURED 2026-09-28, 0.05 ms to import against 3.0 ms
    from _sha256 import sha256 as _sha256
except ImportError:  # pragma: no cover - an interpreter that folded it into `_sha2`
    from hashlib import sha256 as _sha256

from scripts.heading_os_2 import compat as _compat
from scripts.heading_os_2 import resolve as _resolve
from scripts.utils import runtime_dir as _runtime
from scripts.utils.paths import DataRootError, get_data_root

#: THE constant, `REGISTRY_REL`: relative to the data root, and the only data
#: address the engine holds as a literal after HOS-5; everything else is read out
#: of the file it names. See the module docstring and HOS-5 § 7.1. Spelled in
#: `scripts/utils/registry_bootstrap.py` and re-exported here, because the
#: overlay write guard needs it before `scripts` may be imported (HOS-5 F1,
#: 2026-09-25); one spelling, two importers. `EXAMPLE_NAMES_REL` is the engine's
#: DEFAULT registry file, an engine path and not a data address, and travels with
#: it for the same reader.
from scripts.utils.registry_bootstrap import (EXAMPLE_NAMES_REL, REGISTRY_CACHE_REL,
                                              REGISTRY_REL, STORE_KEY, SUBJECT_ROOTS_KEY,
                                              RootsKeyError, StoreKeyError,
                                              check_root_names, check_store_name,
                                              read_roots_line, read_store_line,
                                              store_dirname_from_files,
                                              subject_roots_from_files)

#: The schema version the parser understands. A file declaring anything else is
#: refused rather than read optimistically: a registry is not a place to guess.
SCHEMA_VERSION = 1

#: The two derived namespaces. A name carrying one of these prefixes is answered
#: by the vocabulary it names, and never by the registry file.
RECORD_SCHEME = "record:"  # leak-guard: ok (the scheme itself, not an address spelled with it)
RUNTIME_SCHEME = "runtime:"  # leak-guard: ok (the scheme itself, not an address spelled with it)

#: One scheme-qualified address as it appears INSIDE other text: a Python
#: literal, a line of a SKILL.md, a sentence in a rule.
#:
#: It lives here, beside the two constants it is built from, because three
#: guards need it and a regex over the schemes is the schemes' own declaration
#: in machine-readable form. Writing it out in each of them is this
#: repository's named dominant defect, one decision in N copies, and the copy
#: that stops being fixed would be whichever guard nobody touched that week.
ADDRESS_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in (RECORD_SCHEME, RUNTIME_SCHEME))
    + r")([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def addresses_in(text: str) -> tuple[list[str], str]:
    """Every scheme-qualified address in `text`, and `text` with them BLANKED.

    Blanked to spaces rather than removed, so every offset, column and length a
    caller computes afterwards is the one the text really has.

    The blanking is the point, not the finding. A guard that looks for data
    paths by their first segment reads a `record:` address, a family and a leaf
    after the scheme, as the bare family/leaf path, because `:` is a word
    boundary and sits outside the lookbehind every such pattern uses. MEASURED
    2026-09-19 against two of them on this tree: `scripts/audit-skill-bash-paths.py`
    flagged a `data-path.py path record:...` call naming the `("trace", "report")`
    kind as a bare data-path misroute, and `scripts/check-data-path-references.py`
    extracted the bare pair out of the same string and resolved it against the
    disk. (Spelled out rather than quoted since HOS-240, 2026-10-13: that kind's
    home is empty now, and a quoted example would count as a reference to it.)
    Finding the address without blanking it leaves the false reading beside the
    true one; blanking is what removes it.

    A bare registry name (`crm-config`) carries no scheme and no slash and is
    deliberately not matched. It is placed by a file under the data root, so
    there is nothing on a public clone to check it against, and a guard that
    refused it there would refuse every public clone.
    """
    found: list[str] = []
    pieces: list[str] = []
    last = 0
    for match in ADDRESS_RE.finditer(text):
        found.append(match.group(0))
        pieces.append(text[last:match.start()])
        pieces.append(" " * (match.end() - match.start()))
        last = match.end()
    pieces.append(text[last:])
    return found, "".join(pieces)


#: A registry name: lowercase, kebab, no scheme. Deliberately narrower than the
#: file could hold, so a name cannot arrive looking like one of the two schemes
#: above or like a path.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: A parameter VALUE. One path segment, and not a relative-path trick. The
#: containment check below is the second line of defence, not the first.
PARAM_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: `{person}` in a template. The placeholders ARE the parameter list; there is no
#: second declaration of them to drift from the string.
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

#: The keys an entry may carry. Exactly one of the three BASES, plus `what`, plus
#: an optional `under`. (`root` was a third optional key until 2026-09-25; see
#: `RETIRED_ENTRY_KEYS`.)
BASES = ("path", "record", "runtime")
ENTRY_KEYS = frozenset(BASES) | {"what", "under"}

#: The `root:` key an entry could carry until HOS-132 retired it (2026-09-25).
#: Every entry is measured from the data root.
#:
#: A registry line that still carries `root:` is REFUSED, never ignored. A
#: key that is parsed and then dropped would read to its author as honoured.
RETIRED_ENTRY_KEYS = frozenset({"root"})

#: The registry's two blocks. `names:` is the residue this module places;
#: `services:` is the runtime vocabulary `scripts/utils/runtime_dir.py` validates
#: and spends. Either may be absent; a file with NEITHER is a registry that says
#: nothing, and is refused.
NAMES_KEY = "names"
SERVICES_KEY = "services"

#: Named SETS of `names:` entries, for a reader that walks several directories
#: together (every output directory, every DataStore home). HOS-234,
#: 2026-10-13: these lists were engine constants in `scripts/utils/data_homes.py`,
#: so a directory joining or leaving a set was an engine edit. Each group is
#: `{what: <one line>, names: [<registry name>, ...]}`, and a group naming a name
#: the registry does not place is refused at load, like any other malformed line.
GROUPS_KEY = "groups"
GROUP_KEYS = frozenset({"what", "names"})


class DataPathError(RuntimeError):
    """Base for every refusal here.

    A `RuntimeError` following `DataRootError` and `ManifestError`: callers wrap
    filesystem work in `except OSError`, and an `OSError` subclass would be
    swallowed by a handler written for a missing file, restoring the silence
    this front door exists to remove.
    """


class UnknownDataPath(DataPathError):
    """No name of that spelling, in either vocabulary or in the registry."""


class ParameterMismatch(DataPathError):
    """A parameter is missing, unexpected, or not a single safe path segment."""


class RegistryMalformed(DataPathError):
    """The registry exists and cannot be trusted to answer."""


class AddressEscapesDataRoot(DataPathError):
    """A resolved address would land outside the data repository."""


class MissingRequiredSource(DataPathError):
    """A source the caller declared MANDATORY is not on disk."""


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def registry_path(data_root: Path | None = None) -> Path:
    """Where the registry is, from the one constant. It is not created here."""
    base = Path(data_root) if data_root is not None else get_data_root()
    return base / REGISTRY_REL


def _reject(path: Path, problem: str) -> RegistryMalformed:
    return RegistryMalformed(f"{path}: {problem}")


def _check_relative(value: str, where: str, path: Path) -> str:
    """A registry address is relative, POSIX-spelled and inside the tree.

    The textual half of the containment check. `_under()` does the other half
    after resolution, because a symlinked component can carry a textually clean
    path out of the tree.
    """
    if not isinstance(value, str) or not value.strip():
        raise _reject(path, f"{where} is empty")
    value = value.strip()
    if "\\" in value:
        raise _reject(path, f"{where}={value!r} uses a backslash; addresses are POSIX-spelled")
    if value.startswith("/") or (len(value) > 1 and value[1] == ":"):
        raise _reject(path, f"{where}={value!r} is absolute; addresses are relative to the data root")
    if ".." in PurePosixPath(value).parts:
        raise _reject(path, f"{where}={value!r} climbs out of the data root")
    return value.strip("/")


def _check_pair(value: str, where: str, path: Path) -> tuple[str, str]:
    """`a/b`, the shape both derived vocabularies use. Two segments, no more."""
    cleaned = _check_relative(value, where, path)
    parts = cleaned.split("/")
    if len(parts) != 2 or not all(parts):
        raise _reject(path, f"{where}={value!r} is not a `<first>/<second>` pair")
    return parts[0], parts[1]


def _parse_document(text: str, path: Path) -> dict[str, dict]:
    """The whole registry, validated at LOAD time rather than at the call that needs it.

    A malformed entry nobody has asked for yet is still a malformed registry,
    and finding out one name at a time means finding out on the day that name is
    first used, which is the day nobody is looking at this file.

    Returns `{"names": {...}, "services": {...}}` beside the store name, the
    subject roots and the groups, each None when undeclared. Each block may be absent, and
    an absent block is an empty mapping here; what an EMPTY block means is the
    consumer's to decide, because the two differ. An absent `names:` is a tree
    with no residue addresses, which is ordinary. An absent `services:` is a tree
    on which no runtime writer may write at all, which `runtime_dir` refuses.
    A file with neither block is refused here, for everyone.

    `services:` rows are checked for SHAPE only. What a service name may look
    like is `scripts/utils/runtime_dir._valid_service_name`, and that rule stays
    in the module that spends it: two copies of one rule in two files is the
    defect this front door exists to avoid.
    """
    import yaml  # local, matching `load_routing_map`: the parser's own error type

    from scripts.utils import yamlio  # local: a disk-cache hit never imports PyYAML

    try:
        data = yamlio.safe_load(text)
    except (yaml.YAMLError, UnicodeDecodeError, ValueError) as exc:
        raise _reject(path, f"is not parseable YAML ({type(exc).__name__}: {exc})") from exc
    if not isinstance(data, dict):
        raise _reject(path, "does not hold a mapping at the top level")
    # Checked by VALUE rather than by trusting the parser: on a libyaml build
    # `yamlio.safe_load` accepts two tab cases that the pure-Python loader
    # rejects, so a fail-closed handler keyed on the exception alone is not
    # enough (scripts/utils/yamlio.py lines 33-40).
    version = data.get("version")
    if version != SCHEMA_VERSION:
        raise _reject(path, f"declares version {version!r}; this engine reads version {SCHEMA_VERSION}")
    names = data.get(NAMES_KEY)
    services = data.get(SERVICES_KEY)
    roots = _parse_roots(data, text, path)
    store = _parse_store(data, text, path, roots)
    if names is None and services is None:
        raise _reject(path, f"holds neither a `{NAMES_KEY}:` nor a `{SERVICES_KEY}:` "
                            f"mapping; a registry that names nothing is not a registry")
    for key, block in ((NAMES_KEY, names), (SERVICES_KEY, services)):
        if block is not None and (not isinstance(block, dict) or not block):
            raise _reject(path, f"`{key}:` is present and is not a non-empty mapping")

    # An absent block stays `None` rather than collapsing to `{}`. The two mean
    # different things to `runtime_dir.load_services`, which reads absent as "no
    # block yet, use the shipped default" and empty as the one answer it may
    # never give. A present-but-empty block is already refused above, so `None`
    # here is the only way absence can arrive.
    parsed_names = _parse_names(names or {}, path)
    return {
        NAMES_KEY: parsed_names,
        SERVICES_KEY: dict(services) if services is not None else None,
        STORE_KEY: store,
        SUBJECT_ROOTS_KEY: roots,
        GROUPS_KEY: _parse_groups(data.get(GROUPS_KEY), parsed_names, services, path),
    }


def _parse_roots(data: dict, text: str, path: Path) -> tuple[str, ...] | None:
    """The subject-tree roots this registry lists, or None when it lists none.

    Read twice, as the store key is, and refused unless both readings agree:
    from the parsed YAML, and by `registry_bootstrap.read_roots_line` from the
    raw text, which is what `families.subject_roots()` reads on every hook call
    without importing a YAML parser.
    """
    from scripts.utils.workspace import get_workspace_root

    try:
        line = read_roots_line(text, str(path))
        declared = data.get(SUBJECT_ROOTS_KEY)
        if declared is not None:
            declared = check_root_names(declared, str(path), get_workspace_root())
    except RootsKeyError as exc:
        raise _reject(path, str(exc).split(": ", 1)[-1]) from exc
    if line != declared:
        raise _reject(path, f"`{SUBJECT_ROOTS_KEY}:` reads {declared!r} as YAML and "
                            f"{line!r} as a line; write it as one flow list, "
                            f"`{SUBJECT_ROOTS_KEY}: [a, b]`, in column 0, once")
    return declared


def _scheme_member_problem(member: str, services) -> str | None:
    """Why a scheme-qualified group member names nothing, or None when it is answerable.

    Checked against the document being parsed, never through `validate_address`:
    that asks the registry under the data root for its services, and the
    registry under the data root may be the very file being parsed here.
    """
    scheme = RECORD_SCHEME if member.startswith(RECORD_SCHEME) else RUNTIME_SCHEME
    first, sep, second = member[len(scheme):].partition("/")
    if not sep or not first or not second or "/" in second:
        return f"is not spelled `{scheme}<first>/<second>`"
    if scheme == RECORD_SCHEME:
        from scripts.heading_os_2.families import FAMILIES
        if second not in FAMILIES.get(first, ()):
            return f"names no HEADING OS 2 kind {first}/{second}"
        return None
    known = services if services is not None else _runtime.template_services()
    if first not in known:
        return f"names no runtime service {first!r}"
    if second not in _runtime.KINDS:
        return f"names no runtime kind {second!r}; the kinds are {', '.join(_runtime.KINDS)}"
    return None


def _parse_groups(groups, names: dict[str, dict], services,
                  path: Path) -> dict[str, tuple[str, ...]] | None:
    """Every `groups:` entry as `{group: (name, ...)}`, or None when the block is absent.

    Each member must be a name this tree can answer: one of this registry's own
    `names:` when it has a block, else one of the engine default's, which is
    what `load_registry` answers from on such a tree; or a `record:` or
    `runtime:` name its vocabulary places. A group that names an unknown
    address is refused here rather than said on stderr at every walk, because
    the walk would otherwise lose that directory silently forever.
    """
    if groups is None:
        return None
    if not isinstance(groups, dict) or not groups:
        raise _reject(path, f"`{GROUPS_KEY}:` is present and is not a non-empty mapping")
    known = names if names else template_names()
    out: dict[str, tuple[str, ...]] = {}
    for group, entry in groups.items():
        if not isinstance(group, str) or not NAME_RE.match(group):
            raise _reject(path, f"group {group!r} must be lowercase letters, digits and hyphens")
        if not isinstance(entry, dict):
            raise _reject(path, f"group {group}: the entry must be a mapping, not "
                                f"{type(entry).__name__}")
        unknown = sorted(set(entry) - GROUP_KEYS)
        if unknown:
            raise _reject(path, f"group {group}: unknown key(s) {unknown}; allowed: "
                                f"{sorted(GROUP_KEYS)}")
        what = entry.get("what")
        if not isinstance(what, str) or not what.strip():
            raise _reject(path, f"group {group}: needs a `what:` line saying what the set is")
        members = entry.get("names")
        if not isinstance(members, list) or not members:
            raise _reject(path, f"group {group}: `names:` must be a non-empty list of "
                                f"registry names")
        for member in members:
            if isinstance(member, str) and member.startswith((RECORD_SCHEME, RUNTIME_SCHEME)):
                problem = _scheme_member_problem(member, services)
                if problem:
                    raise _reject(path, f"group {group}: {member!r} {problem}")
            elif not isinstance(member, str) or member not in known:
                raise _reject(path, f"group {group}: {member!r} is not a name this "
                                    f"registry places; add it under `{NAMES_KEY}:` first")
        if len(set(members)) != len(members):
            raise _reject(path, f"group {group}: a name is listed twice")
        out[group] = tuple(members)
    return out


def _parse_store(data: dict, text: str, path: Path,
                 roots: tuple[str, ...] | None) -> str | None:
    """The runtime store's directory name this registry declares, or None when it declares none.

    Read TWICE, and the file is refused unless both readings agree: once from
    the parsed YAML, once by `registry_bootstrap.read_store_line` from the raw
    text, which is what the overlay write guard reads because it cannot import
    a YAML parser. A registry the two would answer differently about (a quoted
    value, flow style, the key twice) would put the guard's record in one
    directory and every writer's files in another, silently. So it never loads.
    """
    try:
        # The line first: it names a value YAML would retype (`yes`, `0123`)
        # for what it is, where the parsed value would arrive as `True` or `83`.
        line = read_store_line(text, str(path))
        declared = data.get(STORE_KEY)
        if declared is not None:
            check_store_name(declared, str(path))
    except StoreKeyError as exc:
        raise _reject(path, str(exc).split(": ", 1)[-1]) from exc
    if line != declared:
        raise _reject(path, f"`{STORE_KEY}:` reads {declared!r} as YAML and {line!r} "
                            f"as a line; write it plain, `{STORE_KEY}: <name>`, in "
                            f"column 0, once")
    if declared is not None:
        clash = _store_collision(declared, data.get(NAMES_KEY), roots)
        if clash is not None:
            raise _reject(path, f"`{STORE_KEY}: {declared}` {clash}; every walker "
                                f"skips the store, so records inside it would vanish "
                                f"from the index and the move, and a walker of the "
                                f"records around it would read the store")
    return declared


def _store_collision(store: str, names, roots: tuple[str, ...] | None) -> str | None:
    """Why `store` may not be the runtime store's directory, or None when it may.

    A one-segment store may not be a directory the data root already uses for
    records: a family or subject root, or the first directory of any address a
    `names:` entry spells. A NESTED store (HOS-241, two segments) may sit
    inside a SUBJECT root, which is where the C3 move puts it, and nowhere
    else: not inside a family root, not equal to or inside any `names:`
    address, and no `names:` address inside it. An address is compared up to
    its first `{placeholder}` segment.
    """
    taken = _top_level_names_in_use(names, roots)
    head = store.split("/", 1)[0]
    if "/" not in store:
        return (f"is a directory the data root already uses for records ({taken[store]})"
                if store in taken else None)
    if roots is None:
        from scripts.utils.workspace import get_workspace_root
        roots = subject_roots_from_files(None, get_workspace_root())
    if head not in roots:
        why = taken.get(head, "not a C3 subject-tree root")
        return (f"nests the store under `{head}/` ({why}); a nested store sits "
                f"inside a subject root only")
    for name, address in _names_addresses(names):
        if address == store or address.startswith(f"{store}/"):
            return f"holds the records of `{name}` ({address})"
        if store.startswith(f"{address}/"):
            return f"sits inside the records of `{name}` ({address})"
    return None


def _names_addresses(names) -> list[tuple[str, str]]:
    """`(name, root-relative address)` for every `path:` base and `record:` `under:`
    a `names:` block spells, cut before its first placeholder segment."""
    out = []
    for name, entry in (names or {}).items():
        if not isinstance(entry, dict):
            continue  # `_parse_names` refuses it with the precise reason
        key = "path" if "path" in entry else "under" if "record" in entry else None
        value = entry.get(key) if key else None
        if not isinstance(value, str):
            continue
        parts = []
        for seg in value.strip("/").split("/"):
            if "{" in seg:
                break
            parts.append(seg)
        if parts and parts != [""]:
            out.append((f"{name}.{key}", "/".join(parts)))
    return out


def _top_level_names_in_use(names, roots: tuple[str, ...] | None) -> dict[str, str]:
    """`{top-level directory: why it is taken}`: the family and subject roots, and the first
    directory of every root-relative address a `names:` entry spells: a `path:`
    base, and a `record:` entry's `under:`, which is its pre-move spelling. A
    `runtime:` entry's `under:` is relative to its service directory, not the
    root, so it takes nothing here. `roots` are the subject roots of the
    document being parsed; None takes the engine default's, the answer
    `families.subject_roots()` gives for a tree that lists none."""
    from scripts.heading_os_2.families import family_roots
    from scripts.utils.workspace import get_workspace_root

    if roots is None:
        roots = subject_roots_from_files(None, get_workspace_root())
    taken = dict.fromkeys(roots, "a C3 subject-tree root")
    taken.update(dict.fromkeys(family_roots(), "a HEADING OS 2 family root"))
    for name, entry in (names or {}).items():
        if not isinstance(entry, dict):
            continue  # `_parse_names` refuses it with the precise reason
        keys = ("path",) if "path" in entry else ("under",) if "record" in entry else ()
        for key in keys:
            value = entry.get(key)
            if isinstance(value, str) and value.strip("/"):
                taken.setdefault(value.strip("/").split("/", 1)[0], f"`{name}.{key}`")
    return taken


def _parse_names(names: dict, path: Path) -> dict[str, dict]:
    """Every `names:` entry, or the first refusal one of them earns."""
    out: dict[str, dict] = {}
    for name, entry in names.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise _reject(path, f"name {name!r} must be lowercase letters, digits and hyphens")
        if not isinstance(entry, dict):
            raise _reject(path, f"{name}: the entry must be a mapping, not {type(entry).__name__}")
        retired = sorted(set(entry) & RETIRED_ENTRY_KEYS)
        if retired:
            raise _reject(path, f"{name}: the `root:` key was retired on 2026-09-25 "
                                f"(HOS-132); every name is measured from the data "
                                f"root, so delete the line")
        unknown = sorted(set(entry) - ENTRY_KEYS)
        if unknown:
            raise _reject(path, f"{name}: unknown key(s) {unknown}; allowed: {sorted(ENTRY_KEYS)}")
        what = entry.get("what")
        if not isinstance(what, str) or not what.strip():
            raise _reject(path, f"{name}: needs a `what:` line saying what this address holds")
        bases = [b for b in BASES if b in entry]
        if len(bases) != 1:
            raise _reject(
                path,
                f"{name}: carries {bases or 'no base'}; an entry names exactly one of "
                f"{list(BASES)}. `path:` is an address of its own, `record:` and `runtime:` "
                f"defer to a vocabulary that already answers.")
        base = bases[0]
        parsed: dict = {"what": what.strip(), "base": base}
        if base == "path":
            parsed["path"] = _check_relative(entry["path"], f"{name}.path", path)
        else:
            parsed[base] = _check_pair(entry[base], f"{name}.{base}", path)
        if "under" in entry:
            parsed["under"] = _check_relative(entry["under"], f"{name}.under", path)
        else:
            parsed["under"] = ""
        template = parsed.get("path", "") + "/" + parsed["under"]
        parsed["params"] = tuple(sorted(set(PLACEHOLDER_RE.findall(template))))
        stray = re.findall(r"\{[^}]*\}", template)
        if len(stray) != len(PLACEHOLDER_RE.findall(template)):
            raise _reject(path, f"{name}: a malformed placeholder in {template!r}; "
                                f"parameters are spelled `{{lowercase_name}}`")
        out[name] = parsed
    return out


# ---------------------------------------------------------------------------
# The parsed registry, cached on disk across processes (HOS-173)
# ---------------------------------------------------------------------------
#
# Every hook is a fresh interpreter, so the in-process `lru_cache` below never
# hits for one. MEASURED 2026-09-28, load average 4.4, one fresh process
# resolving `checkout-state`: importing PyYAML 9.2-10.4 ms, parsing the live
# registry 3.4-3.8 ms, parsing the engine's default registry (asked for the
# store name, which the live one does not declare) 1.9-2.7 ms, of a ~35 ms
# resolution. A hit here imports no YAML and parses nothing.
#
# The key is a SHA-256 over the document's TEXT, the bytes of the engine code
# that parses and validates it as they were when this module loaded, and the
# interpreter and PyYAML build doing the parsing. Never mtime or size, for the
# reason `_parse_document_cached` gives. So an edited registry, an edited
# parser and a different YAML build each miss, and nothing is ever answered
# from an entry built for other bytes. An entry is written only after the
# document parsed AND round-tripped exactly; a refused registry leaves nothing.
#
# The address is `<data root>/<store>/data-path/cache/`, read with the line
# reader the overlay guard uses, because asking `runtime_dir` would parse the
# registry this exists to skip. The kind is `cache`, which every store's
# `.gitignore` already covers. A cache that cannot be read or written is said
# on stderr and the document is parsed; it never changes an answer.

_CACHE_FORMAT = 1

#: The engine files whose code decides what a registry text parses TO.
_PARSER_SOURCES = (
    Path(__file__),
    Path(__file__).with_name("registry_bootstrap.py"),
    Path(__file__).with_name("yamlio.py"),
    Path(__file__).resolve().parent.parent / "heading_os_2" / "families.py",
)


def _parser_fingerprint() -> bytes:
    """What besides the text decides the parse: the parser's code and the YAML build."""
    h = _sha256(f"{_CACHE_FORMAT}\0{sys.prefix}\0{sys.version}\0".encode())
    for src in _PARSER_SOURCES:
        h.update(src.read_bytes())
    from importlib.util import find_spec

    spec = find_spec("yaml")
    origin = Path(spec.origin).parent if spec is not None and spec.origin else None
    if origin is not None:
        for entry in sorted(os.scandir(origin), key=lambda e: e.name):
            st = entry.stat()
            h.update(f"\0{entry.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.digest()


try:
    _PARSER_KEY: bytes | None = _parser_fingerprint()
except OSError as _exc:  # pragma: no cover - an engine that cannot read its own source
    print(f"[data-path] registry disk cache off: cannot fingerprint the parser ({_exc})",
          file=sys.stderr)
    _PARSER_KEY = None


class _Uncacheable(ValueError):
    """A parsed value JSON cannot carry back exactly."""


def _encode(value):
    """JSON-safe form that keeps tuples tuples. Every dict is wrapped, so no tag collides."""
    kind = type(value)
    if value is None or kind in (bool, int, float, str):
        return value
    if kind is tuple:
        return {"t": [_encode(v) for v in value]}
    if kind is list:
        return {"l": [_encode(v) for v in value]}
    if kind is dict and all(type(k) is str for k in value):
        return {"d": {k: _encode(v) for k, v in value.items()}}
    raise _Uncacheable(f"a {kind.__name__} value")


def _decode(value):
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is dict and len(value) == 1:
        (tag, inner), = value.items()
        if tag == "t" and type(inner) is list:
            return tuple(_decode(v) for v in inner)
        if tag == "l" and type(inner) is list:
            return [_decode(v) for v in inner]
        if tag == "d" and type(inner) is dict:
            return {k: _decode(v) for k, v in inner.items()}
    raise _Uncacheable(f"an unrecognised JSON value {value!r:.80}")


def _same(a, b) -> bool:
    """Equal AND of the same type at every level: `1 == True` and `(1,) != [1]` both matter here."""
    if type(a) is not type(b):
        return False
    if type(a) is dict:
        return list(a) == list(b) and all(_same(a[k], b[k]) for k in a)
    if type(a) in (list, tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def _cache_file(data_root: Path | None, text: str) -> Path | None:
    """This text's entry under `data_root`'s store, or None where no cache may live.

    None, silently, for a tree with no data root or one inside the engine
    checkout (the demo sentinel), because a cache there is a file in the public
    repository. None, on stderr, when the store's name or the test redirect
    cannot be read: the parse that follows then refuses or answers on its own.
    """
    if _PARSER_KEY is None:
        return None
    from scripts.utils.workspace import get_workspace_root

    try:
        root = (Path(data_root) if data_root is not None else get_data_root()).resolve()
        engine = get_workspace_root().resolve()
        if root == engine or engine in root.parents:
            return None
        store = _runtime._redirected(root)
        if store is None:
            store = root / store_dirname_from_files(root, engine)
    except (DataRootError, StoreKeyError, OSError, _runtime.RuntimeDirError) as exc:
        print(f"[data-path] registry disk cache skipped: {exc}", file=sys.stderr)
        return None
    key = _sha256(_PARSER_KEY + text.encode("utf-8", "surrogatepass")).hexdigest()
    return store.joinpath(*REGISTRY_CACHE_REL) / f"registry-{key}.json"


def _cache_read(entry: Path) -> dict | None:
    """The document an entry holds, or None on a miss. A bad entry is said, then parsed past."""
    try:
        raw = entry.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"[data-path] registry cache {entry} unreadable ({exc}); parsing", file=sys.stderr)
        return None
    try:
        held = json.loads(raw)
        if held.get("format") != _CACHE_FORMAT or held.get("key") != entry.stem:
            raise _Uncacheable("its format or key does not match its name")
        doc = _decode(held["document"])
        if type(doc) is not dict or set(doc) != {NAMES_KEY, SERVICES_KEY, STORE_KEY,
                                                 SUBJECT_ROOTS_KEY, GROUPS_KEY}:
            raise _Uncacheable("it does not hold a registry document")
        return doc
    except (ValueError, KeyError, AttributeError, TypeError) as exc:
        print(f"[data-path] registry cache {entry} is corrupt ({exc}); parsing, "
              f"and it will be rewritten", file=sys.stderr)
        return None


def _cache_write(entry: Path, doc: dict) -> None:
    """Record a parsed document, atomically. Never on a host whose data root is read-only."""
    from scripts.utils.crm_autolog import data_is_readonly

    if data_is_readonly():
        return
    try:
        encoded = _encode(doc)
        if not _same(_decode(json.loads(json.dumps(encoded))), doc):
            raise _Uncacheable("its JSON round trip differs")
    except (_Uncacheable, ValueError) as exc:
        print(f"[data-path] registry not cached: {exc}", file=sys.stderr)
        return
    body = json.dumps({"format": _CACHE_FORMAT, "key": entry.stem, "document": encoded})
    tmp = entry.with_name(f".{entry.name}.{os.getpid()}.tmp")
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, entry)
    # RuntimeError is the overlay write guard's refusal (`OverlayWriteRefused`),
    # raised in a test child aimed at live data. A cache that may not be written
    # costs a parse next time, never the caller's answer.
    except (OSError, RuntimeError) as exc:
        print(f"[data-path] registry cache {entry} not written ({exc})", file=sys.stderr)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError) as cleanup:
            print(f"[data-path] and {tmp} was left behind ({cleanup})", file=sys.stderr)


def _parse_document_disk(text: str, path: Path, data_root: Path | None) -> dict[str, dict]:
    """`_parse_document`, answered from the disk cache when this exact text was parsed before."""
    entry = _cache_file(data_root, text)
    if entry is not None:
        held = _cache_read(entry)
        if held is not None:
            return held
    doc = _parse_document(text, path)
    if entry is not None:
        _cache_write(entry, doc)
    return doc


@lru_cache(maxsize=8)
def _parse_document_cached(path_str: str, text: str,
                           root_str: str | None = None) -> dict[str, dict]:
    """Parse one registry, keyed on its TEXT.

    Keyed on the text and not on `(path, mtime, size)`, which is the key
    `scripts/utils/runtime_dir.py` rejected on 2026-09-14 with a measurement:
    `cp -p`, `rsync -t`, `tar -xp` and a snapshot rollback all put an old mtime
    back, and a size collides on any same-length edit, so a long-lived process
    kept answering from a registry that had been replaced.

    `root_str` names the data root whose store holds the disk cache; it is in
    the key only so a changed `HEADING_OS_DATA` writes to its own tree.
    """
    return _parse_document_disk(text, Path(path_str),
                                Path(root_str) if root_str is not None else None)


def load_registry_document(data_root: Path | None = None) -> dict[str, dict] | None:
    """The whole validated registry, or `None` when this tree has none.

    `None` and not an empty mapping, because the two consumers need to tell the
    cases apart and one of them must refuse on absence. `load_registry` reads
    `None` as "no residue names here", which is right for a public clone;
    `runtime_dir.load_services` reads it as "no service may be written under",
    which is a refusal. Collapsing them would put the `load_exec_registry`
    failure shape (see the module header) back: an absent mandatory source read as an empty one.
    """
    path = registry_path(data_root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _reject(path, f"cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise _reject(path, f"is not valid UTF-8 ({exc})") from exc
    return _parse_document_cached(str(path), text,
                                  str(data_root) if data_root is not None else None)


def example_names_path() -> Path:
    """The engine-shipped DEFAULT `names:` file.

    Resolved from `get_workspace_root()` so a worktree names its own copy, never
    a constant and never the common git directory. The twin of
    `runtime_dir.example_services_path()`, and for the same reason.
    """
    from scripts.utils.workspace import get_workspace_root

    return get_workspace_root().joinpath(*EXAMPLE_NAMES_REL)


@lru_cache(maxsize=4)
def _template_names_cached(path_str: str, text: str) -> dict[str, dict]:
    return _parse_names_document(text, Path(path_str))


def _parse_names_document(text: str, path: Path) -> dict[str, dict]:
    """The default file, through the SAME parser the live registry goes through.

    One implementation for both, because a rule enforced on the live registry
    only is a rule that lets a malformed entry reach every tree that has no
    registry of its own.
    """
    doc = _parse_document_disk(text, path, None)
    names = doc[NAMES_KEY]
    if not names:
        raise _reject(
            path,
            f"holds no non-empty `{NAMES_KEY}:` mapping. This is what "
            f"`load_registry` answers from on a tree whose registry names "
            f"nothing, so an empty one here would put every bare name back to "
            f"`UnknownDataPath` on exactly the trees this file exists for. "
            f"Refusing instead.")
    return names


def template_names(cached: bool = True) -> dict[str, dict]:
    """The `names:` entries the ENGINE ships, read from the default file.

    **What that file may hold is a boundary, not a preference.** It goes to the
    public repository, so it carries the HEADING OS 2 structure -- families,
    leaves, and the generic sub-addresses inside them, which are identical in
    every workspace -- and nothing belonging to one operator. Two names the
    engine's own code asks for are deliberately absent: one addresses a dated
    one-off directory and one a particular workbook file, and neither is
    structure. Both still refuse on a tree with no registry, which is the honest
    outcome; a caller that needs them is running on a tree that names them.

    Refuses rather than returning `{}` when the file is unreadable or empty:
    an empty default is the `load_exec_registry` failure shape (an absent
    mandatory source read as an empty one), and every caller would see a
    registry that names nothing while looking well-formed.
    """
    path = example_names_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _reject(
            path,
            f"the engine's default `{NAMES_KEY}:` file is unreadable ({exc}). It "
            f"is what a tree with no registry of its own is answered from: a "
            f"public clone on the bundled demo tree, or a data root "
            f"`scripts/init-data.py` has just scaffolded.") from exc
    if not cached:
        return _parse_names_document(text, path)
    return _template_names_cached(str(path), text)


def load_registry(data_root: Path | None = None) -> dict[str, dict]:
    """Every `names:` entry, or a refusal. THREE answers, as `load_services` has three.

    1. A registry that EXISTS AND WILL NOT PARSE refuses here, immediately and
       for every name. A registry that cannot be trusted is not read
       optimistically.
    2. A tree whose registry carries a non-empty `names:` block is answered from
       it, and it ALWAYS WINS over the engine's default. The operator's file is
       the authority on his own tree; nothing is merged in behind it, because a
       merge that guesses is how a registry gains a line nobody chose.
    3. A tree with NO registry, or one carrying no `names:` block, falls back to
       the engine's shipped default. Absent is not broken: a public clone on the
       bundled demo tree and a data root `scripts/init-data.py` has just
       scaffolded both legitimately have none --
       that script seeds from the services template, which carries no `names:`
       block, so a data repository is BORN without one.

    Until 2026-09-22 case 3 returned `{}` and every bare name raised
    `UnknownDataPath` on those trees, which is what held 49 call sites on the
    legacy seam (`scripts/workspace-health.py` lines 49-80 records the
    measurement and names this file as the condition for converting them).
    """
    doc = load_registry_document(data_root)
    names = dict(doc[NAMES_KEY]) if doc is not None else {}
    return names or template_names()


def template_groups() -> dict[str, tuple[str, ...]]:
    """The `groups:` the ENGINE ships, read from the default file; `{}` when it ships none."""
    path = example_names_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _reject(path, f"the engine's default registry is unreadable ({exc})") from exc
    return dict(_template_document_cached(str(path), text)[GROUPS_KEY] or {})


def load_groups(data_root: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Every named group, `{group: (registry name, ...)}`. The `load_registry` rule.

    A tree whose registry carries a `groups:` block is answered from it, and it
    ALWAYS WINS; nothing is merged in behind it. A tree with no registry, or
    one carrying no `groups:` block, is answered from the engine's default
    file, which groups only the generic names it ships itself.
    """
    doc = load_registry_document(data_root)
    groups = doc[GROUPS_KEY] if doc is not None else None
    return dict(groups) if groups else template_groups()


def group_names(group: str, data_root: Path | None = None) -> tuple[str, ...]:
    """The registry names one group lists, in its order. An unknown group refuses."""
    groups = load_groups(data_root)
    if group not in groups:
        raise UnknownDataPath(
            f"{group!r} is not a group this workspace names. Registry: "
            f"{registry_path(data_root)}; known groups: "
            f"{', '.join(sorted(groups)) or '(none)'}. Add it under `{GROUPS_KEY}:`.")
    return groups[group]


def store_dirname(data_root: Path | None = None) -> str:
    """The runtime store's directory name under `data_root`: the registry's `runtime_store:`.

    The tree's registry answers when it declares the key, and it ALWAYS WINS;
    otherwise the engine's default file does, which declares `runtime-junk`, so a
    tree with no entry resolves exactly as it did while the name was the engine
    constant `runtime_dir.STORE_DIRNAME` (operator's ruling 2026-09-14, reversed
    by him 2026-09-25: "yes, move it", translated). The default file carrying no key is a
    refusal, never a guess: a guessed store is a store nobody's ignore rules
    cover.

    `data_root` defaults to `get_data_root()`, as `registry_path` does;
    `runtime_dir.runtime_root` passes its own writable root explicitly, so the
    store's name and the store's parent are always read off the same tree.
    """
    doc = load_registry_document(data_root)
    if doc is not None and doc[STORE_KEY] is not None:
        return doc[STORE_KEY]
    return template_store_dirname(data_root)


#: (path, inode, size, mtime, ctime) of the default file -> its store name. The
#: default is read on every runtime resolution, and it is engine code that
#: changes only on an edit; ctime moves on any rewrite, including one that puts
#: an old mtime back, which is why an mtime-only key is not used.
_TEMPLATE_STORE: dict[tuple, str] = {}


def template_store_dirname(data_root: Path | None = None) -> str:
    """The store name the ENGINE's default registry declares: where a tree with no entry keeps it.

    `data_root` only chooses whose store holds the parse's disk cache; the
    answer is the engine file's whatever it is.
    """
    path = example_names_path()
    try:
        st = path.stat()
        key = (str(path), st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except OSError:
        key = None
    if key is not None and key in _TEMPLATE_STORE:
        return _TEMPLATE_STORE[key]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _reject(path, f"the engine's default registry is unreadable ({exc}); it "
                            f"is what names the runtime store on a tree whose "
                            f"registry does not") from exc
    value = _template_document_cached(str(path), text,
                                      str(data_root) if data_root is not None else None)[STORE_KEY]
    if value is None:
        raise _reject(path, f"declares no `{STORE_KEY}:`; the engine's default must "
                            f"name the runtime store, or a tree with no entry has none")
    if key is not None:
        _TEMPLATE_STORE.clear()
        _TEMPLATE_STORE[key] = value
    return value


@lru_cache(maxsize=4)
def _template_document_cached(path_str: str, text: str,
                              root_str: str | None = None) -> dict[str, dict]:
    return _parse_document_disk(text, Path(path_str),
                                Path(root_str) if root_str is not None else None)


def invalidate_cache() -> None:
    """Drop the parsed registry. For a caller that needs certainty over speed."""
    _parse_document_cached.cache_clear()
    _template_names_cached.cache_clear()
    _template_document_cached.cache_clear()
    _TEMPLATE_STORE.clear()


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def _data_root(data_root: Path | None) -> Path:
    return (Path(data_root) if data_root is not None else get_data_root()).resolve()


def _under(base: Path, rel: str, name: str) -> Path:
    """`base / rel`, resolved, with containment checked AFTER resolution.

    After, so a symlinked component cannot carry an address out of the tree that
    the textual check already cleared. The same order `resolve._under` uses, and
    for the same reason; it is a containment ASSERTION rather than a second
    resolver, which is why it may live here.
    """
    if not rel:
        return base
    resolved = (base / rel).resolve()
    if resolved != base and base not in resolved.parents:
        raise AddressEscapesDataRoot(
            f"{name}: {rel!r} resolves to {resolved}, which is outside the data "
            f"root {base}. Refusing: an address that leaves the data repository "
            f"is not a data address.")
    return resolved


def _fill(template: str, name: str, params: dict[str, str], expected: tuple[str, ...]) -> str:
    """Substitute the parameters, refusing a mismatch in either direction.

    Both directions on purpose. A MISSING parameter would leave `{person}` in a
    path and create a directory of that name; an EXTRA one is a caller that
    believes this address takes an argument it does not, and answering anyway
    teaches it that the belief was right.
    """
    supplied = tuple(sorted(params))
    if supplied != expected:
        missing = sorted(set(expected) - set(supplied))
        extra = sorted(set(supplied) - set(expected))
        raise ParameterMismatch(
            f"{name}: takes {list(expected) or 'no parameters'} and was given "
            f"{list(supplied) or 'none'}"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected {extra}" if extra else ""))
    for key, value in params.items():
        if not isinstance(value, str) or not PARAM_VALUE_RE.match(value):
            raise ParameterMismatch(
                f"{name}: parameter {key}={value!r} is not a single path segment "
                f"(letters, digits, dot, underscore, hyphen; no slash, no `..`)")
    return template.format(**params) if expected else template


def _entry_addresses(name: str, entry: dict, params: dict[str, str], *,
                     data_root: Path | None, reading: bool) -> tuple[Path, ...]:
    root = _data_root(data_root)
    tail = _fill(entry["under"], name, params, entry["params"]) if entry["under"] else ""
    base = entry["base"]
    if base == "path":
        rel = _fill(entry["path"], name, params, entry["params"])
        return (_under(root, rel, name),)
    if base == "record":
        family, leaf = entry["record"]
        # The data root is this entry's LEGACY home, and that is a fact about
        # the move rather than a convenience: `<legacy>` became
        # `<family>/<leaf>/<legacy>`, so an entry's `under:` IS its pre-move
        # root-relative address and the directory that address hangs off is the
        # root itself. `scripts/heading_os_2/compat.legacy_relative` states the
        # same shape from the other end. So a tree with no manifest can be
        # answered here from that legacy address, and a tree that HAS one is
        # untouched: the fallback is reached only through
        # `NoManifestOnThisTree`, and `_refuse_if_migrated` still vetoes a tree
        # that migrated and lost its manifest.
        dirs = (_record_read_dirs(family, leaf, data_root, legacy_root=root) if reading
                else (_record_write_dir(family, leaf, data_root, legacy_root=root),))
        return tuple(_under(d, tail, name) if tail else d for d in dirs)
    service, kind = entry["runtime"]
    one = _runtime_dir(service, kind, data_root)
    return (_under(one, tail, name) if tail else one,)


def _record_write_dir(family: str, leaf: str, data_root: Path | None, *,
                      legacy_root: Path | None = None) -> Path:
    """`write_dir`, degrading to the legacy directory ONLY where one is named.

    `legacy_root` is supplied by a REGISTRY ENTRY, which carries its pre-move
    address in `under:`, and never by the bare `record:<family>/<leaf>` scheme
    form, which carries no address at all. That asymmetry is compat's own rule,
    written there as "from directories the CALLER names": a scheme form with no
    sub-address would degrade to the data root itself, and a plausible-looking
    wrong answer is the one output this seam may not produce. So the scheme form
    goes on refusing on a tree with no manifest, and says so.
    """
    try:
        if legacy_root is not None:
            return _compat.write_dir_or_legacy(family, leaf, legacy_root,
                                               data_root=data_root)
        return _resolve.write_dir(family, leaf, data_root=data_root)
    except _resolve.ManifestError as exc:
        raise DataPathError(f"record:{family}/{leaf}: {exc}") from exc


def _record_read_dirs(family: str, leaf: str, data_root: Path | None, *,
                      legacy_root: Path | None = None) -> tuple[Path, ...]:
    """`read_scope().dirs`, degrading as `_record_write_dir` does.

    One difference from the write side, and it is compat's and not this
    module's: `dirs_or_legacy` also absorbs a VALID kind the manifest does not
    place, answering with no directories and saying so once on stderr, where
    `write_dir_or_legacy` absorbs nothing. That is the right split -- a reader
    of a kind with no records yet must report nothing, a WRITER of one has no
    home to be given -- and it applies here only to a registry name, for the
    same reason the fallback does. `data_read_required` still refuses when
    nothing is at any address, so a mandatory source cannot come back empty.
    """
    try:
        if legacy_root is not None:
            return tuple(_compat.dirs_or_legacy(family, leaf, legacy_root,
                                                data_root=data_root))
        return tuple(_resolve.read_scope(family, leaf, data_root=data_root).dirs)
    except _resolve.ManifestError as exc:
        raise DataPathError(f"record:{family}/{leaf}: {exc}") from exc


def _runtime_dir(service: str, kind: str, data_root: Path | None) -> Path:
    """`<data root>/<runtime store>/<service>/<kind>`, refused before the disk is touched.

    The ONE resolution of a runtime address (since HOS-5 § 6, 2026-09-25, when
    `runtime_dir.get_runtime_dir` was removed). Its two refusals are raised as
    the runtime module's own classes and re-dressed below. `runtime_dir` still
    owns the vocabulary this checks against (`KINDS`, the
    `services:` rows it validates) and the store root; it no longer answers an
    address.

    `DataRootError` is re-dressed too since the service list moved under the
    data root: on a tree with no writable overlay there is no list to check
    against, and the refusal has to arrive as a `DataPathError` or the CLI's
    `except` clause misses it and a caller gets a traceback instead of
    `REFUSED:`.
    """
    try:
        if not isinstance(kind, str) or kind not in _runtime.KINDS:
            raise _runtime.UnknownKind(
                f"runtime kind {kind!r} is not one of {', '.join(_runtime.KINDS)}")
        if not isinstance(service, str) or not service:
            raise _runtime.UnknownService("runtime service name must be a non-empty string")
        services = _runtime.load_services(data_root)
        if service not in services:
            raise _runtime.UnknownService(
                f"runtime service {service!r} has no row in "
                f"{_runtime.services_path(data_root)}; known: "
                f"{', '.join(sorted(services))}. Add the row, with one line "
                "saying what the service is, before writing under it.")
        return _runtime.runtime_root(data_root) / service / kind
    except (_runtime.RuntimeDirError, DataRootError) as exc:
        raise DataPathError(f"runtime:{service}/{kind}: {exc}") from exc


def _in_runtime_store(name: str, data_root: Path | None) -> bool:
    """Whether `name` answers through `_runtime_dir`, and so lands in the store.

    Read off the name's own form, never off the resolved path: walking back up
    from an address to decide what it is would be a second resolution.
    """
    if name.startswith(RUNTIME_SCHEME):
        return True
    if name.startswith(RECORD_SCHEME):
        return False
    entry = load_registry(data_root).get(name)
    return entry is not None and entry["base"] == "runtime"


def _scheme_pair(name: str, scheme: str) -> tuple[str, str]:
    rest = name[len(scheme):]
    parts = rest.split("/")
    if len(parts) != 2 or not all(parts):
        raise UnknownDataPath(
            f"{name!r}: a {scheme.rstrip(':')!r} name is spelled "
            f"`{scheme}<first>/<second>`, and {rest!r} is not that shape")
    return parts[0], parts[1]


def _addresses(name: str, params: dict[str, str], *, data_root: Path | None,
               reading: bool) -> tuple[Path, ...]:
    if not isinstance(name, str) or not name:
        raise UnknownDataPath("a data-path name must be a non-empty string")
    if name.startswith(RECORD_SCHEME):
        family, leaf = _scheme_pair(name, RECORD_SCHEME)
        _fill("", name, params, ())
        return (_record_read_dirs(family, leaf, data_root) if reading
                else (_record_write_dir(family, leaf, data_root),))
    if name.startswith(RUNTIME_SCHEME):
        service, kind = _scheme_pair(name, RUNTIME_SCHEME)
        _fill("", name, params, ())
        return (_runtime_dir(service, kind, data_root),)
    registry = load_registry(data_root)
    entry = registry.get(name)
    if entry is None:
        known = ", ".join(sorted(registry)) or "(the registry names nothing here)"
        raise UnknownDataPath(
            f"{name!r} is not a data address this workspace names. Registry: "
            f"{registry_path(data_root)}; known names: {known}. A record kind is "
            f"asked for as `{RECORD_SCHEME}<family>/<leaf>` and runtime state as "
            f"`{RUNTIME_SCHEME}<service>/<kind>`. Add the name to the registry "
            f"rather than writing the path out.")
    return _entry_addresses(name, entry, params, data_root=data_root, reading=reading)


def data_path(name: str, *, data_root: Path | None = None, **params: str) -> Path:
    """The ONE address a new file of this name belongs at. Not created.

    Always a single Path, for every name, because writing is always answerable:
    a new record of a kind has exactly one correct destination. Reading is the
    question that may have several answers, and it is `data_read`.
    """
    addresses = _addresses(name, params, data_root=data_root, reading=False)
    if len(addresses) != 1:  # pragma: no cover - the write side is single by construction
        raise DataPathError(
            f"{name!r} resolved to {len(addresses)} write addresses; writing has "
            f"exactly one answer. This is a defect in this module, not in the call.")
    return addresses[0]


def data_write_dir(name: str, *, data_root: Path | None = None, **params: str) -> Path:
    """The ONE directory new files of this name go into, CREATED. The write operation.

    HOS-5 § 4: the resolver computes a path and creates nothing, and "creating
    the directory for a result is done explicitly by the write operation". This
    is that operation. It answers exactly what `data_path` answers for the same
    arguments, refuses everything `data_path` refuses and before the disk is
    touched, and then makes the directory with `parents=True`.

    For an address in the runtime store it FIRST makes the store root through
    `runtime_dir.ensure_store_root`, at 0700 with its README, and that ordering
    is the reason this is one operation rather than `data_path(...).mkdir(...)`
    at each caller. MEASURED 2026-09-22 on a scratch root: a caller's own
    `mkdir(parents=True)` under the default umask leaves the store root 0o755,
    and the store holds browser cookies, drafted mail bodies and calendar dumps.
    On an overlay where the root already exists the two are indistinguishable,
    which is exactly why a hand-written `mkdir` would ship green.

    The name must address a DIRECTORY. A registry name that places one file
    (`browser-cookies`) goes through `data_write_file`, which creates the
    file's parent instead; creating a directory at a file's address is not
    something this can undo.
    """
    target = data_path(name, data_root=data_root, **params)  # leak-guard: ok (the write operation receives the address its caller spelled; the caller is checked at its own call)
    if _in_runtime_store(name, data_root):
        _runtime.ensure_store_root(data_root)
    target.mkdir(parents=True, exist_ok=True)
    return target


def data_write_file(name: str, *, data_root: Path | None = None, **params: str) -> Path:
    """The ONE address a FILE of this name is written at, its directory CREATED.

    The write operation for a name that places a single file, which
    `data_write_dir` cannot be: it would make a directory at the file's address.
    Same answer and same refusals as `data_path`, then the store root at 0700
    for a runtime address, then the file's parent directory. The file itself is
    not touched; writing it is the caller's.

    It exists because a writer that needed the directory used to ask
    `data_write_dir("runtime:<service>/<kind>")` and hang the file name off it,
    so the registry name placing that file was never asked and editing its entry
    moved nothing. MEASURED 2026-09-25 for `browser-launch-log` on a synthetic
    root: after the entry was edited the CLI answered the new address and
    `browser.launch_log()` still answered, and created, the old one.
    """
    target = data_path(name, data_root=data_root, **params)  # leak-guard: ok (the write operation receives the address its caller spelled; the caller is checked at its own call)
    if _in_runtime_store(name, data_root):
        _runtime.ensure_store_root(data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# Two registry names give ONE engine checkout its own directory under the data
# root, `checkout-scratch` and `checkout-state`, each parameterised by
# `checkout_name()`. HOS-5 section 7, 2026-09-25: the checkout used to keep its
# scratch in `<checkout>/.tmp/` and its hook state in `<checkout>/.claude/state/`,
# both gitignored and both inside the public engine. Operator decision that day,
# verbatim: "old way should go out and do not stay in the workspace." Each call
# spells its name as a literal, which is what the leak guard reads.


def checkout_name(checkout: Path | None = None) -> str:
    """The parameter value that names one engine checkout: its directory name.

    Derived from the checkout itself, never from a constant or the common git
    directory, so a YARD and HELM each get their own directory (CLAUDE.md, "Guards
    must be armed inside the task"). A leading dot is dropped because a parameter
    value may not begin with one (`PARAM_VALUE_RE`): the main clone `.heading-os`
    is `heading-os`. Two checkouts with one directory name share a directory,
    which is the price of a name a person can read, and the reason a YARD is
    created only through `herdr worktree create`, which names each one uniquely.
    """
    if checkout is None:
        from scripts.utils.workspace import get_workspace_root

        checkout = get_workspace_root()
    name = Path(checkout).resolve().name.lstrip(".")
    if not PARAM_VALUE_RE.match(name):
        raise ParameterMismatch(
            f"the checkout directory name {Path(checkout).name!r} cannot name a "
            f"directory under the data root; rename the checkout")
    return name


def checkout_scratch_dir(checkout: Path | None = None, *,
                         data_root: Path | None = None) -> Path:
    """THIS checkout's scratch directory under the data root, created.

    What `.tmp/` inside the checkout used to be: a commit message a YARD leaves
    for the operator, a registry stanza for HELM, a measurement's raw output. It
    is durable across reboots, which `/tmp` is not, it is one directory per
    checkout, so parallel yards do not collide, and it is ignored by the data
    repository's git, because the store's `tmp/` kind is.

    From a shell: `python scripts/data-path.py scratch`.
    """
    return data_write_dir("checkout-scratch", data_root=data_root,
                          checkout=checkout_name(checkout))


def data_read(name: str, *, data_root: Path | None = None, **params: str) -> tuple[Path, ...]:
    """Every address a reader of this name must look at. May find nothing.

    For a record kind during the transition this is MANY directories, and a
    caller must not assume one: `resolve.read_scope` answers with 216 for the
    widest measured pair. Finding nothing on disk is a legitimate answer here;
    a caller for whom it is not calls `data_read_required` instead.
    """
    return _addresses(name, params, data_root=data_root, reading=True)


def data_read_required(name: str, *, data_root: Path | None = None,
                       **params: str) -> tuple[Path, ...]:
    """The addresses of a MANDATORY source, refusing when none of them is there.

    The whole point is the refusal. HOS-5 § 4 asks for it in these words: "the
    read of a mandatory source checks its presence separately; the absence of
    such a source must not turn into a successful result with zero records."

    Returns only the addresses that EXIST, so a caller cannot walk a directory
    that is not there and read the empty result as an empty source. If none
    exists, it raises, naming every address it looked at, because a refusal that
    does not say where it looked is a hunt.
    """
    addresses = _addresses(name, params, data_root=data_root, reading=True)
    present = tuple(p for p in addresses if p.exists())
    if not present:
        looked = "\n  ".join(str(p) for p in addresses) or "(no address at all)"
        raise MissingRequiredSource(
            f"{name!r} was declared a mandatory source and nothing is at any of "
            f"its addresses:\n  {looked}\n"
            f"Refusing to answer with zero records: an absent mandatory source "
            f"that reads as an empty one is the defect this operation exists to "
            f"prevent (the former load_exec_registry in scripts/utils/workspace.py). If "
            f"an absence is legitimate here, the caller wants data_read().")
    return present


#: A config value naming a SET of addresses: `group:<group>`, one of `groups:`.
GROUP_SCHEME = "group:"  # leak-guard: ok (the scheme itself, not an address spelled with it)


def config_names(value: str, *, field: str,
                 data_root: Path | None = None) -> tuple[tuple[str, Path], ...] | None:
    """The `(name, address)` pairs a value in a data-repository config file names.

    HOS-234 part 5: the data repository's own config files (Sentinel's
    `context_files`, the service manifest's `include`, the service host's
    `state_dirs`, the operator's `voice_reference`) name data by registry name
    instead of by path, so a move changes the registry and nothing else.

    A value is a NAME when it is `group:<group>`, a `record:`/`runtime:` name,
    or matches `NAME_RE` whole; a group answers every member in group order.
    Anything else (it holds a `/` or a `.`) is a PATH, and the answer is None:
    what a path is relative to differs per reader, so the reader keeps its own
    old resolution for it and says so through `deprecated_config_path`. A value
    shaped as a name that the registry does not place REFUSES here, by name,
    with `field` in the message; it is never read as a path instead.
    """
    if not isinstance(value, str) or not value.strip():
        raise UnknownDataPath(f"{field}: an empty or non-string value {value!r}")
    if value.startswith(GROUP_SCHEME):
        try:
            members = group_names(value[len(GROUP_SCHEME):], data_root)
            return tuple((n, data_path(n, data_root=data_root))  # leak-guard: ok (a member of a registry group; the registry lists the names)
                         for n in members)
        except DataPathError as exc:
            raise type(exc)(f"{field}: {exc}") from exc
    if value.startswith((RECORD_SCHEME, RUNTIME_SCHEME)) or NAME_RE.match(value):
        try:
            return ((value, data_path(value, data_root=data_root)),)  # leak-guard: ok (a registry name read from a data config file; the wall over those files checks it)
        except DataPathError as exc:
            raise type(exc)(f"{field}: {exc}") from exc
    return None


def deprecated_config_path(field: str, value: str) -> None:
    """Say on stderr that a config value is still a physical path. One wording for every reader."""
    print(f"DEPRECATED: {field} holds the physical path {value!r}; name it by registry "
          f"name (`python scripts/data-path.py names`) or `{GROUP_SCHEME}<group>` "
          f"(HOS-234). The path form is accepted only while the configs are switched.",
          file=sys.stderr)


def validate_address(name: str, data_root: Path | None = None) -> str | None:
    """None when `name` is an address the ENGINE'S OWN vocabularies place, else why not.

    The one question three guards ask -- `scripts/leak-guard.py`,
    `scripts/check-data-path-references.py` and
    `scripts/utils/runtime_write_guard.py` -- answered in one place. Each of
    them had, or was about to grow, its own copy of "is that a family, a leaf, a
    service, a kind", which is this repository's named dominant defect and the
    reason the copy nobody touches is the one that stops being right.

    **It deliberately does not call `known_names()`, and that is the whole of
    why it is safe to gate on.** `known_names` returns FEWER names on a tree
    with no manifest or no writable overlay: it catches three exceptions and
    carries on, because LISTING is the one operation where an incomplete answer
    is honest rather than broken. A gate keyed on it would refuse every address
    on a public clone, in CI, and under a test fixture pinned at an empty
    scratch root. `FAMILIES` and `KINDS` are engine constants and answer
    identically everywhere; the service list falls back to the engine's own
    shipped template when there is no registry to read, which is the move
    `runtime_write_guard.misrouted()` measured and wrote down on 2026-09-19:
    public code may only name a service the engine ships anyway.

    A BARE REGISTRY NAME returns None, stated here rather than left to be
    discovered. `crm-config` is placed by a file under the data root, so a
    public clone has nothing to check it against, and refusing it there would be
    the same defect by a shorter route. This function answers about the two
    DERIVED vocabularies and says nothing about the third.
    """
    from scripts.utils.runtime_dir import KINDS, RuntimeDirError, known_services, template_services

    if not isinstance(name, str):
        return "an address must be a string"
    if name.startswith(RECORD_SCHEME):
        first, sep, second = name[len(RECORD_SCHEME):].partition("/")
        if not sep or not first or not second or "/" in second:
            return (f"{name!r} is not spelled `{RECORD_SCHEME}<family>/<leaf>`")
        from scripts.heading_os_2.families import FAMILIES
        leaves = FAMILIES.get(first)
        if leaves is None:
            return (f"no family named {first!r}; the nine are "
                    f"{', '.join(sorted(FAMILIES))}")
        if second not in leaves:
            return (f"family {first!r} has no leaf {second!r}; its leaves are "
                    f"{', '.join(leaves)}")
        return None
    if name.startswith(RUNTIME_SCHEME):
        first, sep, second = name[len(RUNTIME_SCHEME):].partition("/")
        if not sep or not first or not second or "/" in second:
            return (f"{name!r} is not spelled `{RUNTIME_SCHEME}<service>/<kind>`")
        try:
            services = set(known_services(data_root))
            where = "the registry under the data root"
        except (RuntimeDirError, DataRootError):
            services = set(template_services())
            where = "the engine's shipped service template"
        if first not in services:
            return f"no service named {first!r} in {where}"
        if second not in KINDS:
            return f"{second!r} is not one of the four kinds: {', '.join(KINDS)}"
        return None
    return None


def known_names(data_root: Path | None = None) -> tuple[str, ...]:
    """Every name that can be asked for on this tree, derived plus registered.

    Derived from the two vocabularies rather than listed, so a family/leaf pair
    added to the manifest or a service added to the registry's `services:` block
    is answerable the same day with nothing edited here. A tree with no manifest
    contributes no record names, which is the honest answer on a public clone
    rather than a refusal.

    `DataRootError` is caught beside `RuntimeDirError` for exactly that reason,
    and only since the services moved into the registry: the runtime vocabulary
    now lives under the data root, so a clone with no writable one has no service
    list to list. LISTING is the one operation where that is not a defect. Asking
    for a runtime address on such a tree still refuses, loudly, in `_runtime_dir`.
    """
    names: list[str] = []
    try:
        for family, leaf in sorted(_resolve.load_locations(data_root=data_root)):
            names.append(f"{RECORD_SCHEME}{family}/{leaf}")
    except _resolve.ManifestError:
        pass
    try:
        for service in sorted(_runtime.known_services(data_root)):
            names.extend(f"{RUNTIME_SCHEME}{service}/{kind}" for kind in _runtime.KINDS)
    except (_runtime.RuntimeDirError, DataRootError):
        pass
    names.extend(sorted(load_registry(data_root)))
    return tuple(names)


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------


def _params_from_argv(pairs: list[str]) -> dict[str, str]:
    """`person=jane` off the command line, refusing anything else.

    A shell cannot call a Python function, which is the whole reason this entry
    point exists: without it every skill, agent brief and shell script stays on
    literal paths, and `scripts/audit-skill-bash-paths.py` has nothing to point
    them at. So the parameter syntax is the plainest thing a shell can type, and
    a malformed pair refuses rather than being read as a name.
    """
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ParameterMismatch(
                f"{pair!r} is not a `name=value` parameter. Parameters are "
                f"passed as `person=jane`, one word each side of the `=`.")
        if key in out:
            raise ParameterMismatch(f"parameter {key!r} was given twice")
        out[key] = value
    return out


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="data-path",
        description="Resolve a logical data-address name to a path. The same "
                    "resolver the engine's Python calls, so a skill, an agent "
                    "brief or a shell script never has to spell an address.",
        epilog="Names: `record:<family>/<leaf>`, `runtime:<service>/<kind>`, or "
               "a name the registry places. `data-path names` lists every one.",
    )
    parser.add_argument("--data-root", type=Path, default=None,
                        help="the data repository to resolve against "
                             "(default: this machine's)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_path = sub.add_parser("path", help="the ONE address a new file goes at")
    p_path.add_argument("name")
    p_path.add_argument("params", nargs="*", metavar="key=value")
    p_path.add_argument("--create", action="store_true",
                        help="the write operation: make the directory first, "
                             "through data_write_dir (a runtime address gets "
                             "the store root at 0700), and print it")

    p_read = sub.add_parser("read", help="every address a reader must look at")
    p_read.add_argument("name")
    p_read.add_argument("params", nargs="*", metavar="key=value")
    p_read.add_argument("--required", action="store_true",
                        help="refuse when nothing is at any of them, instead of "
                             "answering with an address that holds nothing")

    sub.add_parser(
        "scratch",
        help="THIS checkout's scratch directory under the data root, created "
             "(what `<checkout>/.tmp/` used to be)")
    sub.add_parser("names", help="every name this tree can answer")
    sub.add_parser("groups", help="every named group of names this tree can answer")
    p_group = sub.add_parser(
        "group", help="the address of every name in one group, in group order")
    p_group.add_argument("group")
    sub.add_parser("where", help="the registry file in use")
    sub.add_parser("accounts", help="every account's slug, the `{account}` values")
    p_each = sub.add_parser(
        "each", help="an `{account}` name's address for every account that has it")
    p_each.add_argument("name")
    p_each.add_argument("--all", action="store_true",
                        help="every account, including those without this directory")

    p_syn = sub.add_parser(
        "synthetic",
        help="build a throwaway DATA repository the resolvers can answer over")
    p_syn.add_argument("dest", type=Path)

    args = parser.parse_args(argv)
    root = args.data_root

    try:
        if args.cmd == "path":
            # The marker below has to sit on the CALL's own line, which is where
            # `_non_literal_front_door_calls` reports it. The reason in full:
            # this is the ONE place in the engine where an address legitimately
            # arrives as a value rather than a literal, because the command
            # exists so that a shell can ask and a shell hands its argument over
            # as argv. A file-level exemption would have been the wrong tool --
            # it stops checking this module entirely, and this is the module the
            # gate most needs to check.
            # `--create` is the shell's door to the write operation, so a skill
            # that needs a scratch directory never pairs `path` with a hand
            # `mkdir -p`, which under the default umask would leave a new
            # runtime store root readable by everyone (HOS-5 § 7, defect 5).
            resolve = data_write_dir if args.create else data_path
            print(resolve(args.name, data_root=root,  # leak-guard: ok (argv is the address here; see above)
                          **_params_from_argv(args.params)))
            return 0

        if args.cmd == "read":
            reader = data_read_required if args.required else data_read
            for found in reader(args.name, data_root=root,
                                **_params_from_argv(args.params)):
                print(found)
            return 0

        if args.cmd == "scratch":
            print(checkout_scratch_dir(data_root=root))
            return 0

        if args.cmd == "names":
            for name in known_names(root):
                print(name)
            return 0

        if args.cmd == "groups":
            for group in sorted(load_groups(root)):
                print(group)
            return 0

        if args.cmd == "group":
            from scripts.utils.accounts import addresses
            failed = 0
            for name in group_names(args.group, data_root=root):
                try:
                    # An `{account}` member prints one address per account.
                    for one in addresses(name, data_root=root):
                        print(one)
                except DataPathError as exc:
                    failed += 1
                    print(f"REFUSED: {name}: {exc}", file=sys.stderr)
            return 1 if failed else 0

        if args.cmd == "accounts":
            from scripts.utils.accounts import account_slugs
            for slug in account_slugs(root):
                print(slug)
            return 0

        if args.cmd == "each":
            from scripts.utils.accounts import each_account
            for slug, home in each_account(args.name, root, existing=not args.all):
                print(f"{slug}\t{home}")
            return 0

        if args.cmd == "where":
            path = registry_path(root)
            print(path)
            if not path.is_file():
                print("# no registry on this tree; the two vocabularies still "
                      "answer, and a registry name does not", file=sys.stderr)
            return 0

        if args.cmd == "synthetic":
            from scripts.utils.synthetic_data_root import build_synthetic_data_root
            print(build_synthetic_data_root(args.dest))
            return 0
    except DataPathError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    return 2  # pragma: no cover - argparse requires a subcommand


__all__ = [
    "ADDRESS_RE", "AddressEscapesDataRoot", "DataPathError", "GROUPS_KEY",
    "SUBJECT_ROOTS_KEY", "checkout_name", "group_names", "load_groups", "template_groups",
    "checkout_scratch_dir",
    "EXAMPLE_NAMES_REL", "MissingRequiredSource", "NAMES_KEY",
    "ParameterMismatch", "REGISTRY_REL", "RECORD_SCHEME", "RUNTIME_SCHEME",
    "RegistryMalformed", "SCHEMA_VERSION", "SERVICES_KEY", "STORE_KEY", "UnknownDataPath",
    "GROUP_SCHEME", "addresses_in", "config_names", "data_path", "data_read",
    "data_read_required", "data_write_dir", "data_write_file", "deprecated_config_path",
    "example_names_path", "invalidate_cache", "known_names", "load_registry",
    "load_registry_document", "registry_path", "store_dirname", "template_names",
    "template_store_dirname",
    "validate_address",
]
