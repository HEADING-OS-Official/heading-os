"""Resolve a brand asset by logical key, so the engine never spells its filename.

MEASURED 2026-09-02, by a scan that no longer runs: twenty-seven places in this
public repository quoted a filename that exists only in the private data
overlay. A logo, the Word master template, ten GT Standard faces, a product
document and four output examples, written out verbatim in renderers, marp
themes, prose and two test files. The operator's directive that day was given
in capitals and without qualification: everything in the DataStore, then the
top-level `datastore` directory, is private, and that includes the FILENAMES,
not only the contents.

The names were there for an honest reason. The code loads those files, so it had
to say which ones. This module is the seam that lets it stop: the engine asks for
a stable invented key (`logo_primary`, `word_master_template`,
`font_gt_l_medium_oblique`), and the map from key to real filename lives in the
private overlay under the registry name `brand-manifest`, where a real filename
belongs.

Three properties, each of which cost something to learn:

*Resolved at CALL time, never at import.* `data_path()` reads `HEADING_OS_DATA`
on every call, so it follows a caller that asks after the environment moved.
Frozen into a module-level constant it asks once, during its own import, and
stores the answer, so a test that imported the module and then repointed the
data root still read the operator's real overlay. The same reasoning, with the
same wording, guards `datastore_dir()` in `scripts/datastore-extract.py`; it is
not a style preference.

*Resolved against the DATA root, never `get_reference_dir()`.*
`get_reference_dir()` returns the ENGINE root, because `reference/` is engine
content. Reaching for it here would put the manifest path inside the public
repository, and calling it by mistake on 2026-09-02 wrote a private file
straight into the public tree. `data_path()` refuses an address outside the
data repository by construction, so that particular mistake can no longer be
made silently here; every address in this module goes through it.

*A missing manifest or a missing key RAISES, named.* There is deliberately no
`*.example.json` beside this module and no hardcoded default, which breaks the
`resolve_config_with_example` convention its neighbours in `config/` follow. The
convention is right for a roster or a schedule, where a plausible stand-in lets a
public clone run. It is wrong here: a stand-in filename resolves to a file that
does not exist, `_embed_asset` reports a miss and returns "", and the renderer
exits 0 having produced a complete, plausible, entirely unbranded document. A
public clone has no brand assets to load at all, so refusing loudly is the honest
answer and the error names both the key and the manifest path that would supply
it.

*The DataStore's `brand` subtree is resolved in ONE place, here.* Four modules
used to spell `get_datastore_dir() / "brand" / ...` themselves: this one, the
email/dashboard logo loader in `scripts/utils/image.py`, the generator-template
loader in `scripts/utils/html_templates.py`, and the locked-doctype renderer's
`_under_datastore`. `brand_dir` and `brand_file` below are that one derivation:
this module, `html_templates.py`, `doctype_renderer.py` and
`scripts/design-studio.py` call `brand_dir`; `image.py` no longer derives a
brand path at all and resolves its logo by the registry name
`email-signature-logo`.

Public API:
    BrandAssetError
    manifest_path() -> Path
    load_manifest() -> dict[str, str]
    brand_dir(rel) -> Path
    brand_file(rel) -> Path
    brand_asset_path(key, manifest=None) -> Path
    brand_asset_name(key, manifest=None) -> str
"""

from __future__ import annotations

import json
from pathlib import Path


class BrandAssetError(RuntimeError):
    """The manifest is absent, unreadable, malformed, or lacks the key asked for.

    A distinct type, not `FileNotFoundError` or `KeyError`: callers in this
    workspace routinely wrap asset work in `except OSError` or `except KeyError`
    for a genuinely missing file, and a configuration fault swallowed by one of
    those handlers reappears downstream as "brand asset not found", which sends
    the reader to the wrong question entirely.
    """


#: Why every `data_path` call below is wrapped, and why the NAME is spelled at
#: each one rather than passed into a shared helper.
#:
#: Wrapped: refusing on a tree that cannot answer is this module's design,
#: stated at the top of this file -- a public clone has no brand assets to load,
#: so a stand-in address would produce a complete, plausible, entirely unbranded
#: document. What the refusal must not do is arrive as a type nobody is
#: catching. `scripts/marp_render.py` catches `(BrandAssetError, DataRootError)`
#: and nothing wider, and `DataPathError` is a sibling of the second rather than
#: a subclass, so it would pass straight through that handler. MEASURED
#: 2026-09-22 against a data root holding neither the HEADING OS 2 family
#: manifest nor `config/data-paths.yaml`: `data_path("brand-manifest")` raises
#: `UnknownDataPath` and `data_path("record:asset/brand")` raises
#: `DataPathError`, where the pre-move address was answered before.
#:
#: Spelled at the call: `scripts/leak-guard.py` refuses a front-door call whose
#: address is not a literal, because no gate can say which data a variable
#: names. A `_address(name)` helper was written here first and the guard
#: BLOCKED it, by name, on 2026-09-22. Two short handlers are the price of a
#: gate that can still read this file.


def manifest_path() -> Path:
    """Where the key-to-filename map lives, resolved fresh on every call.

    The HEADING OS 2 move empties `config/` 100%: MEASURED 2026-09-16 against
    the rehearsal manifest, `config/brand-assets.json` carries the kind
    `build/config`, the same kind `_registry_path` in `scripts/utils/workspace.py`
    already routes the fleet and org-chart registries through. Without a
    resolver this returns a path that no longer holds a file, `load_manifest`
    reports a manifest it could not read, and every caller of `brand_asset_name`
    refuses: the renderers lose their logos and their faces, loudly rather than
    silently, which is this module's own design.

    MEASURED 2026-09-22 on the operator's overlay: the registry name and the
    migration seam it replaced resolve to the identical absolute path.
    """
    from scripts.utils.data_path import DataPathError, data_path

    try:
        return data_path("brand-manifest")
    except DataPathError as exc:
        raise BrandAssetError(
            f"the brand-asset manifest address could not be resolved: {exc}"
        ) from exc


def brand_dir(rel: str | Path) -> Path:
    """The DIRECTORY `<datastore>/<rel>` became after the HEADING OS 2 move.

    `rel` is datastore-relative and spelled as it was before the move
    (`brand/templates/generators`), because the mover's rule is that the
    destination carries the whole source subpath, so that spelling survives
    verbatim as the tail of the new address under the kind's home. The literal
    `datastore` segment below is the head of that same carried tail; it is where
    the records ARE, not a fallback to where they were.

    The old top-level `datastore` directory fans out to SEVEN kinds and so has
    no single destination to name. The `brand/` subtree does: MEASURED
    2026-09-16 against the migration rehearsal's per-file manifest, every record
    whose legacy path is under that directory's `brand` subtree carries exactly
    one kind, `asset/brand` — cutting the (leak-guard: ok, history)
    family and leaf columns of every such row and sorting them returned a single
    pair. That one kind is what the address below asks for, which is why this
    function can name an address at all while its parent directory cannot.

    The limit, stated rather than left to be found: a `rel` under `brand/` is
    asked of `brand-kit` and anything else of `datastore-brand`, so a `rel`
    that belongs to another DataStore home names a directory that does not
    exist. Route another datastore subtree through its OWN name; do not widen
    this one.

    The 2026-09-16 operator ruling that stood here is now satisfied by the
    address itself rather than deferred to a future caller. The seam this
    replaced fell back to the vacated legacy path when no `asset/brand` record (leak-guard: ok, history)
    matched, silently; the registry answers for the kind and nothing else, so a
    `rel` outside `brand/` yields a missing directory at a named address rather
    than a plausible one at the wrong address. MEASURED 2026-09-22 on the
    operator's overlay, `rel` of `brand/fonts` and of `brand/templates`: this
    expression and the seam it replaced resolve to the identical absolute path.
    """
    from scripts.utils.data_path import DataPathError, data_path

    # C3 (2026-10-13): a `rel` under `brand/` is asked of the registry name of
    # that subtree, `brand-kit`, so the subject-tree move's repoint of the name
    # is followed; anything else is asked of `datastore-brand` (HOS-234 part 11).
    parts = Path(rel).parts
    try:
        if parts and parts[0] == "brand":
            return data_path("brand-kit").joinpath(*parts[1:])
        home = data_path("datastore-brand")
    except DataPathError as exc:
        raise BrandAssetError(
            f"the brand-asset home could not be resolved: {exc}"
        ) from exc
    return home / rel


def brand_file(rel: str | Path) -> Path:
    """The FILE `<datastore>/<rel>` became after the move.

    One expression, shared with `brand_dir`, and it is the removal of the
    migration seam that made them one: the seam answered a FILE by trying the
    pre-move path first and a DIRECTORY by never trying it, so the two needed
    separate calls. An address carried whole under its kind's home is the same
    join either way. The two names stay because the callers' intent differs and
    the docstrings are where that intent is written down.

    Existence is not asserted; the caller decides what a miss means. Bounded to
    `brand/` for the reason in `brand_dir`, which is the docstring to read before
    adding a caller whose `rel` starts anywhere else.
    """
    return brand_dir(rel)


def load_manifest() -> dict[str, str]:
    """The manifest as a plain key-to-relative-path dict.

    Keys beginning with an underscore are documentation inside the JSON and are
    dropped here, so a comment block can never be mistaken for an asset.

    Callers that need several assets in one pass (the marp theme substitution
    needs ten) should call this once and hand the result to `brand_asset_name`,
    rather than re-reading the file per key. It is NOT cached: a cache keyed on
    nothing freezes the first data root this process happened to see, which is
    the exact defect the call-time rule above exists to prevent.
    """
    path = manifest_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrandAssetError(
            f"the brand-asset manifest could not be read from {path}: {exc}. "
            "It lives in the private data overlay and is absent on a public "
            "clone, which has no brand assets to load either. Refusing to guess "
            "a filename."
        ) from exc

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BrandAssetError(
            f"the brand-asset manifest at {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise BrandAssetError(
            f"the brand-asset manifest at {path} must be a JSON object mapping "
            f"a logical key to a datastore-relative path, not a "
            f"{type(data).__name__}"
        )

    return {
        key: value
        for key, value in data.items()
        if not key.startswith("_") and isinstance(value, str)
    }


def _relpath(key: str, manifest: dict[str, str] | None) -> str:
    entries = load_manifest() if manifest is None else manifest
    try:
        return entries[key]
    except KeyError as exc:
        raise BrandAssetError(
            f"no brand asset is registered under the key {key!r} in "
            f"{manifest_path()}. Known keys: "
            f"{', '.join(sorted(entries)) or '(none)'}. Add the key there with "
            "the real relative path; never put the filename back in the engine."
        ) from exc


def brand_asset_path(key: str, manifest: dict[str, str] | None = None) -> Path:
    """The absolute path of the asset registered under `key`.

    Existence is NOT checked here. Callers differ on what a missing file means:
    `_embed_asset` in the doctype renderer degrades and says so, the marp theme
    falls back to a system face, and `brand_master_template` refuses. Answering
    "where would it be" separately from "is it there" keeps those decisions with
    the caller that has to make them.

    Resolved through `brand_file`, so it answers on both sides of the HEADING OS
    2 move. MEASURED 2026-09-16, all 18 keys in the manifest name a path under
    `brand/`, which is the subtree `brand_file` is bounded to; a key registered
    outside it would resolve to the legacy path, per that function's stated limit.
    """
    return brand_file(_relpath(key, manifest))


def brand_asset_name(key: str, manifest: dict[str, str] | None = None) -> str:
    """Just the filename of the asset registered under `key`.

    Wanted by callers that hold their own copy of a file the datastore also
    carries. The marp themes are the case: the licensed web fonts are dropped
    into the themes' own `fonts/` directory (`themes/fonts/` under the registry
    name `skill-brand-assets`, `skill=marp`; `fonts_dir()` in `scripts/marp_render.py`)
    under the names the manifest records, so the theme needs the basename and not
    the datastore path.
    """
    return Path(_relpath(key, manifest)).name
