#!/usr/bin/env python3
"""MARP rendering shim for HEADING OS.

Wraps marp-cli with brand theme injection, hidden-character sanitization,
workspace-aware defaults, frontmatter management, and watch-mode lifecycle.

Usage:
    python scripts/marp_render.py render <source.md> [--pdf-only] [--html-only] [--images png] [--output <dir>] [--verbose]
    python scripts/marp_render.py from <workspace-path.md> [--break-at h2|h3|manual] [--mode dark|light|mixed] [--title "..."] [--subtitle "..."] [--no-auto-cover] [--no-auto-closing] [--output <dir>] [--paginate-heavy] [--verbose]
    python scripts/marp_render.py watch <source.md>
    python scripts/marp_render.py watch stop
    python scripts/marp_render.py watch status
    python scripts/marp_render.py --self-test

Coverage: none live since the 2026-09-21 test-suite split. `--self-test` above
is the only check that still runs against this file, and a person has to run it.

Where data sits: `default_output_dir()` writes decks to the registry name
`presentations`; `workspace_defaults()` is a table of PREFIX KEYS over a source
document's path (never a write target), its post-move keys asked of the registry
by name at call time. The theme, font and skill paths are ENGINE paths.
"""

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.data_path import DataPathError, data_path, data_write_dir
from scripts.utils.accounts import addresses as account_addresses
from scripts.utils.paths import DataRootError
from scripts.utils.workspace import (
    get_data_root,
    get_default_tz,
    get_workspace_root,
)
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET
from scripts.utils.markdown import parse_frontmatter as _parse_frontmatter_text
from scripts.utils.operator_identity import company_brand
from scripts.utils.pid_liveness import pid_runs_program
from scripts.utils.slugs import stable_suffix, transliterate

# ============================================================
# Configuration
# ============================================================

# --- Constants ---

WORKSPACE_ROOT = get_workspace_root()
# The themes, their fonts directory and the sample deck are brand content, so
# since HOS-131 (2026-09-25) they live in the data repository, at the registry
# name `skill-brand-assets` for `marp`, and never in the engine. Each is
# asked for by a function below, so importing this module resolves nothing, and
# each answers None on a clone with no data repository.
#
# Selectable theme variants: every `themes/<name>.css.tmpl` in the brand half is
# the variant `<name>`, and the Marp theme name its CSS declares is the same name.
# The default is the alphabetically first; a variant whose name ends in
# `-investor` is a brand-forward investor master that lays out with inline HTML.
# THEME_NAME is set in main() from --theme-variant, and prepare_theme() and
# inject_frontmatter() read it through selected_theme().
THEME_NAME = ""
INVESTOR_SUFFIX = "-investor"


def theme_variants() -> list[str]:
    """The theme variant names the brand half carries, sorted; empty with none."""
    base = brand_skill_dir()
    themes = base / "themes" if base is not None else None
    if themes is None or not themes.is_dir():
        return []
    return sorted(p.name[:-len(".css.tmpl")] for p in themes.glob("*.css.tmpl"))


def selected_theme() -> str:
    """The variant main() selected, else the default; empty with no brand half."""
    return THEME_NAME or next(iter(theme_variants()), "")


def brand_skill_dir() -> Path | None:
    """The marp skill's brand half in the data repository, or None with none."""
    try:
        return data_path("skill-brand-assets", skill="marp")
    except DataPathError:
        return None


def theme_template() -> Path | None:
    """The selected theme template, or None with no data repository."""
    base = brand_skill_dir()
    name = selected_theme()
    if base is None or not name:
        return None
    return base / "themes" / f"{name}.css.tmpl"


def fonts_dir() -> Path | None:
    """Where the licensed theme faces are dropped, or None with no data repository."""
    base = brand_skill_dir()
    return base / "themes" / "fonts" if base is not None else None


def sample_deck() -> Path | None:
    """The deck `--self-test` renders, or None with no data repository."""
    base = brand_skill_dir()
    return base / "examples" / "sample-deck.md" if base is not None else None


VERSION_PIN_FILE = WORKSPACE_ROOT / "scripts" / ".marp-version"


def _watch_state_file() -> Path:
    """The running watch's pid/url record, in the Workspace's runtime store.

    It was `~/.marp/watch.json` until HOS-5 § 7 (2026-09-25): a record this
    engine writes, kept outside the Workspace. A function rather than a
    module constant so importing this module resolves nothing.
    """
    return data_path("runtime:scratch/tmp") / "marp-watch.json"


# Everything reading the watch state can hit. All three readers listed only
# `(json.JSONDecodeError, KeyError)`, and the state file is the residue of a
# killed watch process, so a half-written or wrongly-encoded one is the ordinary
# case. `UnicodeDecodeError` is a ValueError and a SIBLING of JSONDecodeError,
# not a subclass, so `read_text(encoding="utf-8")` over a file saved as UTF-16
# went straight past the handler. MEASURED 2026-09-01: a watch.json written as
# UTF-16 raised UnicodeDecodeError out of `watch_status()`, which is documented
# to REPORT status and answers "Corrupt watch state file." for every other
# unreadable shape. OSError joins it because a directory, a permission change or
# a vanished home all reach the same read.
_WATCH_STATE_UNREADABLE = (json.JSONDecodeError, KeyError, OSError,
                           UnicodeDecodeError)
WORD_OVERFLOW_THRESHOLD = 150
# The company in branded output comes from operator.yaml (`company_brand()`);
# a clone with none configured prints no company at all.
_CO_NAME, _CO_SHORT, _CO_SITE = company_brand()
DEFAULT_FOOTER = " - ".join(["(C) 2025-2026", *(p for p in (_CO_NAME, _CO_SITE) if p),
                             "Proprietary & Confidential"])
_CO_SUFFIX = f" - {_CO_NAME}" if _CO_NAME else ""

# Workspace-aware defaults for /marp from. These are comparison keys tested with
# `str.startswith` against a document's path relative to one of the two roots;
# nothing is ever joined to them. Two sources, read in this order; the LONGEST
# matching key wins (HOS-234 part 9: the C3 move nests intelligence reports
# inside `knowledge/`), and an equal-length tie goes to the earlier key:
#
#   * LEGACY_WORKSPACE_DEFAULTS: the pre-move spellings, so a document still at
#     its old address matches as it always did, and `reference/` in the ENGINE.
#   * `named_workspace_defaults`: the data-repository homes, by registry name. The
#     address lives in the data repository's `config/data-paths.yaml`, so a
#     folder move there moves the key with no engine change.
#
# The themes, named once, so a registry-named home and its legacy key share one.
THEME_CONTEXT = {"mode": "light", "subtitle": "Operating Context" + _CO_SUFFIX}
THEME_KNOWLEDGE = {"mode": "light", "subtitle": "From the brain - {date}"}
THEME_REFERENCE = {"mode": "light", "subtitle": "Reference" + _CO_SUFFIX}
THEME_INTEL = {"mode": "dark", "subtitle": "Intelligence - Classified"}
THEME_OPERATIONS = {"mode": "dark", "subtitle": "Operations" + _CO_SUFFIX}
THEME_PROPOSAL = {"mode": "mixed", "subtitle": "{filename} - Proposal"}

LEGACY_WORKSPACE_DEFAULTS = {
    "context/": THEME_CONTEXT,  # leak-guard: ok (workspace-path prefix-match key)
    "knowledge/": THEME_KNOWLEDGE,  # leak-guard: ok (workspace-path prefix-match key)
    "reference/": THEME_REFERENCE,  # leak-guard: ok (workspace-path prefix-match key)
    "outputs/intel/": THEME_INTEL,  # leak-guard: ok (workspace-path prefix-match key)
    "outputs/operations/": THEME_OPERATIONS,  # leak-guard: ok (workspace-path prefix-match key)
    "outputs/deliverables/proposals/": THEME_PROPOSAL,  # leak-guard: ok (workspace-path prefix-match key)
}

def named_workspace_defaults(data_root: Path) -> dict:
    """The data-repository homes as prefix keys, each by its registry name.

    A name the registry refuses is named on stderr and left out, never
    swallowed: the table then answers for the rest, as `workspace_relative`
    does for a missing data root.
    """
    homes = (
        ("knowledge-base", THEME_KNOWLEDGE,
         lambda: data_path("knowledge-base", data_root=data_root)),
        ("context-matter", THEME_CONTEXT,
         lambda: data_path("context-matter", data_root=data_root)),
        ("context-entity", THEME_CONTEXT,
         lambda: data_path("context-entity", data_root=data_root)),
        ("reference-docs", THEME_REFERENCE,
         lambda: data_path("reference-docs", data_root=data_root)),
        ("proposals", THEME_PROPOSAL,
         lambda: data_path("proposals", data_root=data_root)),
        ("intel-outputs", THEME_INTEL,
         lambda: data_path("intel-outputs", data_root=data_root)),
        # The umbrella the C3 move makes INSIDE `knowledge-base` (HOS-234 part
        # 9); the longest key wins in `get_workspace_defaults`, so a report
        # there stays dark rather than taking the brain's theme.
        ("intelligence", THEME_INTEL,
         lambda: data_path("intelligence", data_root=data_root)),
        ("operations-handoff", THEME_OPERATIONS,
         lambda: data_path("operations-handoff", data_root=data_root)),
        ("operations-reports", THEME_OPERATIONS,
         lambda: data_path("operations-reports", data_root=data_root)),
        ("operations-scratch", THEME_OPERATIONS,
         lambda: data_path("operations-scratch", data_root=data_root)),
    )
    table = {}
    for name, defaults, resolve in homes:
        try:
            rel = resolve().relative_to(data_root).as_posix()
        except (DataPathError, ValueError) as exc:
            print(f"marp: registry name {name!r} gives no prefix under the data "
                  f"root ({exc}); its documents get the plain defaults", file=sys.stderr)
            continue
        table.setdefault(rel + "/", defaults)
    # HOS-234: the C3 move spreads proposals, intel and operations records over
    # one directory per account, so each account's home of those kinds is a
    # prefix too, or a deck moved there loses its theme.
    for name, defaults in (("account-proposals", THEME_PROPOSAL),
                           ("account-research", THEME_INTEL),
                           ("account-reports", THEME_OPERATIONS),
                           ("account-work", THEME_OPERATIONS)):
        try:
            homes_of = account_addresses(name, data_root=data_root)
        except DataPathError as exc:
            print(f"marp: registry name {name!r} gives no account homes ({exc}); "
                  f"its documents get the plain defaults", file=sys.stderr)
            continue
        for home in homes_of:
            try:
                rel = home.relative_to(data_root).as_posix()
            except ValueError as exc:
                print(f"marp: {name!r} home {home} is outside the data root ({exc})",
                      file=sys.stderr)
                continue
            table.setdefault(rel + "/", defaults)
    return table


def workspace_defaults() -> dict:
    """The live prefix table: legacy keys, then registry names."""
    table = dict(LEGACY_WORKSPACE_DEFAULTS)
    try:
        data_root = get_data_root()
    except DataRootError as exc:
        print(f"marp: cannot resolve the data root ({exc}); workspace defaults "
              f"will only match the legacy keys", file=sys.stderr)
    else:
        for key, defaults in named_workspace_defaults(data_root).items():
            table.setdefault(key, defaults)
    return table


def default_output_dir() -> Path:
    """Where a rendered deck goes: the registry name `presentations`.

    Resolved at call time, never at import, so it follows `HEADING_OS_DATA` for
    a caller that asks after the environment moved. No catch here, deliberately:
    this is a WRITE, and a write that cannot resolve its name must refuse
    rather than invent a directory nobody reads. `render()` creates the
    directory itself before writing.
    """
    return data_path("presentations")


# ============================================================
# Marp CLI Detection
# ============================================================


def get_pinned_version() -> str:
    """Read the pinned marp-cli version string."""
    if VERSION_PIN_FILE.exists():
        return VERSION_PIN_FILE.read_text(encoding="utf-8").strip()
    return "@marp-team/marp-cli@4.4.0"


def _resolve_marp_bin() -> str | None:
    """Resolve the marp executable path. On Windows, npm installs .cmd wrappers
    that subprocess can't find without shell=True. shutil.which handles this."""
    return shutil.which("marp")


def check_marp_installed() -> tuple[bool, str]:
    """Check if marp-cli is installed and return (ok, version_string)."""
    marp_bin = _resolve_marp_bin()
    if not marp_bin:
        return False, ""
    try:
        result = subprocess.run(
            [marp_bin, "--version"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            version = result.stdout.strip().split("\n")[0]
            return True, version
        return False, ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""


# The first version-looking token in `marp --version` output belongs to
# marp-cli itself; anything after it names a bundled package.
_VERSION_TOKEN_RE = re.compile(r"\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?")


def check_version_match(installed_version: str) -> bool:
    """Check if installed version matches pinned version.

    Compares the FIRST version token exactly. The test was
    `pinned_num in installed_version`, and a substring match is not a version
    match. MEASURED 2026-08-29 on this machine, where the pin reads
    `@marp-team/marp-cli@4.4.0` and marp prints
    `@marp-team/marp-cli v4.5.0 (w/ @marp-team/marp-core v4.4.0)`: the pin
    matched the bundled marp-core, so `render` and `--self-test` both reported
    the version as matching while a different marp-cli did the rendering. Same
    hole for `14.4.0` and `4.4.0-beta.1`, which also answered True.

    An unparsable version string answers False, so an unrecognised format warns
    rather than claims a match it never established.
    """
    pinned = get_pinned_version()
    # Extract version number from pin (e.g., "@marp-team/marp-cli@4.1.1" -> "4.1.1")
    pinned_num = pinned.rsplit("@", 1)[-1] if "@" in pinned else pinned
    m = _VERSION_TOKEN_RE.search(installed_version)
    if not m:
        return False
    return m.group(0) == pinned_num


# ============================================================
# Browser & Theme Setup
# ============================================================


def _puppeteer_cache_root() -> Path:
    """Where `@puppeteer/browsers` keeps the browsers it installs.

    `PUPPETEER_CACHE_DIR` is puppeteer's own documented override and is honoured
    first; otherwise the documented default, `~/.cache/puppeteer`. Derived from
    the environment and `Path.home()` and never from a literal, because this is
    the public engine repo and an operator's home directory carries their
    username.

    A separate function so a test can move the seam: the real cache exists on
    the machine this was written on, and a probe test that could not point
    somewhere else would be asserting facts about the developer's disk.
    """
    configured = os.environ.get("PUPPETEER_CACHE_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".cache" / "puppeteer"


def _version_key(name: str) -> tuple:
    """Sort key for a puppeteer install directory such as `linux-153.0.8010.36`.

    Numeric per component, so 153 sorts above 99 where a string sort would not.
    An unparseable component sorts lowest rather than raising: the directory
    layout is someone else's and a naming change must cost us the ordering, not
    the browser.
    """
    tail = name.rsplit("-", 1)[-1]
    parts = []
    for chunk in tail.split("."):
        parts.append(int(chunk) if chunk.isdigit() else -1)
    return tuple(parts)


# Where the executable sits under `<cache>/chrome/<platform>-<version>/`, per
# platform. A glob that matches nothing is the normal answer on a machine whose
# layout differs, and costs one stat.
_PUPPETEER_EXE_GLOBS = (
    "chrome-linux64/chrome",
    "chrome-win64/chrome.exe",
    "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
)


def puppeteer_browser() -> str | None:
    """The newest Chrome that puppeteer installed for itself, if any.

    WHY THIS IS FIRST and not a fallback. marp-cli drives puppeteer-core, and a
    browser installed by `@puppeteer/browsers` is the exact build that
    puppeteer-core expects to speak CDP to. Every other candidate is a browser
    that happens to be on the machine for other reasons and is only
    probably-compatible. Preferring the vendor's own binary over an arbitrary
    system one is the ordinary reading of "which browser should the tool use",
    not a special case for this laptop.

    MEASURED 2026-09-09 on this host, one deck through `marp --pdf`:
      /snap/bin/chromium                      -> no PDF (ProcessSingleton)
      /usr/bin/chromium-browser               -> no PDF (a shell shim that
                                                 execs the same snap)
      /usr/bin/brave-browser                  -> no PDF (launch, Code: 0)
      <puppeteer cache>/.../chrome-linux64/chrome -> 5382-byte PDF
    """
    root = _puppeteer_cache_root() / "chrome"
    if not root.is_dir():
        return None
    try:
        installs = sorted((d for d in root.iterdir() if d.is_dir()),
                          key=lambda d: _version_key(d.name), reverse=True)
    except OSError:
        return None
    for install in installs:
        for rel in _PUPPETEER_EXE_GLOBS:
            exe = install / rel
            if exe.is_file() and os.access(exe, os.X_OK):
                return str(exe)
    return None


def _is_snap_shim(path: str) -> bool:
    """Is this 'browser' actually the snap launcher, or a redirect to it?

    Used to DE-PRIORITISE, never to reject, and the distinction is the whole
    design. Rejecting by path shape would refuse a working browser on a host
    where the snap is unconfined, and this function cannot tell confined from
    unconfined. Ordering costs nothing when the snap works (it is still
    returned, just last) and fixes the case where it does not.

    Two shapes, both MEASURED 2026-09-09 on this host:
      * `/snap/bin/chromium` is a symlink whose real path is `/usr/bin/snap`,
        so the executable is the launcher itself, not a browser.
      * `/usr/bin/chromium-browser` is a 20-line `/bin/sh` script that checks
        for `/snap/bin/chromium` and execs it. Nothing about its PATH location
        says snap; only its contents do.
    """
    try:
        if Path(os.path.realpath(path)).name == "snap":
            return True
    except OSError:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if head[:2] != b"#!":
        return False          # a real ELF binary; not a shim
    return b"/snap/bin/" in head


def probe_browser() -> str | None:
    """Detect a browser for PDF rendering. Returns path or None.

    Order, and the reason for it: the browser puppeteer installed for itself,
    then Chromium-family browsers on PATH, then anything that turns out to be a
    snap wrapper. Nothing is rejected; a candidate can only be moved later.

    This used to return the FIRST `shutil.which` hit and ask nothing else. On a
    host where `chromium` resolves to the snap wrapper that won over every
    working browser further down the list, and the render then failed with an
    error the caller mislabelled `chromium-missing`.
    """
    system = platform.system()

    from_puppeteer = puppeteer_browser()
    if from_puppeteer:
        return from_puppeteer

    if system == "Windows":
        candidates = [
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    else:
        # Linux: check PATH using shutil.which (avoids subprocess for portability).
        # Marp uses Puppeteer under the hood, which requires a Chromium-family
        # browser (Chrome DevTools Protocol). Firefox does NOT work here.
        # Candidate order: Google's own packages first, then the distro
        # chromium package names, then brave and edge. All of them are looked
        # up on PATH by `shutil.which`; no snap-specific path is probed, and a
        # snap only shows up here if its wrapper is on PATH under one of these
        # names. The comment used to claim distro-first and snap paths, and was
        # wrong about both the order and the mechanism.
        deferred = []
        for name in [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",  # Debian/Ubuntu package name
            "brave-browser",
            "brave-browser-stable",
            "brave",
            "microsoft-edge",
            "microsoft-edge-stable",
        ]:
            path = shutil.which(name)
            if not path:
                continue
            # A snap wrapper is not refused, only queued behind every real
            # browser. See `_is_snap_shim`: this cannot tell a confined snap
            # from a working one, so refusing would break the hosts where it
            # works, while ordering costs them nothing.
            if _is_snap_shim(path):
                deferred.append(path)
            else:
                return path
        return deferred[0] if deferred else None

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


MARP_TIMEOUT_S = 120


# Evidence that the browser is NOT THERE, as opposed to there and failing: an
# allowlist of puppeteer's own vocabulary for a binary it cannot locate or run,
# because a catch-all on "browser" reported every launch failure as a missing
# download (MEASURED 2026-09-09). Anything else keeps its stderr verbatim under
# `render-failed`. "no such file or directory" is deliberately absent: a present
# but crashing Brave prints it too.
_BROWSER_ABSENT_MARKERS = (
    "could not find",                  # "Could not find Chrome (ver. 1.2.3)"
    "browser was not found",
    "browser is not found",
    "no browser found",
    "puppeteer browsers install",      # puppeteer's own remedy sentence
    "is not executable",
    "unable to find",
)


def browser_error_is_absence(err_msg: str) -> bool:
    """Does this stderr EVIDENCE a missing browser, rather than mention one?

    Public because it is the seam the tests drive: the classification is the
    defect, so it is asserted directly on real captured stderr rather than
    inferred from a render result.
    """
    lowered = err_msg.lower()
    return any(marker in lowered for marker in _BROWSER_ABSENT_MARKERS)


def _run_marp(cmd: list):
    """Run one marp-cli invocation. Returns the CompletedProcess, or None on timeout.

    Every other failure in `render` becomes a structured `{"ok": False, ...}`
    result, but `subprocess.run(..., timeout=MARP_TIMEOUT_S)` RAISES on a hang,
    which would otherwise kill the CLI with a traceback.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=MARP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None


def theme_missing_result() -> dict:
    """The structured result for an unreadable theme template.

    `prepare_theme` reads the theme template and raises `OSError` when it is
    absent, and that exception used to walk straight out of `render`,
    `transform_workspace_md` and `watch_start`. MEASURED 2026-08-29 by pointing
    the template at a path that does not exist: `render` died with a raw
    `FileNotFoundError` traceback instead of the `{"ok": False, ...}` every
    other failure in this file returns. The template can genuinely be absent on
    a partial skill install, when `--theme-variant` names a variant whose
    file was never added, and, since HOS-131, on any clone with no data
    repository, which is where the themes now live.
    """
    template = theme_template()
    if template is None:
        return {"ok": False, "error": "theme-missing",
                "message": "No theme template: this clone has no data repository, "
                           "and the marp themes live there (themes/ under the "
                           "registry name skill-brand-assets, skill=marp)."}
    return {"ok": False, "error": "theme-missing",
            "message": f"Theme template not readable: {template}. "
                       f"Check the data repository, or the --theme-variant name."}


# A theme template names its faces as `{FONT_<KEY>}`, resolved against the
# private brand manifest by lowercasing the token into `font_<key>`. One
# mechanism, no per-template table: a new face is a placeholder in the CSS and an
# entry in the manifest, and nothing in this file has to learn about it.
_FONT_TOKEN_RE = re.compile(r"\{FONT_([A-Z0-9_]+)\}")

# What a font placeholder becomes when the manifest cannot answer. It is not a
# real filename and is not meant to resolve: the `src: url(...)` names a file
# that is not there, so Chromium falls back to a system sans. That is EXACTLY
# the state a clone without the licensed faces is in, and
# `themes/fonts/README.md` under the registry name `skill-brand-assets`
# (`skill=marp`) in the data repository, beside where the faces are dropped, documents the fallback as
# normal. Refusing to render instead would take a working deck away
# from a clone that never had the fonts.
_UNRESOLVED_FONT = "unresolved-brand-face.woff2"


def substitute_theme_fonts(template_text: str) -> str:
    """Replace every `{FONT_<KEY>}` in a theme template with a real filename.

    The filenames were written into the two `.css.tmpl` themes until 2026-09-02,
    when the operator ruled that a datastore filename is itself private and this
    repository is public. `scripts/utils/brand_assets.py` carries the reasoning;
    this is the substitution end of it, extending the `{FONTS_DIR}` mechanism
    that was already here rather than inventing a second one.

    Degrades rather than raises, and says so once on stderr. See `_UNRESOLVED_FONT`.
    """
    from scripts.utils.brand_assets import BrandAssetError, brand_asset_name

    # `DataRootError` is deliberately NOT an OSError (scripts/utils/paths.py), so
    # a set-but-missing `HEADING_OS_DATA` would otherwise walk out of here as a
    # traceback and kill a render that used to succeed. `workspace_relative` in
    # this same file already catches it and says so; this matches that, because
    # resolving a font NAME is a read and cannot write anything anywhere wrong.
    try:
        manifest = load_brand_manifest()
    except (BrandAssetError, DataRootError) as exc:
        manifest = None
        print(f"{GRAY}marp: {exc} Rendering with system fallback faces.{RESET}",
              file=sys.stderr)

    missing: list[str] = []

    def resolve(match: re.Match) -> str:
        key = f"font_{match.group(1).lower()}"
        if manifest is None:
            return _UNRESOLVED_FONT
        try:
            return brand_asset_name(key, manifest)
        except BrandAssetError:
            missing.append(key)
            return _UNRESOLVED_FONT

    resolved = _FONT_TOKEN_RE.sub(resolve, template_text)
    if missing:
        print(f"{GRAY}marp: the brand manifest has no entry for "
              f"{', '.join(sorted(set(missing)))}; those faces fall back to a "
              f"system sans.{RESET}", file=sys.stderr)
    return resolved


def load_brand_manifest() -> dict:
    """Read the brand manifest once per render.

    A thin seam of its own so a test can drive `substitute_theme_fonts` against a
    scratch manifest without a data overlay, and so the read happens once rather
    than once per placeholder.
    """
    from scripts.utils.brand_assets import load_manifest

    return load_manifest()


def prepare_theme() -> Path:
    """Substitute {FONTS_DIR} and the font names in the theme template.

    Writes the result to a temp file and returns its path. Raises OSError when
    the template is missing; callers turn that into `theme_missing_result()`.
    """
    template = theme_template()
    if template is None:
        # An OSError, because that is what every caller already turns into
        # `theme_missing_result()`, which names this case itself.
        raise FileNotFoundError("no data repository, so no marp theme template")
    template_text = template.read_text(encoding="utf-8")
    # Path.as_uri() properly percent-encodes spaces and special characters
    # (e.g., a space becomes "%20", a parenthesis becomes "%28"), which is
    # critical since marp-cli renders via Chromium which needs valid file:// URIs.
    # `fonts_dir()` is not None here: it shares `theme_template()`'s root.
    fonts_uri = fonts_dir().resolve().as_uri()
    resolved = template_text.replace("{FONTS_DIR}", fonts_uri)
    resolved = substitute_theme_fonts(resolved)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".css", prefix="marp-theme-",
        dir=data_write_dir("runtime:scratch/tmp"),
        delete=False, encoding="utf-8"
    )
    tmp.write(resolved)
    tmp.close()
    return Path(tmp.name)


# ============================================================
# Text Processing & Frontmatter
# ============================================================


def run_sanitizer(text: str) -> tuple[str, int]:
    """Run hidden-character sanitization in-memory. Returns (clean_text, count_found)."""
    # Inline detection of common hidden Unicode characters
    hidden_chars = {
        "\u200b": "ZERO WIDTH SPACE",
        "\u200c": "ZERO WIDTH NON-JOINER",
        "\u200d": "ZERO WIDTH JOINER",
        "\u00ad": "SOFT HYPHEN",
        "\u00a0": "NO-BREAK SPACE",
        "\u200e": "LEFT-TO-RIGHT MARK",
        "\u200f": "RIGHT-TO-LEFT MARK",
        "\u2060": "WORD JOINER",
        "\ufeff": "BOM",
        "\u2061": "FUNCTION APPLICATION",
        "\u2062": "INVISIBLE TIMES",
        "\u2063": "INVISIBLE SEPARATOR",
        "\u2064": "INVISIBLE PLUS",
        "\u180e": "MONGOLIAN VOWEL SEPARATOR",
        "\u202a": "LEFT-TO-RIGHT EMBEDDING",
        "\u202b": "RIGHT-TO-LEFT EMBEDDING",
        "\u202c": "POP DIRECTIONAL FORMATTING",
        "\u202d": "LEFT-TO-RIGHT OVERRIDE",
        "\u202e": "RIGHT-TO-LEFT OVERRIDE",
    }
    # A NO-BREAK SPACE is a SPACE. Deleting it joined the words around it, so
    # `10 pages` came out `10pages` -- silently, in rendered deck text, from a
    # character Option+Space types on macOS. Every other entry above is
    # zero-width and correctly disappears.
    replacements = {"\u00a0": " "}
    count = 0
    clean = text
    for char in hidden_chars:
        found = clean.count(char)
        if found > 0:
            count += found
            clean = clean.replace(char, replacements.get(char, ""))
    return clean, count


def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """Parse YAML frontmatter from markdown text. Returns (metadata, body).

    Thin wrapper around ``scripts.utils.markdown.parse_frontmatter`` that
    returns ``None`` (instead of an empty dict) when no frontmatter is
    present and ``.strip()``s the body, matching the legacy contract that
    ``inject_frontmatter`` and ``paginate_heavy`` callers depend on.

    The shared util uses ``yaml.safe_load`` (when PyYAML is installed) so
    booleans, lists, and quoted strings parse natively - the local bool
    coercion is no longer needed.
    """
    if not text.startswith("---"):
        return None, text
    data, body = _parse_frontmatter_text(text)
    if not data:
        return None, text
    return data, body.strip()


def yaml_scalar(value) -> str:
    """Render one frontmatter value as a YAML scalar.

    One emitter for both frontmatter writers (`inject_frontmatter` and
    `paginate_heavy`), which carried the same three-branch f-string. Values
    merged in from an existing deck come from `yaml.safe_load`, so a key with
    no value arrives as `None`, and `f"{key}: {value}"` wrote `reviewed_by:
    None`. MEASURED 2026-08-29 on a deck whose frontmatter reads
    `reviewed_by:`: the round trip through `inject_frontmatter` turned a YAML
    null into the four-letter STRING "None", silently, on every render.
    """
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "null"
    if isinstance(value, str) and (" " in value or ":" in value):
        # json.dumps, not an f-string. A double-quoted YAML scalar uses the
        # same escaping as JSON, so this handles an embedded `"` or newline,
        # which the bare interpolation did not: a title of Says "hello"
        # emitted `title: "Says "hello""`, and marp then failed to parse the
        # frontmatter or rendered garbled metadata.
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def inject_frontmatter(source_text: str, title: str = "", mode: str = "dark",
                       subtitle: str = "", classification: str = "ceo-only") -> str:
    """Inject MARP frontmatter if missing, or merge with existing."""
    existing_fm, body = parse_frontmatter(source_text)

    fm = {
        "marp": True,
        "theme": selected_theme(),
        "paginate": True,
        "size": "16:9",
        "class": mode,
        "title": title or "Untitled",
        "author": "Misha Hanin",
        "date": datetime.now(get_default_tz()).strftime("%Y-%m-%d"),
        "classification": classification,
        "footer": DEFAULT_FOOTER,
    }

    if existing_fm:
        # Preserve existing values, only fill missing
        for key, value in fm.items():
            if key not in existing_fm:
                existing_fm[key] = value
        fm = existing_fm

    if title:
        fm["title"] = title
    if subtitle:
        fm["subtitle"] = subtitle

    # Build frontmatter string
    lines = ["---"]
    for key, value in fm.items():
        lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    lines.append("")

    return "\n".join(lines) + "\n" + body


def strip_wiki_links(text: str) -> str:
    """Convert [[id]] to id and [[id|Display]] to Display."""
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    return text


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def fence_mask(lines: list) -> list:
    """One bool per line: True when that line sits INSIDE a fenced code block.

    The fence delimiters themselves count as inside, so nothing is ever
    inserted next to one.

    Every splitter and scanner in this file used to be blind to fences, and a
    deck ABOUT markdown is the ordinary case here. A `## Example` inside a
    ```markdown fence got a `---` slide break pushed in front of it, splitting
    the fence across two slides; a literal `---` inside a fence made
    `auto_slide_breaks` decide manual breaks already existed and do nothing at
    all, and made `check_overflow` and `paginate_heavy` count one slide as two.

    The fence LENGTH is tracked alongside its character, because CommonMark
    (and Marpit) close a fence only on a run at least as long as the opener,
    and a four-backtick outer fence wrapping a three-backtick sample is the
    canonical way to show a code block inside a deck about markdown. MEASURED
    2026-08-29 on that exact deck: the mask went False at the inner three
    backticks, `auto_slide_breaks` read the `## Example` after it as a real H2,
    and pushed a `---` slide break into the middle of the outer fence.
    """
    mask = []
    fence = None  # (character, length) of the open fence
    for line in lines:
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)))
            mask.append(m is not None)
            continue
        mask.append(True)
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
            fence = None
    return mask


def split_slides(body: str) -> list:
    """Split on standalone `---` lines that are NOT inside a code fence."""
    lines = body.split("\n")
    mask = fence_mask(lines)
    slides, current = [], []
    for line, inside in zip(lines, mask, strict=True):
        if not inside and line.strip() == "---":
            slides.append("\n".join(current))
            current = []
            continue
        current.append(line)
    slides.append("\n".join(current))
    return slides


def auto_slide_breaks(body: str, break_at: str = "h2") -> str:
    """Insert slide breaks at heading level if no manual breaks exist.
    If the body already contains standalone '---' lines (slide breaks),
    respect them and return unchanged. A `---` inside a code fence is content,
    not a break, and does not count as a manual one."""
    lines = body.split("\n")
    mask = fence_mask(lines)

    # Check for existing manual slide breaks (standalone --- lines)
    for line, inside in zip(lines, mask, strict=True):
        if not inside and line.strip() == "---":
            return body

    heading_pattern = {"h2": r"^## ", "h3": r"^### "}
    pattern = heading_pattern.get(break_at, r"^## ")

    result = []
    for i, (line, inside) in enumerate(zip(lines, mask, strict=True)):
        if i > 0 and not inside and re.match(pattern, line):
            result.append("")
            result.append("---")
            result.append("")
        result.append(line)
    return "\n".join(result)


# ============================================================
# Workspace Defaults & Slide Helpers
# ============================================================


def workspace_relative(source_path: Path) -> str:
    """The path as this workspace names it, resolved against EITHER root.

    Four of the five legacy prefixes in `workspace_defaults()`, every one but
    `reference/`, exist only in the
    private DATA overlay on the two-part topology, never in the engine clone.
    Resolving against `WORKSPACE_ROOT` alone therefore made the whole table
    dead: `relative_to` raised, `rel` became an absolute path, no prefix could
    match it, and every real `/marp from` fell through to "mixed" with the bare
    filename as its subtitle. Measured 2026-08-26 on this workspace, where the
    engine clone holds none of the four.

    `reference/` is the one key present in both, and it is read from whichever
    root the caller's own file is under -- the first match wins, engine first.
    """
    resolved = source_path.resolve()
    roots = [WORKSPACE_ROOT]
    try:
        data_root = get_data_root()
    except DataRootError as exc:
        # Named, never swallowed: a misconfigured overlay silently halving the
        # lookup is the failure this function exists to end.
        print(f"marp: cannot resolve the data root ({exc}); "
              f"workspace defaults will only match the engine tree", file=sys.stderr)
    else:
        if data_root != WORKSPACE_ROOT:
            roots.append(data_root)

    for root in roots:
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(source_path)


def get_workspace_defaults(source_path: Path) -> dict:
    """Determine mode and subtitle defaults based on source directory."""
    rel = workspace_relative(source_path)

    matches = [(prefix, defaults) for prefix, defaults in workspace_defaults().items()
               if rel.startswith(prefix)]
    if matches:
        # max() keeps the first of equal length, so table order breaks a tie.
        _, defaults = max(matches, key=lambda m: len(m[0]))
        subtitle = defaults["subtitle"]
        subtitle = subtitle.replace("{date}", datetime.now(get_default_tz()).strftime("%Y-%m-%d"))
        subtitle = subtitle.replace("{filename}", source_path.stem)
        return {"mode": defaults["mode"], "subtitle": subtitle}

    return {"mode": "mixed", "subtitle": source_path.stem}


def generate_slug(topic: str) -> str:
    """URL-safe slug from a topic, with a Cyrillic title kept rather than erased.

    Without the transliteration pass this returned '' for any Cyrillic input, so
    the deck stem `<short>-{title}-{date}` collapsed to `<short>--27-Aug-2026` and every
    Russian-titled deck rendered that day shared one name. Measured 2026-08-27:
    `generate_slug('Стратегия развития') == ''`.
    """
    slug = transliterate(topic).lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug and topic.strip():
        return stable_suffix(topic)
    return slug[:60]


def check_overflow(source_text: str) -> list[dict]:
    """Check for slides exceeding word threshold. Returns list of warnings."""
    _, body = parse_frontmatter(source_text)
    slides = split_slides(body)
    warnings = []
    for i, slide in enumerate(slides):
        words = len(slide.split())
        if words > WORD_OVERFLOW_THRESHOLD:
            warnings.append({"slide": i + 1, "words": words})
    return warnings


def fence_aware_paragraphs(slide: str) -> list:
    """Split a slide on blank lines that are NOT inside a code fence.

    `split_slides` was taught about fences; this second splitter was not, and
    `re.split(r"\\n\\n+", slide)` chops on any blank line. A blank line inside a
    fenced code block is legal and ordinary. MEASURED 2026-08-29 on a 181-word
    slide holding one ```python block with a blank line in it: `paginate_heavy`
    split on that blank line and produced two slides carrying one fence
    delimiter each, so the opener never closed and the rest of the deck
    rendered as code.
    """
    lines = slide.split("\n")
    mask = fence_mask(lines)
    paragraphs, current = [], []
    for line, inside in zip(lines, mask, strict=True):
        if not inside and not line.strip():
            if current:
                paragraphs.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append("\n".join(current))
    return paragraphs


def paginate_heavy(source_text: str) -> str:
    """Sub-break heavy slides on paragraph boundaries."""
    fm, body = parse_frontmatter(source_text)
    slides = split_slides(body)
    new_slides = []
    for slide in slides:
        words = len(slide.split())
        if words > WORD_OVERFLOW_THRESHOLD:
            paragraphs = fence_aware_paragraphs(slide)
            if len(paragraphs) > 1:
                current = []
                current_words = 0
                for para in paragraphs:
                    para_words = len(para.split())
                    if current_words + para_words > WORD_OVERFLOW_THRESHOLD and current:
                        new_slides.append("\n\n".join(current))
                        current = [para]
                        current_words = para_words
                    else:
                        current.append(para)
                        current_words += para_words
                if current:
                    new_slides.append("\n\n".join(current))
            else:
                new_slides.append(slide)
        else:
            new_slides.append(slide)

    # Reconstruct with frontmatter
    if fm is not None:
        fm_lines = ["---"]
        for key, value in fm.items():
            fm_lines.append(f"{key}: {yaml_scalar(value)}")
        fm_lines.append("---")
        return "\n".join(fm_lines) + "\n\n" + "\n\n---\n\n".join(new_slides)
    return "\n\n---\n\n".join(new_slides)


# ============================================================
# Rendering / Core Logic
# ============================================================


def render(source: Path, output_dir: Path = None, pdf_only: bool = False,
           html_only: bool = False, images_png: bool = False,
           auto_sanitize: bool = True, verbose: bool = False,
           output_stem: str = None) -> dict:
    """Render a MARP markdown file to PDF and/or HTML. Returns result dict.
    output_stem overrides the filename stem (without extension) for output files."""
    if not source.exists():
        return {"ok": False, "error": "source-not-found",
                "message": f"Source not found: {source}. Check the path and try again."}

    # `--pdf-only --html-only` skips the PDF branch AND the HTML branch, and
    # neither branch records an error when it is skipped. MEASURED 2026-08-29:
    # the call answered {"ok": True, "outputs": [], "errors": []}, so the CLI
    # printed "Render successful" and exited 0 having written no file at all.
    # A scripted pipeline reading that exit code ships nothing and says it won.
    if pdf_only and html_only:
        return {"ok": False, "error": "conflicting-format-flags",
                "message": "--pdf-only and --html-only exclude each other; "
                           "passing both would render nothing. Pass one, or neither."}

    # Check marp-cli
    installed, version_str = check_marp_installed()
    if not installed:
        pinned = get_pinned_version()
        return {"ok": False, "error": "marp-not-installed",
                "message": f"marp-cli not installed. Run: npm install -g {pinned}"}

    if not check_version_match(version_str):
        pinned = get_pinned_version()
        pinned_num = pinned.rsplit("@", 1)[-1]
        print(f"{YELLOW}Warning: marp-cli version {version_str} differs from pinned {pinned_num}. "
              f"Renders may differ. Continuing.{RESET}")

    # Read source
    source_text = source.read_text(encoding="utf-8")

    # Sanitize
    clean_text, hidden_count = run_sanitizer(source_text)
    if hidden_count > 0:
        if not auto_sanitize:
            sanitized_path = source.with_suffix(".sanitized.md")
            sanitized_path.write_text(clean_text, encoding="utf-8")
            return {"ok": False, "error": "hidden-chars-detected",
                    "message": f"Hidden characters detected ({hidden_count}). "
                               f"Sanitized copy written: {sanitized_path}. "
                               f"Re-run with --auto-sanitize to render."}
        print(f"{YELLOW}Hidden characters: {hidden_count} found, rendering from sanitized copy.{RESET}")
        source_text = clean_text

    # Prepare theme
    try:
        theme_path = prepare_theme()
    except OSError:
        return theme_missing_result()

    # THE THEME IS LIVE FROM HERE, so the next three steps are guarded. The
    # `finally` that unlinks it used to start below them, at the `try` around
    # the render itself, and each of the three can raise: the source copy
    # (a read-only source directory), `mkdir` (a bad output path), and the
    # `stem` line's own inputs. Any of them left a resolved theme CSS in the
    # shared temp directory with nothing holding a reference to it.
    # `tmp_source_path` is created inside this block and removed by the block
    # below once it exists, so it is bound to None first: an exception before
    # the assignment must not turn into a NameError in the handler.
    tmp_source_path = None
    try:
        # Write temp source (never mutate original). Place it next to the
        # original so relative image paths (e.g. `![bg](_assets/foo.png)`)
        # resolve from the same directory as the source MD.
        tmp_source = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix=f".{source.stem}.marp-src-",
            dir=str(source.parent),
            delete=False, encoding="utf-8"
        )
        tmp_source.write(source_text)
        tmp_source.close()
        tmp_source_path = Path(tmp_source.name)

        out_dir = output_dir or source.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = output_stem or source.stem
    except BaseException:
        # Re-raised, never swallowed: the caller's contract is unchanged and
        # this clause exists only so the scratch does not outlive the failure.
        theme_path.unlink(missing_ok=True)
        if tmp_source_path is not None:
            tmp_source_path.unlink(missing_ok=True)
        raise

    outputs = []
    errors = []

    try:
        # Probe browser
        browser_path = probe_browser()

        # Build base command
        marp_bin = _resolve_marp_bin()
        base_cmd = [
            marp_bin,
            str(tmp_source_path),
            "--theme", str(theme_path),
            "--allow-local-files",
        ]
        # The investor master expresses multi-element grid layouts (stat rows, feature
        # cards, comparison panels) with inline HTML, so enable HTML for that variant
        # only. Internal decks keep HTML disabled (default) for safety.
        if selected_theme().endswith(INVESTOR_SUFFIX):
            base_cmd.append("--html")
        if browser_path:
            base_cmd.extend(["--browser-path", browser_path])

        # Render PDF
        if not html_only:
            pdf_out = out_dir / f"{stem}.pdf"
            cmd = base_cmd + ["--pdf", "-o", str(pdf_out)]
            if verbose:
                print(f"{GRAY}Running: {' '.join(cmd)}{RESET}")
            result = _run_marp(cmd)
            if result is None:
                errors.append({"type": "pdf", "error": "timeout",
                               "message": f"marp-cli did not finish within "
                                          f"{MARP_TIMEOUT_S}s."})
            elif result.returncode == 0 and pdf_out.exists():
                outputs.append({"type": "pdf", "path": str(pdf_out), "size": pdf_out.stat().st_size})
            else:
                err_msg = result.stderr.strip() if result.stderr else "Unknown error"
                if browser_error_is_absence(err_msg):
                    # The message reports what was established and no more: the
                    # browser could not be found. It no longer asserts that a
                    # download is under way, because nothing here starts one and
                    # nothing here can observe one. The real stderr rides along,
                    # which is what was missing outside `verbose`.
                    errors.append({"type": "pdf", "error": "chromium-missing",
                                   "message": "PDF export needs a Chromium-family browser "
                                              "and marp-cli could not find one. Install one, "
                                              "or run: npx @puppeteer/browsers install "
                                              "chrome@stable\nmarp-cli output:\n" + err_msg})
                else:
                    errors.append({"type": "pdf", "error": "render-failed",
                                   "message": f"Render failed. marp-cli output:\n{err_msg}"})
                    if verbose:
                        print(f"{RED}STDERR: {result.stderr}{RESET}")
                        print(f"{GRAY}STDOUT: {result.stdout}{RESET}")

        # Render HTML (output format determined by -o extension, not a flag)
        if not pdf_only:
            html_out = out_dir / f"{stem}.html"
            cmd = base_cmd + ["-o", str(html_out)]
            if verbose:
                print(f"{GRAY}Running: {' '.join(cmd)}{RESET}")
            result = _run_marp(cmd)
            if result is None:
                errors.append({"type": "html", "error": "timeout",
                               "message": f"marp-cli did not finish within {MARP_TIMEOUT_S}s."})
            elif result.returncode == 0 and html_out.exists():
                outputs.append({"type": "html", "path": str(html_out), "size": html_out.stat().st_size})
                # Deep design verdict on the deck we just rendered. Slides are
                # read on a screen, so the screen profile applies. Reports only:
                # a design finding must never cost a rendered deck.
                try:
                    from scripts.utils import impeccable_engine
                    impeccable_engine.report_for_artifact(html_out, profile="screen")
                except ImportError:
                    pass
            else:
                err_msg = result.stderr.strip() if result.stderr else "Unknown error"
                errors.append({"type": "html", "error": "render-failed",
                               "message": f"Render failed. marp-cli output:\n{err_msg}"})

        # Render PNG images
        if images_png:
            # Fingerprint what is already on disk under this stem. The glob
            # after the run matches every `{stem}.*.png` in the directory, not
            # what this invocation wrote, and output directories are reused.
            # MEASURED 2026-08-29 with a stub marp that exits 0 and writes
            # nothing: a `deck.004.png` left by an earlier run was reported as
            # a fresh output of this one, and its presence satisfied the
            # no-output check below, which exists to catch exactly that.
            # Size rides along with mtime so a same-second rewrite of a
            # different length still reads as new.
            before = {}
            for png in out_dir.glob(f"{stem}.*.png"):
                st = png.stat()
                before[png] = (st.st_mtime_ns, st.st_size)
            cmd = base_cmd + ["--images", "png", "-o", str(out_dir / f"{stem}.png")]
            if verbose:
                print(f"{GRAY}Running: {' '.join(cmd)}{RESET}")
            result = _run_marp(cmd)
            if result is None:
                errors.append({"type": "png", "error": "timeout",
                               "message": f"marp-cli did not finish within {MARP_TIMEOUT_S}s."})
            elif result.returncode == 0:
                # Count the PNGs THIS run wrote: new files, plus ones whose
                # fingerprint moved.
                pngs = []
                for png in sorted(out_dir.glob(f"{stem}.*.png")):
                    st = png.stat()
                    if before.get(png) != (st.st_mtime_ns, st.st_size):
                        pngs.append(png)
                for png in pngs:
                    outputs.append({"type": "png", "path": str(png), "size": png.stat().st_size})
                # A zero exit with no files is still a failure. The PDF and HTML
                # paths both record their failures; this one recorded none, so
                # "Render successful" printed with the requested PNGs simply
                # absent and `ok` stayed True.
                if not pngs:
                    errors.append({"type": "png", "error": "no-output",
                                   "message": f"marp-cli exited 0 but wrote no new "
                                              f"{stem}.*.png in {out_dir}."})
            else:
                err_msg = result.stderr.strip() if result.stderr else "Unknown error"
                errors.append({"type": "png", "error": "render-failed",
                               "message": f"Render failed. marp-cli output:\n{err_msg}"})

        # Check overflow
        overflow_warnings = check_overflow(source_text)

    finally:
        # Clean up temp files
        tmp_source_path.unlink(missing_ok=True)
        theme_path.unlink(missing_ok=True)

    sanitizer_status = "clean" if hidden_count == 0 else f"{hidden_count} found, rendered from sanitized copy"

    return {
        "ok": len(errors) == 0,
        "outputs": outputs,
        "errors": errors,
        "overflow_warnings": overflow_warnings,
        "hidden_characters": sanitizer_status,
        "marp_version": version_str,
    }


# ============================================================
# Workspace Markdown Transform
# ============================================================


def transform_workspace_md(source: Path, break_at: str = "h2", mode: str = None,
                           title: str = None, subtitle: str = None,
                           no_auto_cover: bool = False, no_auto_closing: bool = False,
                           do_paginate_heavy: bool = False, output_dir: Path = None,
                           verbose: bool = False) -> dict:
    """Transform a workspace markdown doc into MARP slides without modifying source."""
    if not source.exists():
        return {"ok": False, "error": "source-not-found",
                "message": f"Source not found: {source}"}

    source_text = source.read_text(encoding="utf-8")

    # Parse existing frontmatter
    existing_fm, body = parse_frontmatter(source_text)

    # Determine title
    doc_title = title
    if not doc_title and existing_fm and "title" in existing_fm:
        doc_title = existing_fm["title"]
    if not doc_title:
        # Use first H1
        h1_match = re.search(r"^# (.+)$", body, re.MULTILINE)
        if h1_match:
            doc_title = h1_match.group(1).strip()
    if not doc_title:
        doc_title = source.stem

    # Workspace-aware defaults
    ws_defaults = get_workspace_defaults(source)
    slide_mode = mode or ws_defaults["mode"]
    slide_subtitle = subtitle or ws_defaults["subtitle"]

    # Strip wiki-links
    body = strip_wiki_links(body)

    # Insert slide breaks
    if break_at != "manual":
        body = auto_slide_breaks(body, break_at)

    # Build slides
    slides = []

    # Auto cover slide
    if not no_auto_cover:
        try:
            rel_path = source.resolve().relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:
            rel_path = source.name
        cover_mode = "dark" if slide_mode in ("dark", "mixed") else "light"
        cover = (
            f'<!-- _class: "title {cover_mode}" -->\n'
            f"<!-- _paginate: false -->\n"
            f'<!-- _footer: "" -->\n\n'
            f"# {doc_title}\n"
            f"## {slide_subtitle}\n\n"
            f"Source: {rel_path}\n"
            f"{datetime.now(get_default_tz()).strftime('%Y-%m-%d')}"
        )
        slides.append(cover)

    # Content slides
    slides.append(body)

    # Auto closing slide
    if not no_auto_closing:
        close_mode = "dark" if slide_mode in ("dark", "mixed") else "light"
        closing = (
            f'<!-- _class: "closing {close_mode}" -->\n'
            f"<!-- _paginate: false -->\n"
            f'<!-- _footer: "" -->\n\n'
            f"# End\n\n"
            f"{_CO_NAME}"
        ).rstrip()
        slides.append(closing)

    # Assemble full source
    full_body = "\n\n---\n\n".join(slides)

    # Pass `subtitle=` so the frontmatter carries it, not just the cover slide (MEASURED 2026-08-29).
    full_source = inject_frontmatter(full_body, title=doc_title, mode=slide_mode,
                                     subtitle=slide_subtitle,
                                     classification="ceo-only")

    # Paginate heavy if requested
    if do_paginate_heavy:
        full_source = paginate_heavy(full_source)

    # Write to temp file and render
    # `dir=source.parent`, as in `render`: relative image paths resolve from the
    # directory of the file being rendered.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix=f".{source.stem}.marp-from-",
        dir=str(source.parent),
        delete=False, encoding="utf-8"
    )
    tmp.write(full_source)
    tmp.close()
    tmp_path = Path(tmp.name)

    try:
        # Build a meaningful output stem from title and date
        date_str = datetime.now(get_default_tz()).strftime("%d-%b-%Y")
        title_slug = generate_slug(doc_title)
        meaningful_stem = f"{_CO_SHORT or 'deck'}-{title_slug}-{date_str}"

        result = render(tmp_path, output_dir=output_dir or default_output_dir(),
                        verbose=verbose, output_stem=meaningful_stem)
        result["source_title"] = doc_title
        result["source_mode"] = slide_mode
    finally:
        tmp_path.unlink(missing_ok=True)

    return result


# ============================================================
# Watch Mode
# ============================================================


def watch_start(source: Path) -> dict:
    """Start marp-cli watch mode for live preview."""
    if not source.exists():
        return {"ok": False, "error": "source-not-found",
                "message": f"Source not found: {source}"}

    # Check for existing watch
    if _watch_state_file().exists():
        try:
            state = json.loads(_watch_state_file().read_text(encoding="utf-8"))
            pid = state.get("pid")
            if pid and _is_process_running(pid):
                return {"ok": False, "error": "watch-active",
                        "message": f"Watch already active (PID {pid}, source: {state.get('source_path')}). "
                                   f"Run '/marp watch stop' first."}
        except _WATCH_STATE_UNREADABLE:
            pass

    # Check marp-cli
    marp_bin = _resolve_marp_bin()
    if not marp_bin:
        pinned = get_pinned_version()
        return {"ok": False, "error": "marp-not-installed",
                "message": f"marp-cli not installed. Run: npm install -g {pinned}"}

    # Prepare theme
    try:
        theme_path = prepare_theme()
    except OSError:
        return theme_missing_result()

    # Build command
    cmd = [
        marp_bin,
        str(source),
        "--theme", str(theme_path),
        "--allow-local-files",
        "--watch",
        "--server",
    ]

    browser_path = probe_browser()
    if browser_path:
        cmd.extend(["--browser-path", browser_path])

    # Start detached process
    if platform.system() == "Windows":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        proc = subprocess.Popen(
            cmd,
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # Write state file
    watch_state = data_write_dir("runtime:scratch/tmp") / "marp-watch.json"
    state = {
        "pid": proc.pid,
        "url": "http://localhost:8080",
        "source_path": str(source),
        "theme_path": str(theme_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    watch_state.write_text(json.dumps(state, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "pid": proc.pid,
        "url": "http://localhost:8080",
        "source_path": str(source),
        "message": f"Watch started (PID {proc.pid}). Preview at http://localhost:8080",
    }


def watch_stop() -> dict:
    """Stop active watch mode."""
    if not _watch_state_file().exists():
        return {"ok": False, "message": "No active watch session."}

    try:
        state = json.loads(_watch_state_file().read_text(encoding="utf-8"))
    except _WATCH_STATE_UNREADABLE:
        _watch_state_file().unlink(missing_ok=True)
        return {"ok": False, "message": "Corrupt watch state file removed."}

    pid = state.get("pid")
    theme_path = state.get("theme_path")

    # Liveness is checked BEFORE signalling. The recorded PID belongs to a marp
    # process that may have died long ago, and PIDs are reused: signalling one
    # blind meant sending SIGTERM to whatever unrelated process now holds that
    # number. `_is_process_running` is not proof of identity -- only that the
    # number is live -- so a stale state file whose process is gone now cleans
    # up and says so instead of firing into the dark.
    signalled = False
    if pid and _is_process_running(pid):
        signalled = True
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                signalled = False

    # Clean up theme temp file
    if theme_path:
        Path(theme_path).unlink(missing_ok=True)

    _watch_state_file().unlink(missing_ok=True)

    if not signalled:
        return {"ok": True, "signalled": False,
                "message": f"No live process for PID {pid}; stale watch state removed."}
    return {"ok": True, "signalled": True, "message": f"Watch stopped (PID {pid})."}


def watch_status() -> dict:
    """Report watch mode status."""
    if not _watch_state_file().exists():
        return {"running": False, "message": "No active watch session."}

    try:
        state = json.loads(_watch_state_file().read_text(encoding="utf-8"))
    except _WATCH_STATE_UNREADABLE:
        return {"running": False, "message": "Corrupt watch state file."}

    pid = state.get("pid")
    running = _is_process_running(pid) if pid else False

    if not running:
        _watch_state_file().unlink(missing_ok=True)
        if state.get("theme_path"):
            Path(state["theme_path"]).unlink(missing_ok=True)
        return {"running": False, "message": "Watch process no longer running. State cleaned up."}

    started = state.get("started_at", "unknown")
    return {
        "running": True,
        "pid": pid,
        "url": state.get("url", "http://localhost:8080"),
        "source_path": state.get("source_path"),
        "started_at": started,
    }


def _is_process_running(pid: int) -> bool:
    """Check if a process is still running.

    On Windows the PID is read out of `tasklist`'s CSV, field by field. It used
    to be `str(pid) in result.stdout` -- a substring search over the whole
    output, so PID 808 also matched 8080 and 18080. A dead watch then reported
    as running and `watch_start` refused to start with "watch-active".
    """
    if platform.system() == "Windows":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10
        )
        import csv
        import io
        return any(len(row) >= 2 and row[1].strip() == str(pid)
                   for row in csv.reader(io.StringIO(result.stdout)))
    # POSIX asks the shared implementation. Inline, this caught `OSError`, so
    # `PermissionError` -- which means the process EXISTS and belongs to another
    # user -- read as dead. MEASURED 2026-08-29 against PID 1: `watch_status()`
    # then reported "Watch process no longer running. State cleaned up." and
    # deleted BOTH the watch state file and the generated theme file, from a
    # command whose whole job is to report.
    # Identity, not only liveness, since 2026-10-12: the watch state file
    # outlives a reboot and the kernel reuses the PID, so a stale state read an
    # unrelated process as the watch and `watch_start` refused to start. The
    # watch runs marp-cli, so its command line carries "marp".
    return pid_runs_program(pid, "marp")


# ============================================================
# Self-Test
# ============================================================


def self_test() -> dict:
    """Run self-test: render the sample deck and validate output."""
    print(f"{BOLD}MARP Self-Test{RESET}")
    print(f"{GRAY}{'=' * 40}{RESET}")

    results = {"checks": [], "ok": True}

    # 1. Check marp-cli
    installed, version_str = check_marp_installed()
    if installed:
        results["checks"].append({"name": "marp-cli installed", "ok": True, "detail": version_str})
        version_ok = check_version_match(version_str)
        results["checks"].append({
            "name": "version match",
            "ok": version_ok,
            "detail": f"{'matches' if version_ok else 'MISMATCH with'} pinned {get_pinned_version()}"
        })
    else:
        results["checks"].append({"name": "marp-cli installed", "ok": False, "detail": "NOT FOUND"})
        results["ok"] = False
        return results

    # 2. Check browser
    browser = probe_browser()
    results["checks"].append({
        "name": "system browser",
        "ok": browser is not None,
        "detail": browser or "Not found: PDF export needs a Chromium-family browser on PATH"
    })

    # 3. Check the theme template exists. Every render reads it, and until now
    # the self-test checked the sample deck and the browser but not this file,
    # so a missing template killed `--self-test` itself with a traceback at
    # step 4 rather than reporting a failed check.
    #
    # Since HOS-131 the theme and the sample deck live in the data repository.
    # A clone with none (a public clone) cannot render a branded deck at all,
    # so the render half is SKIPPED and said to be, rather than failed: the
    # engine ships no sample content to render in its place.
    template, deck = theme_template(), sample_deck()
    if template is None:
        print(f"{YELLOW}render checks SKIPPED: no data repository, so no theme "
              f"template and no sample deck (registry name skill-brand-assets, skill=marp)"
              f"{RESET}")
        results["skipped"] = "no data repository"
        return results
    if not template.exists():
        results["checks"].append({"name": "theme template", "ok": False,
                                  "detail": f"NOT FOUND: {template}"})
        results["ok"] = False
        return results
    results["checks"].append({"name": "theme template", "ok": True, "detail": str(template)})

    # 4. Check sample deck exists
    if not deck.exists():
        results["checks"].append({"name": "sample deck", "ok": False, "detail": f"NOT FOUND: {deck}"})
        results["ok"] = False
        return results
    results["checks"].append({"name": "sample deck", "ok": True, "detail": str(deck)})

    # 5. Render to temp dir
    with tempfile.TemporaryDirectory(prefix="marp-test-", dir=data_write_dir("runtime:scratch/tmp")) as tmp_dir:
        tmp_path = Path(tmp_dir)
        start_time = time.time()

        result = render(deck, output_dir=tmp_path, verbose=True)
        elapsed = time.time() - start_time

        results["checks"].append({
            "name": "render success",
            "ok": result.get("ok", False),
            "detail": f"{elapsed:.1f}s"
        })

        if result.get("ok"):
            for output in result.get("outputs", []):
                size = output.get("size", 0)
                ok = True
                if output["type"] == "pdf" and size < 50_000:
                    ok = False
                if output["type"] == "html" and size < 20_000:
                    ok = False
                results["checks"].append({
                    "name": f"{output['type']} size",
                    "ok": ok,
                    "detail": f"{size:,} bytes"
                })

            # Check for hidden chars in HTML
            html_outputs = [o for o in result.get("outputs", []) if o["type"] == "html"]
            if html_outputs:
                html_text = Path(html_outputs[0]["path"]).read_text(encoding="utf-8")
                _, hidden = run_sanitizer(html_text)
                results["checks"].append({
                    "name": "hidden chars in HTML",
                    "ok": hidden == 0,
                    "detail": f"{hidden} found" if hidden > 0 else "clean"
                })

            results["checks"].append({
                "name": "duration under 60s",
                "ok": elapsed < 60,
                "detail": f"{elapsed:.1f}s"
            })
        else:
            for err in result.get("errors", []):
                results["checks"].append({
                    "name": f"{err['type']} render",
                    "ok": False,
                    "detail": err.get("message", "Unknown error")
                })

        results["hidden_characters"] = result.get("hidden_characters", "unknown")

    # Print results
    print()
    for check in results["checks"]:
        status = f"{GREEN}PASS{RESET}" if check["ok"] else f"{RED}FAIL{RESET}"
        print(f"  {status}  {check['name']}: {check['detail']}")
        if not check["ok"]:
            results["ok"] = False

    print()
    overall = f"{GREEN}ALL CHECKS PASSED{RESET}" if results["ok"] else f"{RED}SOME CHECKS FAILED{RESET}"
    print(f"  {BOLD}{overall}{RESET}")

    return results


# ============================================================
# Output / Result Printing
# ============================================================


def print_result(result: dict) -> None:
    """Pretty-print a render result."""
    if result.get("ok"):
        print(f"\n{GREEN}{BOLD}Render successful{RESET}")
    else:
        print(f"\n{RED}{BOLD}Render failed{RESET}")

    for output in result.get("outputs", []):
        size_kb = output.get("size", 0) / 1024
        print(f"  {CYAN}{output['type'].upper()}{RESET}: {output['path']} ({size_kb:.1f} KB)")

    for error in result.get("errors", []):
        print(f"  {RED}ERROR ({error.get('type', '?')}){RESET}: {error.get('message', '')}")

    for warning in result.get("overflow_warnings", []):
        print(f"  {YELLOW}WARNING{RESET}: Slide {warning['slide']} has {warning['words']} words "
              f"(threshold: {WORD_OVERFLOW_THRESHOLD})")

    hidden = result.get("hidden_characters", "unknown")
    print(f"  Hidden characters: {hidden}")

    if result.get("source_title"):
        print(f"  Title: {result['source_title']}")
    if result.get("source_mode"):
        print(f"  Mode: {result['source_mode']}")


# ============================================================
# Main / CLI
# ============================================================


def main():
    parser = argparse.ArgumentParser(
        description="MARP rendering shim for HEADING OS",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--self-test", action="store_true", help="Run self-test")
    # ONE declaration; each subcommand re-declares it with `default=argparse.SUPPRESS`
    # so a subparser's False cannot overwrite it and it works in either position
    # (MEASURED 2026-08-27 and 2026-08-29).
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--theme-variant", default=None,
                        help="Theme variant: a `themes/<name>.css.tmpl` of the brand "
                             "half (default: the alphabetically first); a name ending "
                             "in -investor is the brand-forward investor master.")

    subparsers = parser.add_subparsers(dest="command")

    # render
    render_p = subparsers.add_parser("render", help="Render a MARP .md file")
    render_p.add_argument("source", type=Path, help="Source .md file")
    # Mutually exclusive at the CLI so argparse names the conflict, and guarded
    # again inside `render` for programmatic callers. See the note there.
    formats = render_p.add_mutually_exclusive_group()
    formats.add_argument("--pdf-only", action="store_true")
    formats.add_argument("--html-only", action="store_true")
    render_p.add_argument("--images", choices=["png"], help="Also generate PNG images")
    render_p.add_argument("--output", type=Path, help="Output directory")
    render_p.add_argument("--auto-sanitize", action="store_true", default=False,
                           help="Render from sanitized copy instead of blocking on hidden chars")
    render_p.add_argument("--verbose", action="store_true",
                        default=argparse.SUPPRESS)

    # from
    from_p = subparsers.add_parser("from", help="Transform workspace markdown into slides")
    from_p.add_argument("source", type=Path, help="Source workspace .md file")
    from_p.add_argument("--break-at", choices=["h2", "h3", "manual"], default="h2")
    from_p.add_argument("--mode", choices=["dark", "light", "mixed"])
    from_p.add_argument("--title", type=str)
    from_p.add_argument("--subtitle", type=str)
    from_p.add_argument("--no-auto-cover", action="store_true")
    from_p.add_argument("--no-auto-closing", action="store_true")
    from_p.add_argument("--paginate-heavy", action="store_true")
    from_p.add_argument("--output", type=Path)
    from_p.add_argument("--verbose", action="store_true",
                        default=argparse.SUPPRESS)

    # watch
    watch_p = subparsers.add_parser("watch", help="Watch mode")
    watch_p.add_argument("action", nargs="?", default=None, help="Path to watch, or 'stop'/'status'")
    watch_p.add_argument("--verbose", action="store_true",
                         default=argparse.SUPPRESS)

    args = parser.parse_args()

    # Select the theme variant. prepare_theme() and inject_frontmatter() read these
    # module globals, so assigning here is enough. With no brand half the globals
    # stay empty and the render reports the missing theme, as it always did.
    variants = theme_variants()
    variant = getattr(args, "theme_variant", None) or (variants[0] if variants else "")
    if variants and variant not in variants:
        parser.error(f"--theme-variant {variant!r}: the brand half carries {', '.join(variants)}")
    global THEME_NAME
    THEME_NAME = variant

    if args.self_test:
        result = self_test()
        sys.exit(0 if result["ok"] else 1)

    if args.command == "render":
        result = render(
            source=args.source.resolve(),
            output_dir=args.output,
            pdf_only=args.pdf_only,
            html_only=args.html_only,
            images_png=args.images == "png",
            auto_sanitize=args.auto_sanitize,
            verbose=args.verbose,
        )
        print_result(result)
        sys.exit(0 if result["ok"] else 1)

    elif args.command == "from":
        result = transform_workspace_md(
            source=args.source.resolve(),
            break_at=args.break_at,
            mode=args.mode,
            title=args.title,
            subtitle=args.subtitle,
            no_auto_cover=args.no_auto_cover,
            no_auto_closing=args.no_auto_closing,
            do_paginate_heavy=args.paginate_heavy,
            output_dir=args.output,
            verbose=args.verbose,
        )
        print_result(result)
        sys.exit(0 if result["ok"] else 1)

    elif args.command == "watch":
        action = args.action
        if action == "stop":
            result = watch_stop()
            print(result["message"])
            sys.exit(0 if result.get("ok", True) else 1)
        elif action == "status":
            result = watch_status()
            if result.get("running"):
                print(f"{GREEN}Watch active{RESET} (PID {result['pid']})")
                print(f"  URL: {result['url']}")
                print(f"  Source: {result['source_path']}")
                print(f"  Started: {result['started_at']}")
            else:
                print(result["message"])
            sys.exit(0)
        elif action:
            source = Path(action).resolve()
            result = watch_start(source)
            if result.get("ok"):
                print(f"{GREEN}{result['message']}{RESET}")
            else:
                print(f"{RED}{result.get('message', 'Watch failed')}{RESET}")
            sys.exit(0 if result.get("ok") else 1)
        else:
            parser.print_help()
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
