"""The service-host VM topology resolver, shared by the puller and the pulse probe.

Moved here verbatim from scripts/pull-service-state.py (HOS-355, 2026-11-12):
`service_config`, `vm_roots`, `_vm_path`, `_data_rel` and `state_dirs`, with
their docstrings. One resolver of service-host.json now serves both
scripts/pull-service-state.py and the service-host pulse probe.
The one change: this module sits one level deeper than the puller did, so the
example fallback is `parent.parent / "service-host.example.json"`.

`vm_state_path` is new: the VM path of ONE state_dirs entry, resolving that
entry alone, so an unrelated malformed entry never blanks a caller's line.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.utils.workspace import get_data_root, resolve_config_with_example
from scripts.utils.data_path import DataPathError, config_names, deprecated_config_path
from scripts.utils.paths import DataRootError

# Service-host topology comes from the private data overlay; the engine ships a
# generic example. Post engine/data split the VM carries TWO roots — the engine
# clone (vm_engine_root) and the data overlay (vm_data_root) — each overridable
# per-instance via .env (SERVICE_VM_ENGINE_ROOT / SERVICE_VM_DATA_ROOT). The host
# ADDRESS is always SERVICE_VM_HOST in .env.
def service_config() -> tuple[dict, str | None]:
    """Read service-host.json. Returns (config, error); NEVER raises.

    Resolved on CALL, never at import. `resolve_config_with_example` reaches the
    data root, which is read out of `HEADING_OS_DATA` on every call, so a
    module-level `_SVC, _SVC_ERROR = ...` stored one answer for the life of the
    process: a caller that repointed the root afterwards still got whatever the
    root was during this module's import.

    The pair is returned from ONE read so the config and its error can never
    come from two different loads. It never raises: this used to run at import,
    where no handler was in scope, and an unparseable config raised
    json.JSONDecodeError while a top-level JSON list raised AttributeError from
    the `.get` in the callers, both killing the run with a raw traceback before
    `main` -- and its named, actionable message -- existed. Carrying the error as
    a value lets `state_dirs` raise the ValueError that `main` already catches
    and prints properly.
    """
    try:
        path = resolve_config_with_example(
            "service-host.json",
            Path(__file__).resolve().parent.parent / "service-host.example.json",
        )
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError: a config saved as
        # UTF-16 would otherwise escape as a traceback.
        return {}, f"could not be read: {exc}"
    except Exception as exc:  # noqa: BLE001 - the docstring's promise is total
        # Locating the file runs first (`resolve_config_with_example` ->
        # `get_workspace_root()`), and a root that will not resolve raises
        # RuntimeError (MEASURED 2026-09-01). The reason is RETURNED, not
        # swallowed: `state_dirs` raises it as the ValueError `main` prints.
        return {}, f"could not be located: {type(exc).__name__}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"is not valid JSON ({path}): {exc}"
    if not isinstance(data, dict):
        return {}, f"must hold a JSON object, not a {type(data).__name__} ({path})"
    return data, None


def vm_roots() -> dict:
    """The two VM roots, .env overrides winning.

    This used to be a module-level dict. `load_env()` -- the thing that copies
    .env into os.environ -- runs inside main(), which is AFTER module import, so
    the overrides the docstring above promises were read before they existed and
    every run silently used the config-file values instead. Resolving on call is
    what makes the promise true.
    """
    svc, _ = service_config()
    return {
        "engine": os.environ.get("SERVICE_VM_ENGINE_ROOT") or svc.get("vm_engine_root", ""),
        "data": os.environ.get("SERVICE_VM_DATA_ROOT") or svc.get("vm_data_root", ""),
    }


def _vm_path(entry, roots: dict) -> tuple[str, str]:
    """Resolve one state_dirs entry to (mirror_name, absolute VM path).

    New 3-tuple form [name, root, rel] joins `rel` onto the named VM root
    (engine|data). Falls back to the retired 2-tuple [name, rel] form by joining
    onto the engine root, so an un-migrated config still resolves.

    A malformed entry used to raise ValueError from the tuple unpack, and an
    empty root produced the path "/rel" -- scp then pulled from the VM's root
    filesystem. Both are refused by name now.
    """
    if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
        raise ValueError(f"malformed state_dirs entry (want 2 or 3 items): {entry!r}")
    name, root, rel = entry if len(entry) == 3 else (entry[0], "engine", entry[1])
    # Types first, because everything below indexes and joins these. A
    # non-string element raised AttributeError out of `lstrip`/`rstrip`,
    # and `main` catches only ValueError, so the run ended in a traceback
    # rather than the named one-line reason this function promises.
    if not all(isinstance(v, str) for v in (name, root, rel)):
        raise ValueError(
            f"state_dirs entry must hold strings only (name, root, rel): "
            f"{entry!r}")
    # The mirror name is joined onto the mirror directory and the previous
    # copy is rmtree'd, so a PATH here deletes outside the mirror.
    # MEASURED 2026-08-29: `..`, `.` and a single-segment absolute name all
    # came back accepted. `..` deletes the mirror's parent wholesale, `.`
    # deletes the mirror, and `/tmp` deletes /tmp.
    if name != Path(name).name or name in ("", ".", ".."):
        raise ValueError(
            f"state_dirs mirror name {name!r} must be a plain directory "
            f"name: it is joined onto the mirror and the previous copy is "
            f"deleted, so a path here deletes outside the mirror")
    base = roots.get(root, "")
    if not isinstance(base, str):
        raise ValueError(
            f"VM root {root!r} must be a string, not a "
            f"{type(base).__name__}")
    if not base:
        raise ValueError(
            f"state_dirs entry {name!r} needs VM root {root!r}, which is empty; "
            f"set SERVICE_VM_{root.upper()}_ROOT in .env or vm_{root}_root in "
            f"service-host.json")
    if root == "data":
        rel = _data_rel(name, rel)
    return name, f"{base.rstrip('/')}/{rel.lstrip('/')}"


def _data_rel(name: str, value: str) -> str:
    """The data-relative path a `data` entry's third item names. HOS-234 part 5.

    A registry name resolves through `config_names` against THIS machine's data
    root, and the relative part is joined onto the VM's data root. That is the
    same address on the VM because the VM's data root is a pull-only clone of
    this data repository, registry included (`config/data-paths.yaml` is
    tracked), and the VM's own writers resolve the same name against it: the
    team bot writes its own state through `data_path()`. It holds while the VM's
    clone is at the registry commit this machine has; a pull brings it there.
    A path is still joined as written and said on stderr as deprecated. A name
    the registry does not place, or a group, is refused: one entry is one
    mirrored directory.
    """
    field = f"service-host.json state_dirs entry {name!r}"
    try:
        named = config_names(value, field=field)
        if named is None:
            deprecated_config_path(field, value)
            return value
        if len(named) != 1:
            raise ValueError(f"{field}: {value!r} names {len(named)} addresses; "
                             f"an entry mirrors one directory")
        return named[0][1].relative_to(get_data_root()).as_posix()
    except (DataPathError, DataRootError) as exc:
        raise ValueError(str(exc)) from exc


def state_dirs() -> list:
    """(local mirror name, VM absolute path) pairs. Call AFTER load_env().

    Every other key in this file is read with `.get` and a default; `state_dirs`
    was the one subscript, so a config missing it raised KeyError -- which is not
    a ValueError, so `main`'s handler could not catch it and the run ended in a
    traceback instead of the one-line reason.
    """
    svc, error = service_config()
    if error:
        raise ValueError(error)
    entries = svc.get("state_dirs")
    if entries is None:
        raise ValueError(
            "no 'state_dirs' key, so there is nothing to pull; add a `state_dirs` "
            "list; the entry shape is in scripts/service-host.example.json's "
            "`_comment`")
    if not isinstance(entries, list):
        raise ValueError(
            f"'state_dirs' must be a list of entries, not a {type(entries).__name__}")
    roots = vm_roots()
    return [_vm_path(e, roots) for e in entries]


def vm_state_path(mirror_name: str) -> str:
    """The absolute VM path of the state_dirs entry named `mirror_name`. Call AFTER load_env().

    Only the matching entry is resolved: an entry that is not a non-empty list
    or tuple is skipped, and the others are never passed to `_vm_path`, so a
    malformed sibling cannot turn one caller's answer into an error. Raises
    ValueError when the config cannot be read, has no state_dirs list, has no
    entry of that name, or the entry itself does not resolve.
    """
    svc, error = service_config()
    if error:
        raise ValueError(error)
    entries = svc.get("state_dirs")
    if not isinstance(entries, list):
        raise ValueError("service-host.json has no state_dirs list")
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        if entry[0] == mirror_name:
            return _vm_path(entry, vm_roots())[1]
    raise ValueError(f"service-host.json state_dirs has no {mirror_name!r} entry")
