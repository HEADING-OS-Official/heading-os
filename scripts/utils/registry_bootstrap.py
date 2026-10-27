"""What can be read out of the registry before the `scripts` package may be imported.

    from scripts.utils.data_path import REGISTRY_REL, store_dirname   # everyone else
    <loaded by path>.store_dirname_from_files(data_root, engine_root)  # the overlay guard

The standard library ONLY, and that is the reason this module exists. The
overlay write guard (`scripts/utils/overlay_write_guard.py`) is armed from a
`.pth` during `site.py`, where importing anything under `scripts.` binds that
name for the whole interpreter and `import yaml` would load a third-party
package into every Python process on the machine. It still has to know the
runtime store's directory name, because its record lives inside the store. So
the one fact it needs is written here in a form it can read with a regular
expression, and `scripts/utils/data_path.py` refuses any registry whose YAML
answer differs from what this module reads off the same text. Two readers, one
spelling, and a registry the two would disagree about never loads.

Holds the registry's own address, `REGISTRY_REL`, which `data_path` re-exports:
the guard needs it too, and a second spelling of the one data address the
engine keeps as a literal is exactly what HOS-5 section 7 exists to prevent.

## The store key

`runtime_store: <directory name>` at the top level of the registry. Operator
decision 2026-09-25 ("yes, move it", translated), reversing his ruling of 2026-09-14 that
kept the name an engine constant. Absent from the tree's registry, the engine's
default file `scripts/data-names.example.yaml` answers, and it answers
`runtime-junk`, so a tree with no entry resolves exactly as it did before.

The value is a directory under the data root, relative to it: one segment or
several joined by `/`, each of lowercase letters, digits and hyphens, the
`names:` rule. Several since HOS-241 (2026-10-13), when the C3 move put the
store inside a subject root, two segments deep; which directories
it may then sit in is `data_path._parse_store`'s question, because only that
reader knows the roots and the `names:` addresses. It is written plain, never
quoted and never in flow style, because that is the form a line reader can
read exactly.
"""
from __future__ import annotations

import re
from pathlib import Path

#: THE constant. Relative to the data root, and the only data address the engine
#: holds as a literal after HOS-5: everything else is read out of the file it
#: names. Re-exported by `scripts/utils/data_path.py`.
REGISTRY_REL = "config/data-paths.yaml"  # leak-guard: ok (the registry's own address)

#: The engine's DEFAULT registry file, under the ENGINE root. An engine path,
#: not a data address.
EXAMPLE_NAMES_REL = ("scripts", "data-names.example.yaml")

#: The registry's top-level key naming the runtime store's directory.
STORE_KEY = "runtime_store"

#: What one segment of a store name may be: the `names:` spelling. A subject
#: root is one such segment too (`check_root_names`).
STORE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: A directory name the store may not take because the data root already means
#: something by it: the registry's own directory. The family roots and every
#: top-level directory a `names:` entry addresses are refused too, by
#: `data_path._parse_store`, which can read them; this reader cannot.
RESERVED_STORE_NAMES = frozenset({REGISTRY_REL.split("/", 1)[0]})

# Names `STORE_NAME_RE` admits that YAML 1.1 reads as something other than a
# string: a bool, null, an integer or a date. Refused HERE, by name, so the
# message says why; left to the parser they would arrive as `True` or `123`.
_YAML_TYPED = re.compile(
    r"^(?:yes|no|true|false|on|off|null|[0-9]+|0x[0-9a-f]+|0b[01]+"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2})$")

#: The guard's own record directory under the store, relative to it. Named here
#: because the ignore rules a relocated store needs include it: the record is
#: volume, not history, and it is written by a process that never asks
#: `runtime_root` whether the store may be written.
GUARD_RECORD_REL = ("overlay-guard", "logs")

#: The parsed-registry cache under the store, relative to it: `<service>/<kind>`.
#: Spelled here rather than asked of `runtime_dir` because the one reader that
#: needs it, `data_path`'s disk cache, must find it BEFORE the registry that
#: registers the service has been parsed; parsing is what the cache avoids. The
#: kind is `cache`, which `required_ignore_lines` already keeps out of git.
REGISTRY_CACHE_REL = ("data-path", "cache")

# A top-level key starts in column 0. Anything after the colon is captured whole
# so that a quoted, flow-style or commented-away value is SEEN and refused
# rather than silently read as something else.
_KEY_LINE = re.compile(rf"^{STORE_KEY}[ \t]*:(?P<rest>.*)$", re.MULTILINE)
_PLAIN_VALUE = re.compile(r"^[ \t]*(?P<value>\S+)[ \t]*(?:#.*)?$")


class StoreKeyError(ValueError):
    """The registry spells `runtime_store` in a form this reader cannot read exactly."""


def check_store_name(value: object, where: str) -> str:
    """`value` if it is a store name this engine accepts, else `StoreKeyError`.

    A root-relative directory: segments of the `names:` spelling joined by `/`,
    no empty segment, no leading or trailing slash, so `..` and an absolute
    path cannot be spelled at all.
    """
    if not isinstance(value, str) or not all(STORE_NAME_RE.match(seg)
                                             for seg in value.split("/")):
        raise StoreKeyError(
            f"{where}: `{STORE_KEY}: {value!r}` must be a directory under the data "
            f"root, segments of lowercase letters, digits and hyphens joined by `/`")
    if _YAML_TYPED.match(value):
        raise StoreKeyError(
            f"{where}: `{STORE_KEY}: {value}` is read by YAML as a boolean, null, "
            f"number or date, not a name; choose a name with a letter in it that "
            f"is not yes/no/on/off/true/false/null")
    if value.split("/", 1)[0] in RESERVED_STORE_NAMES:
        raise StoreKeyError(
            f"{where}: `{STORE_KEY}: {value}` names a directory the data root "
            f"already uses for something else")
    return value


def read_store_line(text: str, where: str) -> str | None:
    """The store name the registry TEXT spells at the top level, or None when it spells none.

    Refuses a second occurrence and any value that is not written plain. It
    does not know YAML and does not pretend to: `data_path._parse_document`
    parses the same text properly and refuses the file when the two answers
    differ, which is what makes this reader safe to rely on.
    """
    # A byte-order mark is not part of the first line's text for YAML either.
    found = _KEY_LINE.findall(text.lstrip("\ufeff"))
    if not found:
        return None
    if len(found) > 1:
        raise StoreKeyError(f"{where}: `{STORE_KEY}:` appears {len(found)} times")
    plain = _PLAIN_VALUE.match(found[0])
    if plain is None:
        raise StoreKeyError(f"{where}: `{STORE_KEY}:` carries no value on its own line")
    return check_store_name(plain.group("value"), where)


def store_dirname_from_files(data_root: Path, engine_root: Path) -> str:
    """The store's directory name for `data_root`, read with the standard library alone.

    The tree's registry answers when it spells the key; otherwise the engine's
    default file does. For the overlay guard only: every other caller asks
    `data_path.store_dirname`, which parses the YAML and checks this answer.
    Raises `StoreKeyError` or `OSError`; the caller decides what a refusal costs.
    """
    registry = Path(data_root) / REGISTRY_REL
    try:
        text = registry.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    value = read_store_line(text, str(registry))
    if value is not None:
        return value
    example = Path(engine_root).joinpath(*EXAMPLE_NAMES_REL)
    value = read_store_line(example.read_text(encoding="utf-8"), str(example))
    if value is None:
        raise StoreKeyError(f"{example}: the engine's default spells no `{STORE_KEY}:`")
    return value


# ---------------------------------------------------------------------------
# The subject roots
# ---------------------------------------------------------------------------
#
# `subject_roots: [<name>, <name>, ...]` at the top level of the registry: the
# top-level directories of the data repository's subject tree (HOS-234, operator
# decision 2026-10-13, "NO hardcoded paths", translated). Every guard asking "is this a
# data root" asks `families.subject_roots()`, which reads this line, so adding,
# renaming or dropping a root is an edit of this one line and nothing else.
#
# Read here, with a regular expression, for the reason the store key is: the
# readers are hooks that run on every tool call, and parsing the YAML costs them
# an import of PyYAML. One line in flow style, in column 0, once; the YAML parse
# of the same text in `data_path._parse_document` must agree with this reading
# or the registry does not load.

#: The registry's top-level key listing the subject-tree roots.
SUBJECT_ROOTS_KEY = "subject_roots"

_ROOTS_LINE = re.compile(rf"^{SUBJECT_ROOTS_KEY}[ \t]*:(?P<rest>.*)$", re.MULTILINE)
_FLOW_LIST = re.compile(r"^[ \t]*\[(?P<items>[^\[\]#]*)\][ \t]*(?:#.*)?$")


class RootsKeyError(ValueError):
    """The registry spells `subject_roots` in a form this reader cannot read exactly."""


def check_root_names(values: object, where: str, engine_root: Path | None = None) -> tuple[str, ...]:
    """`values` as a tuple if every one is a subject root this engine accepts, else `RootsKeyError`.

    A root is one directory name under the data root, spelled like a store
    name. It may not be the registry's own directory, may not repeat, and, when
    `engine_root` is given, may not name a top-level entry of the engine
    checkout: the redirect hook moves every write under a data root into the
    overlay, so a root that shadows an engine directory would carry engine
    writes out of the engine.
    """
    if not isinstance(values, (list, tuple)) or not values:
        raise RootsKeyError(f"{where}: `{SUBJECT_ROOTS_KEY}:` must be a non-empty list")
    seen: list[str] = []
    for value in values:
        if not isinstance(value, str) or not STORE_NAME_RE.match(value) or _YAML_TYPED.match(value):
            raise RootsKeyError(
                f"{where}: `{SUBJECT_ROOTS_KEY}:` entry {value!r} must be one directory "
                f"name of lowercase letters, digits and hyphens, not one YAML retypes")
        if value in RESERVED_STORE_NAMES:
            raise RootsKeyError(
                f"{where}: `{SUBJECT_ROOTS_KEY}:` entry {value!r} names the registry's "
                f"own directory")
        if value in seen:
            raise RootsKeyError(f"{where}: `{SUBJECT_ROOTS_KEY}:` names {value!r} twice")
        if engine_root is not None and (Path(engine_root) / value).exists():
            raise RootsKeyError(
                f"{where}: `{SUBJECT_ROOTS_KEY}:` entry {value!r} is a top-level entry of "
                f"the engine checkout {engine_root}; a data root of that name would "
                f"redirect engine writes into the data repository")
        seen.append(value)
    return tuple(seen)


def read_roots_line(text: str, where: str) -> tuple[str, ...] | None:
    """The subject roots the registry TEXT lists at the top level, or None when it lists none.

    Same contract as `read_store_line`: a second occurrence, a value spread over
    several lines, or anything that is not a one-line flow list is refused, and
    `data_path._parse_document` refuses a file the YAML reads differently.
    """
    found = _ROOTS_LINE.findall(text.lstrip("\ufeff"))
    if not found:
        return None
    if len(found) > 1:
        raise RootsKeyError(f"{where}: `{SUBJECT_ROOTS_KEY}:` appears {len(found)} times")
    flow = _FLOW_LIST.match(found[0])
    if flow is None:
        raise RootsKeyError(
            f"{where}: `{SUBJECT_ROOTS_KEY}:` must be one line in flow style, "
            f"`{SUBJECT_ROOTS_KEY}: [a, b, c]`")
    items = [item.strip() for item in flow.group("items").split(",")]
    if items and not items[-1]:
        items.pop()  # a trailing comma, which YAML also accepts
    return check_root_names(items, where)


def subject_roots_from_files(data_root: Path | None, engine_root: Path) -> tuple[str, ...]:
    """The subject roots for `data_root`, read with the standard library alone.

    The tree's registry answers when it lists the key; otherwise the engine's
    default file does, which lists the generic subject tree. `data_root` None
    means "this machine has no data root to ask", and the default answers. The
    default is the FAIL-CLOSED answer for every guard that asks: a public clone
    or a test root gets the full generic set as data roots, never an empty one.
    Raises `RootsKeyError` for a registry spelling the key wrongly, and for a
    root that names an engine directory; `OSError` only for the engine's own
    default file.
    """
    if data_root is not None:
        registry = Path(data_root) / REGISTRY_REL
        try:
            text = registry.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            text = ""
        value = read_roots_line(text, str(registry))
        if value is not None:
            return check_root_names(value, str(registry), engine_root)
    example = Path(engine_root).joinpath(*EXAMPLE_NAMES_REL)
    value = read_roots_line(example.read_text(encoding="utf-8"), str(example))
    if value is None:
        raise RootsKeyError(f"{example}: the engine's default lists no `{SUBJECT_ROOTS_KEY}:`")
    return check_root_names(value, str(example), engine_root)


def required_ignore_lines(store: str) -> tuple[str, ...]:
    """The `.gitignore` lines a store named `store` cannot do without, root-relative.

    The two rebuildable kinds, the two throwaway suffixes in every kind, and the
    overlay guard's record. What a fresh data repository ships under the default
    name (`scripts/create-data-repo.py`) is a superset of these.
    """
    return (f"{store}/*/cache/", f"{store}/*/tmp/", f"{store}/**/*.lock",
            f"{store}/**/*.tmp", "/".join((store,) + GUARD_RECORD_REL) + "/")


def ignore_problems_in(text: str, where: str, store: str, default: str) -> list[str]:
    """Why ignore-file TEXT does not cover a store named `store`; empty when it does.

    Two questions about the text, neither needing git: is each required line
    there under the new name (or the whole store ignored with `<store>/`), and
    does any rule still name the default store, which is the ignore set that did
    not follow. A negated line (`!...`) satisfies nothing: it un-ignores.
    """
    active = set()
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and not line.startswith("!"):
            active.add(line.lstrip("/"))
    problems = []
    if f"{store}/" not in active:
        problems = [f"{where} has no `{need}`" for need in required_ignore_lines(store)
                    if need not in active]
    if store != default:
        stale = sorted(line for line in active
                       if line == default or line.startswith(f"{default}/"))
        if stale:
            problems.append(
                f"{where} still names the default store `{default}/` in {len(stale)} "
                f"rule(s), which ignore nothing once the store is `{store}/`: "
                + ", ".join(stale))
    return problems


__all__ = [
    "EXAMPLE_NAMES_REL", "GUARD_RECORD_REL", "REGISTRY_CACHE_REL", "REGISTRY_REL",
    "RESERVED_STORE_NAMES", "SUBJECT_ROOTS_KEY", "RootsKeyError",
    "STORE_KEY", "STORE_NAME_RE", "StoreKeyError", "check_root_names", "check_store_name",
    "ignore_problems_in", "read_roots_line", "read_store_line", "required_ignore_lines",
    "store_dirname_from_files", "subject_roots_from_files",
]
