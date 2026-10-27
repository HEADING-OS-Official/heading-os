#!/usr/bin/env python3
"""Resolve the registry-named layers of `config/memory-index.yaml` into globs.

A layer of the content store used to spell its directory as a literal glob,
`{status/entity/}crm/contacts/*.md`. When the data repository moves a directory  # leak-guard: ok (history: the literal glob this module replaced)
(the C3 subject-tree move, 2026-10-13) the registry is repointed and the literal
is not, so the layer silently stops claiming the files and they leave /recall.
So a layer may instead NAME its directories and files:

    - layer: crm
      names: [crm-contacts]       # registry names of directories; an `{account}`
                                  # name is one directory per account
      groups: [context-homes]     # named sets of them, the registry's `groups:`
      glob: "*.md"                # relative to EACH named directory
      files: [people, pipeline]   # registry names of single files, taken as is
      file_groups: [context-files]
      literal: ["a/b/**/*.md"]    # root-relative globs for what no name covers

and this module turns that into the one data-root-relative `glob` every reader
already understands, `{<dir1>/*.md,<dir2>/*.md,<file>,a/b/**/*.md}`, by asking
`data_path()` at load time. Repointing a name in the registry is then the whole
change when its directory moves.

A layer without `names:` or `files:` is returned untouched, so the engine-side
layers (`skill`, `rule`) and the literal ones keep their meaning.

A name that does not resolve, or resolves outside the data root, is SAID on
stderr and dropped from that layer; the rest of the layer still resolves. A
layer left with nothing gets `glob: None`, which every reader skips. That is
loud rather than fatal on purpose: the per-prompt recall hook loads this config,
and one bad registry line must not take recall down with it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.data_homes import resolve, resolve_all
from scripts.utils.data_path import DataPathError, load_groups
from scripts.utils.workspace import get_data_root

NAMED_KEYS = ("names", "groups", "files", "file_groups", "literal")

# A resolved address carrying one of these would be read as glob syntax, or
# split the brace group this module emits.
_GLOB_SPECIAL = re.compile(r"[{},*?\[\]]")


def _expand(pattern: str) -> list[str]:
    """Brace expansion, innermost group first, so the output nests no braces."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    pre, post = pattern[: m.start()], pattern[m.end():]
    out: list[str] = []
    for option in m.group(1).split(","):
        out.extend(_expand(pre + option + post))
    return out


def is_named(layer: dict) -> bool:
    return isinstance(layer, dict) and any(layer.get(k) for k in NAMED_KEYS)


def _rels(name: str, root: Path, layer: str) -> list[str]:
    """`_rel` for every address of a name: one per account for an `{account}`
    name, existing directories only, else its one (HOS-234 part 10)."""
    from scripts.utils.accounts import PARAM, name_params
    try:
        per_account = name_params(name, root) == (PARAM,)
    except DataPathError as exc:  # said, then dropped, as `_rel` does
        sys.stderr.write(f"[memory-index] layer {layer!r}: registry name {name!r} "
                         f"unreadable ({exc}); that part of the layer claims nothing\n")
        return []
    if not per_account:
        one = _rel(name, root, layer)
        return [one] if one is not None else []
    out = []
    for path in resolve_all(name, root):
        if path.is_dir():
            rel = _rel(name, root, layer, path)
            if rel is not None:
                out.append(rel)
    return out


def _rel(name: str, root: Path, layer: str, path: Path | None = None) -> str | None:
    if path is None:
        path = resolve(name, root)   # says on stderr when the name has no address
    if path is None:
        return None
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError) as exc:  # said out loud, then dropped
        sys.stderr.write(f"[memory-index] layer {layer!r}: registry name {name!r} "
                         f"resolves outside the data root ({exc}); that part of "
                         f"the layer claims nothing\n")
        return None
    if _GLOB_SPECIAL.search(rel):
        sys.stderr.write(f"[memory-index] layer {layer!r}: registry name {name!r} resolves "
                         f"to {rel!r}, which holds glob syntax; that part of the layer "
                         f"claims nothing\n")
        return None
    return rel


def resolve_layer(layer: dict, root: Path) -> dict:
    """One layer with `names:`/`files:`/`literal:` folded into a root-relative `glob`."""
    if not is_named(layer):
        return layer
    label = layer.get("layer")
    dir_names = list(layer.get("names") or [])
    file_names = list(layer.get("files") or [])
    groups = load_groups(root) if (layer.get("groups") or layer.get("file_groups")) else {}
    for key, into in (("groups", dir_names), ("file_groups", file_names)):
        for group in layer.get(key) or []:
            if group in groups:
                into.extend(groups[group])
            else:
                sys.stderr.write(f"[memory-index] layer {label!r}: no name group "
                                 f"{group!r} in the data registry's `groups:`\n")
    tails = _expand(layer.get("glob") or "**/*.md") if dir_names else []
    options: list[str] = []
    for name in dir_names:
        for rel in _rels(name, root, label):
            options.extend(f"{rel}/{tail}" for tail in tails)
    for name in file_names:
        rel = _rel(name, root, label)
        if rel is not None:
            options.append(rel)
    for pattern in layer.get("literal") or []:
        options.extend(_expand(pattern))
    options = list(dict.fromkeys(options))
    resolved = {**layer, "glob_relative": layer.get("glob")}
    if not options:
        sys.stderr.write(f"[memory-index] layer {label!r}: no registry name resolved; "
                         f"the layer claims nothing this run\n")
        resolved["glob"] = None
    elif len(options) == 1:
        resolved["glob"] = options[0]
    else:
        resolved["glob"] = "{" + ",".join(options) + "}"
    return resolved


def resolve_layers(layers, data_root: Path | None = None) -> list:
    """Every layer resolved against `data_root` (this machine's by default)."""
    if not isinstance(layers, list) or not any(is_named(lc) for lc in layers):
        return layers
    root = Path(data_root) if data_root is not None else get_data_root()
    return [resolve_layer(lc, root) for lc in layers]

