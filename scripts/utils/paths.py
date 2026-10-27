"""Canonical workspace path resolution -- the single source of truth.

This module hardens workspace-root discovery so every downstream script,
hook, daemon, and shell launcher resolves the same root the same way, on
Windows, WSL2, native Linux, and macOS, regardless of where the repo is
cloned or which user runs it.

Resolution order for ``get_workspace_root()`` (first hit wins):

1. ``WORKSPACE_ROOT`` environment variable (explicit override -- used by
   systemd units, CI, containers, and tests). Must point at a real dir.
2. Marker walk: starting from this file, walk parent directories until one
   contains BOTH stable markers ``CLAUDE.md`` and ``.claude/``. This is the
   structural identity of a HEADING OS workspace and survives relocation.
3. Labeled fallback constant ``_FALLBACK_ROOT`` (three levels up from this
   file: ``scripts/utils/paths.py`` -> workspace root). Clearly labeled as
   a last resort, NOT a hardcoded absolute path.

There is intentionally no hardcoded ``/mnt/c/...`` or ``/home/<user>`` value
anywhere in the primary resolution path.

Backward compatibility: ``scripts/utils/workspace.py`` re-exports
``get_workspace_root`` and ``load_env`` from here, so the long-standing
``from scripts.utils.workspace import get_workspace_root`` import keeps
working unchanged. New code may import directly from this module.

Shell callers (``.sh`` scripts) can resolve the root without sourcing this
package via the documented one-liner::

    ROOT="$(python3 "$WS/scripts/utils/paths.py")"

or, when WORKSPACE_ROOT may already be set::

    ROOT="$(WORKSPACE_ROOT="${WORKSPACE_ROOT:-}" python3 -c \
      'from scripts.utils.paths import get_workspace_root as r; print(r())')"

See ``scripts/install-router-accuracy-timer.sh`` for the systemd install-time
templating pattern (it resolves the root from its own location and bakes it
into ``WorkingDirectory=`` and ``ExecStart=`` so the unit is self-contained).

Tests: none live. The override and engine-tree-warning behaviours below were each
covered until the 2026-09-21 test-suite split, and nothing goes red for them
now, so read the resolvers themselves before changing one.
"""

import logging
import os
import re
from pathlib import Path

_log = logging.getLogger(__name__)

# ============================================================
# Constants
# ============================================================

# Stable markers that identify a HEADING OS workspace root. Both must be present.
_ROOT_MARKERS = ("CLAUDE.md", ".claude")

# Labeled last-resort fallback: three parents up from this file.
#   scripts/utils/paths.py -> scripts/utils -> scripts -> <workspace root>
# This is a STRUCTURAL fallback derived from __file__, NOT a hardcoded
# absolute path. It is only used if the env override is unset and the
# marker walk finds nothing (e.g. markers were renamed).
_FALLBACK_ROOT = Path(__file__).resolve().parent.parent.parent


# ============================================================
# Root resolution
# ============================================================

def _has_markers(candidate: Path) -> bool:
    """True if every marker in _ROOT_MARKERS exists under candidate."""
    return all((candidate / marker).exists() for marker in _ROOT_MARKERS)


def get_workspace_root() -> Path:
    """Return the workspace root as an absolute pathlib.Path.

    Resolution order (first hit wins):
      1. WORKSPACE_ROOT env override (if set and the directory exists).
      2. Walk up from this file to the dir containing CLAUDE.md AND .claude/.
      3. Labeled structural fallback (_FALLBACK_ROOT).

    Never returns a hardcoded absolute literal as the primary value.
    """
    # 1. Explicit environment override.
    env_root = os.environ.get("WORKSPACE_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser()
        # Resolve even if it does not exist yet, but only honour it when real.
        if candidate.is_dir():
            return candidate.resolve()

    # 2. Marker walk up from this file.
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if parent.is_dir() and _has_markers(parent):
            return parent

    # 3. Labeled fallback constant.
    return _FALLBACK_ROOT


# ============================================================
# Data-root seam (HEADING OS engine/data separation, spec Section 2)
# ============================================================

# Bump when the on-disk private-data format changes in a way that needs migration.
DATA_SCHEMA_VERSION = 1


class DataRootError(RuntimeError):
    """Raised when the data root cannot be honoured.

    Two cases: a write attempted with no real data root (demo mode), and a
    `HEADING_OS_DATA` that names a path which is not a directory.

    A `RuntimeError` on purpose. Callers wrap filesystem work in
    `except OSError`, and an `OSError` subclass here would be swallowed by a
    handler written for a missing file, restoring the silence this replaced.
    """


def env_data_root() -> Path | None:
    """The HEADING_OS_DATA override, or None when it cannot be honoured.

    Set-but-missing is the dangerous case and it used to be SILENT. Both
    resolvers read the variable, checked `is_dir()`, and on a miss simply fell
    through to the next candidate -- which on this machine is the operator's
    real private overlay. So a caller that set the variable precisely to keep
    a write away from live data got the live data instead, with nothing said.

    Measured on 2026-08-24: `tests/test_docx_helpers.py` built its sandbox as
    `tmp_path / "data"` and, for cases with no seed files and no brand
    template, never created it. Three generators then wrote into
    `.heading-os-data/outputs/` for real, overwriting three tracked exec-meeting
    documents. The sandbox had only ever worked because an unrelated
    `mkdir` of the output leaves happened to create the root as a side effect.

    The first fix kept the fallback and added a warning, on the reasoning that
    an `.env` naming a path that has since moved should not brick every
    session. The operator overruled that on 2026-08-25, and the reason is
    sound: setting this variable is a deliberate act, nobody sets it by
    accident, and "I told you where the data is" followed by a write somewhere
    else is not a recoverable state. A warning is one line inside a hundred,
    and in a daemon or a scheduled run nobody reads it at all.

    So a set-but-missing value now RAISES. Unset is untouched and still returns
    None, which is the ordinary path for every caller that does not use the
    override.

    `DataRootError`, not `NotADirectoryError`: several callers wrap filesystem
    work in `except OSError`, and this must not be swallowed by a handler
    written for a missing file.
    """
    env = os.environ.get("HEADING_OS_DATA")
    if not env:
        return None
    cand = Path(env).expanduser()
    if cand.is_dir():
        return cand.resolve()
    raise DataRootError(
        f"HEADING_OS_DATA is set to {env!r}, which is not an existing "
        f"directory. Refusing to fall back: the fallback on an operator "
        f"machine is the live private overlay, so continuing would write real "
        f"data to a path you did not ask for. Create the directory, or unset "
        f"HEADING_OS_DATA."
    )


def engine_siblings_root(root: Path | None = None) -> Path:
    """The directory the engine clone's SIBLINGS live in.

    The workspace's private repository is laid out beside the engine clone:
    `.heading-os-data`. Every caller that needed a sibling wrote
    `get_workspace_root().parent`, which is the parent of THIS CHECKOUT.

    A YARD is a git worktree of the engine, and it is not beside the engine: it
    lives under `.yard/.heading-os/<branch>`, two levels below. `.parent` there
    names `.yard/.heading-os`, which holds no sibling repository. MEASURED
    2026-09-09 from a worktree: the two sibling resolvers of that day (for the
    corporate repository and for an executive's overlay, both removed with the
    executive model on 2026-09-25) pointed inside `.yard`.

    So the question is asked of git, exactly as `scripts/utils/clone_guard.py`
    asks it: a worktree's `.git` is a FILE, and `--git-common-dir` names the
    main clone's `.git`. Never assembled by walking `..` a fixed number of
    times, which is the defect this function exists to stop repeating.

    Two properties worth keeping: in the main clone this is `root.parent` and
    costs one `stat`, no subprocess; and anything that is not a checkout, or a
    machine with no usable git, falls back to `root.parent`, the behaviour
    every caller had before.

    NOT used by `get_data_root()` below, and that is a decision rather than an
    oversight. Its step 3 walks the same `root.parent`, so a YARD with no
    `HEADING_OS_DATA` misses the overlay and collapses to the demo tree. Moving
    it would decide what an UNPROVISIONED yard may reach, which is a safety
    question and not a path repair: every guard gated on `data_root_is_demo()`
    or `data_overlay_present()` changes its answer there. Put to the operator
    2026-09-09; he kept the current behaviour, with the yard's overlay coming
    from the `HEADING_OS_DATA` its bootstrap writes into `.env`, and the
    unprovisioned case reported by `check_yard_bootstrap` in
    `.claude/hooks/session-start.py`. That hook still runs; the test that
    covered the unprovisioned case does not, after the 2026-09-21 test-suite
    split. Do not "make it consistent" with this function without asking again.
    """
    root = Path(root) if root is not None else get_workspace_root()
    try:
        if not (root / ".git").is_file():
            return root.parent               # the main clone, or not a checkout
    except OSError:
        return root.parent

    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            # `errors="replace"` and not a bare `text=True`: an undecodable byte
            # raises `UnicodeDecodeError`, a ValueError that the handler below
            # does not catch, so one byte would propagate out of a path lookup.
            capture_output=True, text=True, errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return root.parent
    common = proc.stdout.strip()
    if proc.returncode != 0 or not common:
        return root.parent
    return Path(common).parent.parent


#: The file only the engine carries: the list `scripts/check-engine-only.py`
#: holds every tracked engine file against. A tree holding it has declared
#: itself engine-only, whatever else has appeared inside it.
ENGINE_TREE_MARKER = Path("config") / "engine-allowlist.yaml"


def is_engine_tree(root: Path | None = None) -> bool:
    """True when ``root`` (default: the workspace root) is an engine checkout.

    Asked of the tree itself and never inferred from where the data root
    resolved, because that inference is what a stray directory or a
    ``HEADING_OS_DATA`` naming the checkout could turn off.
    """
    root = Path(root) if root is not None else get_workspace_root()
    return (root / ENGINE_TREE_MARKER).is_file()


def get_data_root() -> Path:
    """Resolve the private-data root. First hit wins:

      1. HEADING_OS_DATA env override, when it points at a real dir. A value
         naming a path that does not exist RAISES ``DataRootError`` (see
         ``env_data_root``); it is never ignored and never falls through to the
         rules below, because on an operator machine the next rule to match is
         the live private overlay.
      2. Sibling ``../.heading-os-data`` (the dedicated data repo).
      3. Demo mode: ``<workspace_root>/examples``, a SENTINEL meaning "no data
         repository". The engine shipped a small invented tree there until
         HOS-131 (2026-09-25) made the engine hold only the engine; nothing is
         there now, and a reader in demo mode sees a directory that does not
         exist. Kept as the answer rather than removed because every guard
         asks ``data_root_is_demo()`` and that question still has to have one.

    The workspace itself is never its own data root: a legacy in-tree rule
    stood before the sibling rule until 2026-10-16 (engine audit q73). An engine
    tree carrying a ``crm/contacts`` or ``knowledge`` directory is still reported  # leak-guard: ok (the marker directories the warning below looks for)
    with a warning, because that data belongs in the data repository.
    """
    env_root = env_data_root()
    if env_root is not None:
        return env_root
    root = get_workspace_root()
    in_tree = (root / "crm" / "contacts").is_dir() or (root / "knowledge").is_dir()
    if in_tree and is_engine_tree(root):
        # An engine tree is never its own data root. Answering the engine here
        # used to switch the leak guard off (2026-09-28): `_in_engine_repo()`
        # found the two equal and `check-staged` passed every file.
        _log.warning(
            "get_data_root(): %s is an engine tree carrying a crm/contacts or "  # leak-guard: ok (the marker directories this warning looks for)
            "knowledge directory; ignoring it as a data root. Move that data to "
            "the data repository.", root)
    sibling = root.parent / ".heading-os-data"
    if sibling.is_dir():
        return sibling.resolve()
    return (root / "examples").resolve()


def data_root_is_demo() -> bool:
    """True when get_data_root() resolved to the demo sentinel: no data repository.

    The sentinel is ``<workspace_root>/examples``, which the engine no longer
    ships (HOS-131), so a True here means "there is no data at all", not "there
    is example data"."""
    return get_data_root() == (get_workspace_root() / "examples").resolve()


def data_overlay_present() -> bool:
    """True when a SEPARATE private data overlay backs this workspace.

    Narrower than ``not data_root_is_demo()``: it also answers False when
    ``HEADING_OS_DATA`` names the workspace itself, so a guard gated on it never
    asserts against a root that is not a data overlay (reported against v0.8.0,
    when a since-removed in-tree rule could answer the workspace).

    So a guard that needs the operator's private records should ask this, not
    ``data_root_is_demo()``: True only where the overlay is a real sibling, or an
    explicit ``HEADING_OS_DATA`` naming another directory.
    """
    return not data_root_is_demo() and get_data_root() != get_workspace_root()


def read_data_schema_version() -> int:
    """Read the data root's .schema-version. Missing/unreadable -> assume current
    (demo roots carry no marker and must not be blocked)."""
    f = get_data_root() / ".schema-version"
    try:
        return int(f.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return DATA_SCHEMA_VERSION


def check_schema_compatible() -> tuple[bool, str]:
    """Return (ok, message). ok=False only when the engine schema is NEWER than
    the data on disk -- i.e. a migration is required before the workspace runs."""
    data_v = read_data_schema_version()
    if data_v < DATA_SCHEMA_VERSION:
        return (
            False,
            f"Engine data schema v{DATA_SCHEMA_VERSION} is newer than data v{data_v}; "
            "run a data migration before continuing.",
        )
    return (True, "")


def require_writable_data_root() -> Path:
    """Return the data root, or raise DataRootError when there is none (demo mode)."""
    if data_root_is_demo():
        raise DataRootError(
            "No private data folder found - running with no data at all. "
            "Run `python scripts/init-data.py` to create your data folder."
        )
    # F-9.7: refuse when the overlay schema is behind the engine (pending
    # migrations), so a write can never land on an un-migrated overlay. On the
    # live workspace this is a strict no-op: with no .schema-version file,
    # read_data_schema_version() returns the current DATA_SCHEMA_VERSION and the
    # only registered migration is the v1 baseline, so nothing is pending. The
    # refusal fires only once a future migration bumps max_version() above a
    # stamped overlay's recorded version. Local import avoids an import cycle
    # (scripts.migrations is discovered at call time, not module load).
    from scripts.migrations import max_version
    data_v = read_data_schema_version()
    target = max_version()
    if data_v < target:
        raise DataRootError(
            f"Data overlay schema v{data_v} is behind engine v{target}; pending "
            "migrations must run first. Run: python scripts/migrate-data.py --apply"
        )
    return get_data_root()


def require_outside_engine_clone(path: Path, what: str) -> Path:
    """Return ``path``, or raise DataRootError when it sits inside the engine.

    Operator law, 2026-08-26: no data from the DATA repository may ever sit in
    the engine. The mechanism that broke it is a write path: with no private
    overlay ``get_data_root()`` falls to its documented last resort
    ``<workspace_root>/examples``, so a tool that writes to the data root writes
    into the repository that gets pushed.

    Asks about the PATH, not about the environment, and the difference is the
    whole point. The first version of this guard asked
    ``data_overlay_present()``, which is a fact about the machine rather than
    about the write, and it refused fifty writes that were already safe: the
    team-bot and capture suites redirect their module-level directory constant
    to a ``tmp_path`` before calling anything, so nothing could reach the clone
    and the guard stopped them anyway. Measured on a worktree with no overlay:
    13 failures became 63. A guard that fires on safe work gets deleted by the
    next person who hits it, and the law goes with it.

    ``what`` names the caller in the message, because the refusal is read by
    somebody who ran one script and needs to know which write was refused.
    """
    resolved = Path(path).resolve()
    root = get_workspace_root().resolve()
    if resolved == root or root in resolved.parents:
        raise DataRootError(
            f"{what} resolved to {resolved}, inside the engine clone at {root}. "
            "The engine is code only. Point HEADING_OS_DATA at a private data "
            "overlay, or run `python scripts/init-data.py` to create one."
        )
    return resolved


def assert_data_root_external() -> Path:
    """Return the data root, or raise `DataRootError` naming what is wrong.

    The precondition a YARD (a git worktree of the engine) has to clear before
    an agent starts in it, and the one that no existing check covered.

    The leak guard and the data-path redirect both auto-activate on
    ``get_data_root() != get_workspace_root()``. That predicate is TRUE in demo
    mode as well, because ``<root>/examples`` is not ``<root>``, so both stay
    armed and the workspace looks healthy while every classification decision is
    being made against the demo sentinel (an example tree until HOS-131, nothing
    since) instead of the operator's real layout. Nothing errors and nothing warns.

    MEASURED 2026-09-03 in a fresh worktree of this repository, with no `.env`
    (it is gitignored, so a new checkout never has one):

        get_workspace_root()   -> <worktree>            correct
        get_data_root()        -> <worktree>/examples   DEMO
        data_overlay_present() -> False

    Sibling auto-discovery is what collapses: a YARD lives under
    ``~/ai/claude-workspaces/.yard/<name>/``, so ``../.heading-os-data`` resolves
    to ``.yard/.heading-os-data``, which does not exist. Hence the absolute
    ``HEADING_OS_DATA`` a YARD's `.env` carries, and hence this assertion, which
    checks the RESULT rather than trusting that the variable was written.

    Five refusals, each with its own message:

      1. ``HEADING_OS_DATA`` set to a relative path. It would mean a different
         directory to a daemon, to a systemd unit and to a shell, and only one
         of them would be right. (A set-but-missing value already raises inside
         ``env_data_root``; this catches the value that exists but is ambiguous.)
      2. Demo mode: the ``examples`` sentinel, which names no data at all.
      3. The data root IS the workspace root.
      4. The data root sits inside the workspace root.
      5. The data root is not a git repository, so nothing there can be
         committed and `/backup` would silently do nothing.

    Cases 3 and 4 are delegated to ``require_outside_engine_clone`` rather than
    reimplemented, so there is one containment rule in this file and not two
    that can drift apart.

    Demo is checked BEFORE containment even though ``<root>/examples`` is inside
    the clone and would be caught there. The two refusals have different
    remedies: containment says "point the variable somewhere else", and demo
    says "in a worktree this is what an UNSET variable looks like, because
    sibling discovery cannot find `../.heading-os-data` from a checkout that is
    not beside it". Ordering it second is what lets the message name the actual
    cause instead of a symptom.
    """
    env = os.environ.get("HEADING_OS_DATA")
    if env and not Path(env).expanduser().is_absolute():
        raise DataRootError(
            f"HEADING_OS_DATA is set to the relative path {env!r}. It would "
            f"resolve against whatever directory the caller happened to be in, "
            f"so a daemon, a systemd unit and a shell would each reach a "
            f"different tree. Set it to an absolute path."
        )

    root = get_data_root()
    if data_root_is_demo():
        raise DataRootError(
            f"The data root resolved to {root}, the demo sentinel, which "
            f"holds no data at all. In a worktree this is what an unset HEADING_OS_DATA looks "
            f"like: sibling auto-discovery cannot find ../.heading-os-data from "
            f"a checkout that is not beside it. The workspace will look healthy "
            f"and classify every path against no data. Set HEADING_OS_DATA "
            f"to the absolute path of the real overlay."
        )

    # Raises for cases 3 and 4, naming the resolved path and the engine clone.
    require_outside_engine_clone(root, "The data root")

    if not (root / ".git").exists():
        raise DataRootError(
            f"The data root {root} is not a git repository. Its history is how "
            f"the overlay is backed up, so a root without one loses every write "
            f"silently. Check HEADING_OS_DATA, or run "
            f"`python scripts/create-data-repo.py`."
        )
    return root


# ============================================================
# Home + data/state/log dir helpers
# ============================================================

def home() -> Path:
    """Return the current user's home directory (cross-platform).

    Honours the HOME env var on POSIX and USERPROFILE on Windows via
    pathlib's own resolution. Never embeds a literal username.
    """
    return Path.home()


# `data_dir`, `state_dir`, `private_cache_dir` and `log_dir` stood here until
# 2026-09-25. Each answered a directory INSIDE the engine checkout (`.data/`,
# `.state/`, `.cache/`, `.logs/`, all gitignored) unless an environment variable
# moved it, and HOS-5 section 7 took every artifact out of the checkout that day
# (operator decision, verbatim: "old way should go out and do not stay in the
# workspace"). A writer asks `scripts/utils/data_path.py` for
# `runtime:<service>/<kind>` instead. `data_dir` had no caller left.


# ============================================================
# .env loading (canonical; re-exported by workspace.py)
# ============================================================

# A POSIX environment-variable name. Deliberately case-permissive: this is a
# READER, and refusing to see a lowercase key the shell would happily export is
# a silent miss, not a safety property.
_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def parse_env_line(line: str):
    """The ``(key, value)`` a single ``.env`` line assigns, or None.

    ONE grammar for the whole workspace. Six places parsed this one file with
    six hand-rolled rules, and MEASURED 2026-08-28 they disagreed about the same
    bytes:

        line                load_env      load_gh_token   load_env_key
        KEY="quoted"        quoted        quoted          "quoted"
        KEY='quoted'        quoted        quoted          'quoted'
        <space>KEY=v        v             no match        no match
        export KEY=v        key was       no match        no match
                            "export KEY"

    Every consequence is silent. A ``.env`` written in the dotenv-quoted style
    the loader below documents as supported sends an API key to healthchecks.io
    WITH its quotes attached. One leading space in front of ``GH_TOKEN=`` makes
    every push report "no GH_TOKEN in engine .env", which names a cause that is
    not true. ``export KEY=v`` set a variable literally named ``export KEY`` and
    left ``KEY`` unset.

    Returns None for a blank line, a comment, a line with no ``=``, and a line
    whose key is not a valid environment-variable name. None means "this line
    assigns nothing", which is also exactly what a WRITER needs to know before
    deciding whether it is looking at the line it came to replace: readers and
    writers that disagree about that leave duplicate keys behind.
    """
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if key[:6] == "export" and key[6:7] in (" ", "\t"):
        key = key[6:].strip()
    if not _ENV_NAME_RE.match(key):
        return None
    value = value.strip()
    # ONE matching pair, never a character-class strip. A chained
    # `.strip('"').strip("'")` took the trailing quote off `KEY="unbalanced`,
    # which has no pair at all, and unwrapped `KEY="'x'"` twice down to `x`.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def iter_env_pairs(text: str):
    """Every ``(key, value)`` assigned in ``.env`` text, in file order."""
    for line in text.splitlines():
        pair = parse_env_line(line)
        if pair is not None:
            yield pair


def read_env_value(env_path, key: str, *, default=None):
    """The FIRST value ``key`` is assigned in the ``.env`` at *env_path*.

    FIRST, not last, because ``load_env`` uses ``setdefault``: on a file with a
    duplicate key it is the first line that reaches ``os.environ``, so any other
    answer would disagree with the environment the same file produces.

    Fail-soft on purpose. A missing, unreadable, or non-UTF-8 file returns
    *default* rather than raising: ``load_gh_token`` is evaluated eagerly by
    every ``supervised_push`` caller, and a wall built to fail open must not
    carry a hard-crash path.
    """
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug(".env unreadable at %s: %s", env_path, exc)
        return default
    for found, value in iter_env_pairs(text):
        if found == key:
            return value
    return default


def load_env(workspace_root: Path = None) -> None:
    """Load .env variables into os.environ (without overwriting existing vars).

    Strips matching surrounding single/double quotes from values per dotenv
    convention, so KEY="value" and KEY=value both yield 'value'. Without this,
    callers that pass the value straight into libraries expecting bare strings
    (e.g. a URL handed to httpx) hit "missing scheme" errors when the literal
    '"https://..."' arrives intact. The grammar is `parse_env_line`, shared with
    every other reader and writer of this file.

    The read is whole-file rather than line-by-line so a non-UTF-8 byte raises
    before any variable is set. The streaming version decoded a read buffer at a
    time, so on a file that fits in one buffer it also raised before setting
    anything; past that it did not. MEASURED 2026-08-28 with a bad byte at the
    end: a 1711-byte file set 0 variables before raising, an 18911-byte file set
    1749, leaving the process half-populated with no record of where it stopped.
    """
    root = workspace_root or get_workspace_root()
    env_path = root / ".env"
    if not env_path.exists():
        return
    for key, value in iter_env_pairs(env_path.read_text(encoding="utf-8")):
        os.environ.setdefault(key, value)


TZ_FALLBACK = "UTC"


def resolve_tz_name() -> tuple[str, bool]:
    """The operator's IANA zone name, and whether anything actually set one.

    Reads `os.environ` only. The caller owns `load_env()`, because the two
    callers cache it differently: `workspace.get_default_tz_name` loads once per
    process, the `tz` CLI below loads on every invocation.

    ONE owner for the BLANK case, which is the whole reason this function
    exists. `HEADING_OS_TZ` had two readers and they disagreed about an empty
    value. The `tz` CLI treated it as unset and answered UTC with a line on
    stderr; `get_default_tz_name` returned `""`, and `get_default_tz()` then
    raised `ValueError: ZoneInfo keys must be normalized relative paths, got:`
    out of a helper documented to default to UTC. MEASURED 2026-09-01 with a
    scratch `.env` carrying a bare `HEADING_OS_TZ=`: the CLI printed `UTC` and
    exited 0 while the in-process helper crashed every caller that asked for a
    zone. `.env` is hand-edited and gitignored, so a key left with no value is
    an ordinary typo rather than a contrived input.

    Whitespace is stripped for the same reason: `HEADING_OS_TZ= ` is the same
    typo with a space after it, and `ZoneInfo(" ")` raises the same way.

    The boolean is the announcement signal, kept separate from the name so the
    CLI can say "nothing configured a zone" without re-deriving it from a
    comparison against `TZ_FALLBACK`. An operator who really did write
    `HEADING_OS_TZ=UTC` configured a zone, and should not be told he did not.
    """
    name = (os.environ.get("HEADING_OS_TZ") or "").strip()
    if not name:
        return TZ_FALLBACK, False
    return name, True


# ============================================================
# Shell-callable resolver
# ============================================================
# Running this module directly prints the resolved workspace root, so .sh
# scripts can do:  ROOT="$(python3 scripts/utils/paths.py)"
#
# With the `tz` argument it prints the operator's timezone instead:
#   TZ_VALUE="$(python3 scripts/utils/paths.py tz)"
#
# The timer installers need this because they are bash and cannot read `.env`,
# where HEADING_OS_TZ actually lives. Reading the environment alone -- which is
# what they did -- renders UTC on a machine whose timezone is correctly
# configured, because nothing exports that variable. Measured 2026-08-03: it is
# unset even in an interactive login shell.
#
# Precedence is load_env's, unchanged: an explicit `HEADING_OS_TZ=X install.sh`
# still wins over `.env`, because load_env uses setdefault.
if __name__ == "__main__":
    import sys as _sys

    if _sys.argv[1:2] == ["tz"]:
        load_env()
        # `resolve_tz_name`, not a second reading of the variable. The blank
        # case was handled here and nowhere else, so the in-process helper
        # answered "" for the same `.env` this branch answered UTC for.
        _tz, _configured = resolve_tz_name()
        if not _configured:
            # Announced, never silent. A silent UTC default is the root of every
            # defect this resolver exists to end: an installer rendered UTC while
            # the operator believed the unit was local, and nothing said so.
            # stdout stays clean for the shell that consumes it.
            print("HEADING_OS_TZ resolved from neither the environment nor .env; "
                  "falling back to UTC", file=_sys.stderr)
        print(_tz)
    else:
        print(get_workspace_root())
