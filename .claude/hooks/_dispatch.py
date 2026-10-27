#!/usr/bin/env python3
"""Consolidated CEO-machine PreToolUse hook dispatcher.

One process runs every check in-process: first block wins, advisories are
aggregated. There are no per-check shims (deleted in ba1affd); a
settings.local.json that still names one needs `scripts/setup-platform.sh` re-run.

Matcher scope: settings.local.json registers this dispatcher under seven
matchers — `Write|Edit|MultiEdit|NotebookEdit`, `Bash`, `Read|Grep|Glob`,
`mcp__codegraph__.*`, `Agent|Task|Workflow`, `SendMessage` (2026-09-27, so
continuing an agent resets the fan-out count as dispatching one does), and
`mcp__.*hindsight.*` (2026-10-04, HOS-198, so a Hindsight MCP query can open
`check_hindsight_first`).
Every payload shape reaches every check, so a new check has to answer what it
does with each of them rather than inherit a three-matcher assumption. The
codegraph and agent matchers arrived on 2026-08-29 with the walls that need them: a matcher is the only
thing that makes a door reachable, and `check_graph_first` and
`check_fanout_first` were each written with an unlock the dispatcher could not
see. A wall whose unlock is unreachable is a cage. Grep and
Glob joined the third matcher on 2026-08-28: `check_protect_personal_threads`
refused the Bash spellings of a personal-thread read (`grep`, `rg`) while the
native tools were not dispatched here at all, so the guard covered the harder
route and missed the plain one. Read carries a
`file_path` but no content, which is why `check_protect_docs` excludes it by
name: `check_protect_docs` reaching a path
test on a Read payload is what policy-denied an ordinary operator Read at
2026-08-11T21:44:19, recorded in the denial log. The path-scoped checks return
None on a Bash payload for the plainer reason that it carries no `file_path` at
all.

`scripts/heading_os_2/resolve.py` is not imported here and must not be: it
parses the manifest on every call and REFUSES on a tree that has none (the CI
demo tree, a pull-only service host), which for a wall means dying exactly where it is
needed (MEASURED 2026-09-14: 55 ms to import at its fastest).
"""
from __future__ import annotations
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# `typing` is deliberately NOT imported. `from __future__ import annotations`
# above makes every annotation in this file a string that nothing evaluates, and
# `python3 -X importtime` charged the import 4.9 ms of the ~43 ms this hook costs
# on EVERY Write, Edit, MultiEdit, NotebookEdit, Bash and Read. Removed
# 2026-08-20. If you add a runtime typing use here (cast, TypeVar, a validated
# model), import it inside the function that needs it, not at module scope.

WORKSPACE = Path(__file__).resolve().parent.parent.parent

# `HEADING_OS_STATE_DIR` is read here as well as in
# `scripts/utils/checkpoint_paths.state_root()`, and the duplication is
# deliberate rather than an oversight. This hook runs on EVERY Write, Edit,
# MultiEdit, NotebookEdit, Bash and Read, and importing that module at module
# scope to reach one five-line resolver would charge the import to all of them;
# the comment on the absent `typing` import twelve lines up is the same
# reasoning. The two copies must agree, on the exact value AND on the
# relative-path refusal, and a check used to make the copy that stopped being
# fixed fail rather than drift. It was parked in the 2026-09-21 test-suite
# split, so the agreement now has to be carried by hand: a change to either
# resolver is a change to both. The obligation was written because "a fix that
# landed in one of two copies" is a defect this repository has already shipped
# once.
#
# Frozen at import, like `RATE_LIMIT_STATE_FILE` below, and that is correct for
# what reads it: every consumer is a hook the harness starts as a fresh
# subprocess, so the environment is fixed before this line runs. An IN-PROCESS
# test that sets the variable after import will not move these constants and
# must `monkeypatch.setattr` the constant itself.
#
# Answered once per process and remembered: four module-level constants ask
# it, and each unpinned ask went through the data-path registry again. A hook
# process ends before the environment or the registry could change under it.
_STATE_DIR_MEMO: list[Path] = []


def _state_dir() -> Path:
    if not _STATE_DIR_MEMO:
        _STATE_DIR_MEMO.append(_resolve_state_dir())
    return _STATE_DIR_MEMO[0]


def _resolve_state_dir() -> Path:
    """This checkout's guard state, honouring the pin. See `state_root()` for the why.

    Not `.claude/state/` inside the checkout any more: since 2026-09-25 (HOS-5
    section 7) it is the data repository's `checkout-state` for THIS checkout,
    answered by `checkpoint_paths.checkout_state_root`, which also owns the one
    case that still answers inside the checkout (a tree with no data repository
    at all). Imported at the call, after `sys.path` holds this checkout; the
    module-scope import the comment above warns against is still avoided.
    """
    env = os.environ.get("HEADING_OS_STATE_DIR")
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_absolute():
            return candidate
        print(
            f"dispatch: HEADING_OS_STATE_DIR={env!r} is relative and is being "
            f"ignored; a relative value resolves against whatever directory the "
            f"harness launched the hook in.",
            file=sys.stderr)
    from scripts.utils.checkpoint_paths import checkout_state_root

    return checkout_state_root(WORKSPACE)

# One lexical path collapse, shared with `data-path-redirect.py`. Measured cost
# of the import: inside the run-to-run noise of this hook (12 runs each way,
# 2026-08-29), so it is at module scope rather than deferred.
sys.path.insert(0, str(WORKSPACE))
from scripts.utils.pathnorm import normalize_path, normalize_segments  # noqa: E402

def _record_denial(mechanism: str, payload: dict, reason: str) -> None:
    """Count one refusal. Telemetry only — it can never change a decision.

    Called from main()'s terminal deny path and from nowhere else, so a check
    added tomorrow is counted by construction rather than by its author
    remembering to add a line. Lazy and guarded, because this runs inside the
    workspace's only synchronous wall between a model mistake and a written
    credential, and no failure here may take that wall down. It runs on denials
    only, which are rare, so the overwhelming majority of tool calls never pay
    for the import.
    """
    try:
        if str(WORKSPACE) not in sys.path:
            sys.path.insert(0, str(WORKSPACE))
        from scripts.utils.denial_log import log_denial

        tool_input = payload.get("tool_input") or {}
        log_denial(
            mechanism=mechanism,
            action=payload.get("tool_name") or "unknown",
            path=(tool_input.get("file_path")
                  or tool_input.get("notebook_path")
                  or None),
            reason=reason,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[_dispatch] denial counter unavailable ({type(exc).__name__}): {exc}",
              file=sys.stderr)

# ============================================================
# check_prevent_secrets — secret patterns in content or Bash commands
# ============================================================

SECRET_PATTERNS = [
    (re.compile(r'(?<![A-Za-z0-9_-])sk-ant-[a-zA-Z0-9_-]{16,}'), "Anthropic API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])pplx-[a-zA-Z0-9]{16,}'), "Perplexity API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])r8_[a-zA-Z0-9]{16,}'), "Replicate API token"),
    (re.compile(r'(?<![A-Za-z0-9_-])fc-[A-Za-z0-9]{16,}'), "Firecrawl API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])ctx7sk-[a-zA-Z0-9-]{16,}'), "Context7 API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])cpx-[a-zA-Z0-9]{16,}'), "CLIProxyAPI local proxy key"),
    (re.compile(r'(?<![A-Za-z0-9_-])ghp_[a-zA-Z0-9]{16,}'), "GitHub personal access token"),
    (re.compile(r'(?<![A-Za-z0-9_-])gho_[a-zA-Z0-9]{16,}'), "GitHub OAuth token"),
    # Boundary-anchored so an embedded base64 image cannot spell a key. The
    # reasoning, the measurement, and why the padding character is NOT in the
    # lookahead live in `scripts/utils/secret_patterns.py`; this copy exists
    # because that import cannot be made here (see the module docstring there).
    # Nothing compares the two any more: the check that held them byte-identical
    # went in the 2026-09-21 test-suite split, so an edit to one side that skips
    # the other is caught by a reader or not at all.
    (re.compile(r'(?<![A-Za-z0-9+/])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/])'), "AWS access key"),
    (re.compile(r'(?<![A-Za-z0-9_-])xoxb-[0-9]+-[a-zA-Z0-9]+'), "Slack bot token"),
    (re.compile(r'(?<![A-Za-z0-9_-])xoxp-[0-9]+-[a-zA-Z0-9]+'), "Slack user token"),
    (re.compile(r'(?<![A-Za-z0-9_-])ya29\.[A-Za-z0-9._-]{50,}'), "Google OAuth token"),
    # JWT, PEM private keys, and credentialed connection strings (F-L3; mirror in secret-scanner.py)
    (re.compile(r'(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), "JWT bearer token"),
    # Boundary-anchored 2026-09-22 so the literal cannot fire from the middle of
    # a run of token characters. `-` is deliberately absent from both
    # lookarounds; the reasoning and the measurement are at the mirror of this
    # entry in scripts/utils/secret_patterns.py. Nothing compares the two copies
    # any more (see the note on the AWS entry above), so this one was edited by
    # hand in the same change.
    (re.compile(r'(?<![A-Za-z0-9_])-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----(?![A-Za-z0-9_])'), "PEM private key"),
    # Kept character-for-character in step with scripts/utils/secret_patterns.py.
    # The userinfo halves exclude what a URI cannot carry unencoded, so JSON
    # punctuation plus a JSON-LD `@` key no longer reads as `host:user@`.
    # MEASURED 2026-09-17: see the sibling for the numbers.
    (re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]{0,31}://(?!user:pass(?:word)?@|username:password@)[^:@\s/?"<>{}|\\^`\[\]]{2,}:[^:@\s/?"<>{}|\\^`\[\]]{2,}@'), "connection string with inline credentials"),
    # Case is ignored on the four space-terminated alternatives only, since
    # 2026-09-27. Why those four and not the other six, and why that cannot
    # admit a secret the old form refused: the mirror in
    # scripts/utils/secret_patterns.py. Edited by hand in the same change.
    (re.compile(
        r'\*\*Password:\*\*\s+'
        r'(?!Stored|REDACTED|N/A|TBD|Change|Reset|(?i:See |Set |Use |Your ))'
        r'[^\n]{8,}'
    ), "Plaintext password in markdown"),
    # Whole-value placeholder exclusion; the rationale, the dropped inert `<`
    # alternative, and the residual bound are documented at the mirror of this
    # entry in scripts/utils/secret_patterns.py. The regex text must still stay
    # byte-identical to that mirror, and since the 2026-09-21 split nothing
    # verifies that it does. Copy the mirror across rather than retyping it.
    (re.compile(
        r'(?:EXCHANGE_PASSWORD|DB_PASSWORD|SMTP_PASSWORD|AUTH_PASSWORD)'
        r'\s*=\s*'
        r'(?i:(?!'
        r'(?=[A-Za-z0-9_-]*(?:your|changeme|example|placeholder|redacted|dummy|x{3,}))'
        r'(?:[A-Za-z]+[0-9]{0,3}|[0-9]{1,4})'
        r'(?:[-_](?:[A-Za-z]+[0-9]{0,3}|[0-9]{1,4}))*'
        r'(?![A-Za-z0-9!@#$%^&*_+=-])'
        r'))'
        r'[A-Za-z0-9!@#$%^&*_+=-]{8,}'
    ), "Password in environment variable assignment"),
]

# Mirror of REQUIRED_SUBSTRING in scripts/utils/secret_patterns.py, embedded for
# the same reason the pattern list is. A pattern that cannot match without a
# literal substring records it; testing the substring first is O(n) and changes
# no verdict. Measured 2026-07-31: without it a 200 KB single-string scan costs
# 81.6s here, with it 0.00223s.
REQUIRED_SUBSTRING = {
    "connection string with inline credentials": "://",
}

# Every allowance below is PATH-SCOPED, with ONE deliberate exception named
# here rather than left for a reader to trip over: the `.env` / `.env.*` branch
# in _secrets_path_allowed is basename-wide by design, because `git check-ignore`
# confirms both repositories ignore those names at any depth, so such a file can
# never be committed and never reaches a wall.
#
# For everything else there is deliberately no basename-wide set any more: one
# existed until 2026-07-31 and allowed prevent-secrets.py, secret-scanner.py and
# .env.example anywhere in EITHER repository, which was measured against the
# live gate with a planted key in outputs/scratch/, knowledge/ and  # leak-guard: ok (history: where the 2026-07-31 decoys were planted)
# crm/contacts/ — all three written successfully. The scanner side (SKIP_PATHS in  # leak-guard: ok (history: where the 2026-07-31 decoys were planted)
# scripts/secret-scanner.py) already carried repo-relative paths for the same
# three files; the WORKSPACE_PATHS set is the hook's alignment with it. The
# directory set below has no scanner counterpart at all, so it is anchored on its
# own terms.
#
# Every file below is allowed WORKSPACE-EXACT, never by containing directory.
# `scripts/`, `.claude/hooks/` and `scripts/utils/` are all ordinary creatable
# directory names, so a segment match leaves a decoy — outputs/scratch/scripts/  # leak-guard: ok (history: measured decoy paths)
# secret-scanner.py, outputs/scratch/.claude/hooks/_dispatch.py,  # leak-guard: ok (history: measured decoy paths)
# outputs/scratch/scripts/utils/secret_patterns.py — unscanned, which is a  # leak-guard: ok (history: measured decoy paths)
# smaller version of the basename-anywhere hole this file closed on the same
# day. The first narrowing reached only the `scripts/` entry; the other two
# stayed directory-scoped until the decoys were measured against the live gate
# and written successfully. The one case the wider scope also covered, a nested
# scripts/utils/nested/secret_patterns.py, exists nowhere in the tree.
#
# These are exact repo-relative paths, matching the scanner's own SKIP_PATHS
# rather than widening past it. _is_workspace_file honours the relative form and
# THIS workspace's absolute form; a foreign absolute root is not this file.
#
# Why each is exempt: secret_patterns.py holds the vocabulary by definition and
# would self-trigger on every edit, and _dispatch.py embeds a copy of it for the
# same reason. secret-scanner.py has held ZERO re.compile calls since the
# vocabulary moved out of it, so its "same pattern catalog" justification stopped
# being true; it is kept because the scanner skips it by path and the two walls
# agreeing is worth more than removing an allowance that costs nothing.
SECRETS_ALLOW_WORKSPACE_PATHS = {
    "scripts/secret-scanner.py",
    "scripts/utils/secret_patterns.py",
    ".claude/hooks/_dispatch.py",
}
# Directory allow-list, anchored to THIS workspace's own directory, not to any
# path that happens to contain the segment.
#
# Segment-anchoring alone was not enough, and this is the same hole one level up
# from the basename set above. `tests/security/` and `.sessions/` are ordinary
# creatable directory names, so while the old rule accepted any path merely
# CONTAINING the segment, a decoy at outputs/scratch/tests/security/planted.py,  # leak-guard: ok (history: measured decoy paths)
# knowledge/tests/security/planted.md, outputs/scratch/.sessions/planted.json or  # leak-guard: ok (history: measured decoy paths)
# crm/contacts/.sessions/planted.md went unscanned. All four were measured  # leak-guard: ok (history: measured decoy paths)
# against the live gate and written successfully. The data-overlay case is the
# live one: `git check-ignore` returns 1 for outputs/reports/tests/security/, so  # leak-guard: ok (history: the measured check-ignore probe)
# such a decoy would be TRACKED, and unlike the basename hole the scanner's
# SKIP_PATHS carries no counterpart for either directory at all.
SECRETS_ALLOW_DIR_SEGMENTS = [
    ".sessions/",                      # OAuth tokens, Telegram sessions
    "tests/security/",                 # Security test fixtures
]

def _under_dir(normalized: str, segment: str) -> bool:
    """True only for THIS workspace's own `segment` directory, relative or absolute.

    Anchored at the workspace root, not merely at a `/`. A look-alike like
    `mytests/security/x` was already excluded by the older segment anchoring;
    what that anchoring still admitted was a REAL `tests/security/` nested
    anywhere, e.g. a `tests/security/x.py` under a data directory. See the comment on
    SECRETS_ALLOW_DIR_SEGMENTS for the measurement.
    """
    return normalized.startswith((segment, WORKSPACE.as_posix() + "/" + segment))


def _is_workspace_file(normalized: str, rel: str) -> bool:
    """True only for THIS workspace's own copy of `rel`, absolute or relative."""
    return normalized == rel or normalized == (WORKSPACE / rel).as_posix()


def _secrets_path_allowed(file_path: str) -> bool:
    # Normalize FIRST so a Windows-style backslash path resolves its basename
    # correctly even on Linux (os.path.basename does not split on "\" off Windows).
    normalized = file_path.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    if any(_is_workspace_file(normalized, rel) for rel in SECRETS_ALLOW_WORKSPACE_PATHS):
        return True
    # Exact .env basename set only — `.env` and dotted variants (`.env.local`,
    # `.env.production`), but NOT look-alikes like `.envil` or `.environment`.
    #
    # `.env.example` is carved OUT, and it is the one exception worth stating.
    # The rest of this branch exempts the gitignored files that legitimately
    # hold live credentials; a `.example` template holds placeholders by
    # definition, so it needs no exemption, and the old basename-wide entry let
    # a planted key be written to a `.env.example` in any directory. Scanning it
    # costs nothing (measured: the real template is clean) and a real credential
    # appearing in one is a finding rather than a false positive. This is
    # STRICTER than the scanner's SKIP_PATHS, which still skips the repo-root
    # `.env.example`; the difference is deliberate and in the safe direction.
    if basename != ".env.example" and (basename == ".env" or basename.startswith(".env.")):
        return True
    return any(_under_dir(normalized, seg) for seg in SECRETS_ALLOW_DIR_SEGMENTS)


def _registry_cookie_path() -> Path | None:
    """The headless browser's cookie jar, absolute, from the registry name `browser-cookies`.

    The one file this gate exempts by address, compared as a resolved absolute
    path so it follows the registry wherever the jar moves. Asked only once a
    secret has MATCHED, so an ordinary write never pays for the import. A
    registry that cannot answer (a public clone has none) exempts nothing, which
    is the safe direction, and says so on stderr.
    """
    try:
        from scripts.utils.data_path import data_path
        return data_path("browser-cookies").resolve()
    except Exception as exc:  # noqa: BLE001 - a hook must not crash the tool call; logged
        print(f"[secrets-gate] browser-cookies unresolved, no extra exemption: {exc}",
              file=sys.stderr)
        return None


def _is_cookie_jar(file_path: str, cwd: str) -> bool:
    """True when `file_path`, resolved against the call's cwd, IS the registry's cookie jar."""
    jar = _registry_cookie_path()
    if jar is None:
        return False
    target = Path(file_path.replace("\\", "/")).expanduser()
    if not target.is_absolute():
        target = Path(cwd or WORKSPACE) / target
    try:
        return target.resolve() == jar
    except (OSError, RuntimeError) as exc:
        print(f"[secrets-gate] {file_path!r} unresolvable, no exemption: {exc}",
              file=sys.stderr)
        return False

def _scan_for_secrets(text: str) -> tuple[bool, str | None]:
    if not text:
        return False, None
    for pattern, desc in SECRET_PATTERNS:
        needle = REQUIRED_SUBSTRING.get(desc)
        if needle is not None and needle not in text:
            continue
        if pattern.search(text):
            return True, desc
    return False, None

def check_prevent_secrets(payload: dict) -> dict | None:
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    if tool_name == "Bash":
        # Bash payloads have no file_path; scan the command string only.
        command = tool_input.get("command", "")
        matched, desc = _scan_for_secrets(command)
        if matched:
            return {
                "decision": "block",
                "reason": (
                    f"BLOCKED: Detected {desc} in Bash command. "
                    f"Secrets must NEVER appear in commands. "
                    f"Use environment variables from .env instead."
                ),
            }
        return None
    # All four write shapes the dispatcher is registered for, not just the two
    # that carry their text at the top level. MultiEdit keeps every replacement
    # in edits[i]["new_string"] and NotebookEdit names its target notebook_path
    # and its text new_source, so both walked past this gate untouched until
    # 2026-07-31: the notebook returned at the empty-path guard below, the
    # MultiEdit scanned an empty string. check_protect_personal_threads
    # destructures the same four correctly, and it is the fix for a prior
    # instance of this bug — the shape is copied from it deliberately.
    file_path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "") or ""
    if not file_path or _secrets_path_allowed(file_path):
        return None
    parts = [
        tool_input.get("content") or "",
        tool_input.get("new_string") or "",
        tool_input.get("new_source") or "",
    ]
    for edit in (tool_input.get("edits") or []):
        if isinstance(edit, dict):
            parts.append(edit.get("new_string") or "")
    matched, desc = _scan_for_secrets("\n".join(parts))
    if matched and _is_cookie_jar(file_path, payload.get("cwd") or ""):
        return None
    if matched:
        basename = os.path.basename(file_path)
        return {
            "decision": "block",
            "reason": (
                f"BLOCKED: Detected {desc} in content being written to {basename}. "
                f"Secrets must NEVER be written to workspace files. "
                f"Store API keys in .env (loaded via load_api_key() from scripts/utils/api.py). "
                f"Store passwords in a password manager. "
                f"If this is a false positive, the file may need to be added to the "
                f"allow-list in .claude/hooks/_dispatch.py."
            ),
        }
    return None

# ============================================================
# check_protect_personal_threads — block leaks of personal-thread content
# ============================================================

PERSONAL_PATH_RE = re.compile(r"threads[/\\]personal[/\\]", re.IGNORECASE)

# The directory itself, a path inside it and the archived copies: one copy of
# each pattern, in `scripts/utils/personal_paths.py` (since 2026-09-11).
from scripts.utils.personal_paths import (  # noqa: E402
    names_personal_archive as _names_personal_archive,
)
from scripts.utils.personal_paths import (  # noqa: E402
    canonicalise_paths as _canonicalise_paths,
)
from scripts.utils.personal_paths import (  # noqa: E402
    names_personal_threads as _names_personal_threads,
)
from scripts.utils.personal_paths import (  # noqa: E402
    names_personal_home as _names_personal_home,
)
from scripts.utils.personal_paths import (  # noqa: E402
    personal_home_dir as _personal_home_dir,
)
from scripts.utils.personal_paths import (  # noqa: E402
    personal_home_fragment as _personal_home_fragment,
)
from scripts.utils.personal_paths import PERSONAL_HOME_NAME  # noqa: E402


# A glob segment that is not a plain directory name. `*`, `?` and `[abc]` can
# each expand to `personal`, so a segment carrying one is treated as if it might.
_GLOB_META_RE = re.compile(r"[*?\[]")


def _segment_can_be(segment: str, name: str) -> bool:
    """Can ONE glob segment expand to the literal directory `name`?"""
    if segment.lower() == name:
        return True
    if not _GLOB_META_RE.search(segment):
        return False
    return fnmatch.fnmatch(name, segment.lower())


def _tail_reaches_personal(tail: list[str]) -> bool:
    """Can these segments, taken below the threads root, reach a CEO-only one?

    The three shapes are `<threads>/personal/...`,
    `<threads>/archive/<anything>/personal/...` and, since the C3 subject-tree
    archive (2026-10-13), `<threads>/<year>/personal/...`; all are produced by
    `scripts/thread.py`. Anything else under `threads/` is business content and
    must stay searchable, which is why this asks about those shapes rather than
    refusing every `**`.
    """
    if not tail:
        return True                       # the threads root itself, read whole
    head, rest = tail[0], tail[1:]
    if head == "**":
        return True                       # crosses any depth, so it crosses these
    if _segment_can_be(head, "personal"):
        return True
    if _segment_can_be(head, "archive"):
        if not rest or rest[0] == "**":
            return True                   # the whole archive, or any depth in it
        return not rest[1:] or _segment_can_be(rest[1], "personal")
    if re.fullmatch(r"\d{4}", head):
        if not rest or rest[0] == "**":
            return True                   # the whole year, or any depth in it
        return _segment_can_be(rest[0], "personal")
    return False


def _main_clone_sibling_data() -> Path:
    """`<main clone>-data`, with the main clone found the way git records it.

    In the main clone this is `WORKSPACE.parent / f"{WORKSPACE.name}-data"`,
    unchanged. In a YARD -- a worktree, whose `.git` is a FILE pointing into the
    main clone's `.git/worktrees/<name>` -- that expression named a sibling of
    the WORKTREE, two levels down, and answered with a path that has never
    existed on any machine.

    `--git-common-dir` is the same question `scripts/utils/clone_guard.py` asks;
    its answer is the main clone's `.git`, whose parent is the main clone.

    It is asked ONLY when this directory is ITSELF a worktree, and that guard is
    the correctness of the function rather than an optimisation. `git -C X
    rev-parse` answers about the nearest repository AT OR ABOVE X, so from a
    directory that is not a checkout it reports an ANCESTOR's main clone.
    MEASURED 2026-09-09 with `WORKSPACE` pointed at a fresh EMPTY directory
    created under this worktree: the fallback returned the operator's live
    `.heading-os-data` and `_threads_roots()` returned its real `threads/`, out
    of a directory holding nothing. That is the `CLAUDE.md` rule "derive the
    tree a guard looks at from the current checkout, never from the common git
    directory" broken by the code written to obey it, and the bidirectional
    check written alongside it caught its own author. That check was parked in
    the 2026-09-21 test-suite split; the measurement above is what remains, so
    re-read it before touching the fallback.

    `scripts/utils/paths.engine_siblings_root()` carries the same guard; this
    copy was written without it, which is the fix-in-one-of-N-copies shape. The
    two cannot be merged into one: this branch exists precisely because
    importing that module is what failed.

    A main clone (`.git` is a DIRECTORY) keeps the old answer and spawns
    nothing. Anything else, and any git that cannot answer, falls back to this
    checkout's own sibling: the pre-2026-09-09 behaviour, and the safe
    direction, since a base that does not exist is dropped below, never
    invented.
    """
    # Imported here, not at module scope: this file is loaded on EVERY tool call
    # and pays for every top-level import, while this branch is the rare one.
    import subprocess

    root = WORKSPACE
    try:
        if not (root / ".git").is_file():
            return root.parent / f"{root.name}-data"
    except OSError:
        return root.parent / f"{root.name}-data"

    try:
        # `errors="replace"`, never a bare `text=True`: a byte git's output
        # cannot decode would raise `UnicodeDecodeError`, which is a ValueError
        # and is caught by NO handler here. One such byte would end this hook,
        # and a PreToolUse hook that dies takes its wall with it. Three hooks
        # share that exposure and a check held all three until the 2026-09-21
        # test-suite split parked it; the `errors="replace"` is the guard, and
        # it is now only as safe as the next reader of this comment.
        proc = subprocess.run(
            ["git", "-C", str(WORKSPACE), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, errors="replace", timeout=5)
        common = proc.stdout.strip()
        if proc.returncode == 0 and common:
            root = Path(common).parent
    except (OSError, subprocess.SubprocessError):
        pass
    return root.parent / f"{root.name}-data"


def _threads_homes() -> list[str]:
    """Every path, relative to a base, that thread records can sit at.

    Only `threads`: the HEADING OS 2 `<family>/<leaf>/threads` homes this list
    also carried were never created, and the C3 subject-tree homes are walled by
    registry name in `_threads_roots` instead.
    """
    return ["threads"]


def _threads_roots() -> list[Path]:
    """Every threads directory a sweep on this machine could descend into.

    Only directories that EXIST are returned. The engine clone ships no threads
    at all, which is what makes the unanchored-sweep carve-out below safe, and
    an entry for a directory that is not there would take that carve-out away
    for no gain.

    Each base is crossed with `_threads_homes()`, and the threads directories
    the registry names are added as they are. `_search_reaches_personal` walks
    path SEGMENTS and anchors on a literal `threads` wherever it sits.
    """
    global _THREADS_ROOTS_CACHE
    if _THREADS_ROOTS_CACHE is not None:
        return _THREADS_ROOTS_CACHE
    bases = [WORKSPACE]
    # `get_data_root()` already honours the `HEADING_OS_DATA` pin, so reading
    # the variable here as well was a second answer to a question that has one.
    # Mutation-checked 2026-08-29: removing the env branch changed no verdict.
    try:
        from scripts.utils.workspace import get_data_root
        bases.append(get_data_root())
    except Exception:  # pragma: no cover - resolver unavailable
        # Fail toward the wall, not away from it: guess the sibling layout
        # rather than conclude there is no threads directory anywhere.
        #
        # The sibling of the MAIN CLONE, never of this checkout. A YARD is a
        # worktree two levels below it, so `WORKSPACE.parent / f"{name}-data"`
        # named `<branch>-data` there, a directory that does not exist, and the
        # overlay's `threads/` silently left the wall's list. Every path that
        # asks this question in this file already resolves HELM first
        # (`_yard_data_roots`), and this one did not.
        #
        # Asked of git rather than assembled from `..`, and by the same question
        # `scripts/utils/clone_guard.py` asks, because the branch this sits in
        # is the one where importing that module is what failed.
        bases.append(_main_clone_sibling_data())
    roots = []
    # C3: the threads directories at their REGISTRY addresses as well, so a
    # subject-tree home the derived list below cannot know is still a root.
    try:
        from scripts.utils.data_path import data_path
        for named in (data_path("threads"), data_path("threads-archive")):
            if named.is_dir() and named.resolve() not in roots:
                roots.append(named.resolve())
    except Exception as exc:  # noqa: BLE001 (said; the derived homes still apply)
        print(f"dispatch: threads registry names unresolved ({type(exc).__name__}: "
              f"{exc}); only the derived threads homes are walled", file=sys.stderr)
    homes = _threads_homes()
    for base in bases:
        try:
            resolved = Path(base).resolve()
        except OSError:  # pragma: no cover - unresolvable base
            continue
        for home in homes:
            candidate = resolved.joinpath(*home.split("/"))
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
    _THREADS_ROOTS_CACHE = roots
    return roots


_THREADS_ROOTS_CACHE: list[Path] | None = None


def _expression_can_reach(expression: list[str], rel: list[str]) -> bool:
    """Can these glob segments walk down `rel`, then into a CEO-only subtree?

    `rel` is the threads root stated relative to the search root. Once it is
    consumed the ordinary tail question applies.
    """
    if not rel:
        return _tail_reaches_personal(expression)
    if not expression:
        return False
    head, rest = expression[0], expression[1:]
    if head == "**":
        return True                       # crosses any depth, so it crosses rel
    if _segment_can_be(head, rel[0].lower()):
        return _expression_can_reach(rest, rel[1:])
    return False


def _sweep_descends_from_above(tool_name: str, fields: dict, cwd: str) -> bool:
    """Can a sweep rooted ABOVE the threads tree descend into the CEO-only part?

    The anchor test below only fires on a LITERAL `threads` segment, so a search
    rooted one directory higher carried no such segment and was allowed. That is
    the plainest way to sweep the private corpus, and the shape this workspace
    instructs, because agent threads reset cwd and absolute paths are the fix.
    Measured 2026-08-29: `Grep(path=<data-root>)`, `Glob("**/*.md",
    path=<data-root>)` and a Grep two levels up were all allowed while
    `Grep(path=<data-root>/threads)` was refused.

    The comment on the unanchored carve-out justified itself with "an unanchored
    sweep stays inside the engine clone, which holds no threads at all". That
    premise is now checked rather than assumed: the question asked here is
    whether the resolved search root is an ancestor of a threads directory that
    actually exists.
    """
    raw_path = fields["path"]
    try:
        root = Path(raw_path).resolve() if raw_path else Path(cwd or ".").resolve()
    except OSError:  # pragma: no cover - unresolvable root
        return False
    for threads_root in _threads_roots():
        try:
            rel = threads_root.relative_to(root).parts
        except ValueError:
            continue                      # not below this search root
        if not rel:
            continue                      # the root IS the threads dir: anchored
        if tool_name == "Grep":
            # ripgrep walks the whole tree under `path`, and applies `--glob` at
            # any depth rather than as a ceiling, so it descends either way.
            return True
        if _expression_can_reach(fields["pattern"].split("/"), list(rel)):
            return True
    return False


def _search_reaches_personal(segments: list[str]) -> bool:
    """Can a search described by these path segments reach the CEO-only threads?

    The wall used to ask "does this argument SPELL the directory", and a
    wildcard never spells it: a Glob that sweeps the whole threads tree with a
    double star walked straight through and put private thread filenames in the
    transcript. Measured 2026-08-29, 7 of 13 verdicts wrong. This asks the
    question that matters instead - can the expression EXPAND to something
    inside the subtree.

    Only a LITERAL `threads` segment anchors the search. A wildcard that could
    itself expand to `threads` is not treated as an anchor, because then
    `Glob("**/*.py")` is refused and that is every ordinary sweep in the engine.

    That limit is deliberate, and what makes it safe is `_sweep_descends_from_above`
    one screen up - NOT the sentence that used to stand here, which read: "only a
    `threads`-prefixed path is re-anchored at the data root by
    data-path-redirect.py, so an unanchored sweep stays inside the engine clone,
    which holds no threads at all." That premise was measured FALSE and retired.
    A Glob with no `path` resolves against `cwd`, the redirect says nothing about
    where `cwd` points, and with `cwd` at the data root the sweep descends
    straight into the threads tree.

    The sentence stayed here after the check that replaced it landed, which is
    the dangerous half of a stale comment: it told the next reader the carve-out
    was self-justifying, so the sibling rule carrying the actual load reads as
    redundant and invites deletion. `_sweep_descends_from_above` resolves the
    real search root and refuses when that root sits above a threads tree that
    exists. Do not remove it.
    """
    # `..` was left in place here while `""` and `.` were dropped, so
    # `Grep(path="threads/business/../personal")` found no `personal` segment  # leak-guard: ok (history: the call that got through)
    # directly after `threads` and was allowed. The shared collapse pops it.
    parts = normalize_segments("/".join(segments))
    parts = [s for s in parts if s]
    for index, segment in enumerate(parts):
        if segment.lower() == "threads":
            return _tail_reaches_personal(parts[index + 1:])
    return False


def _glob_can_enter(expression: list[str], target: list[str], *, descends: bool) -> bool:
    """Can these segments walk down to `target` or below it?

    Every file under the personal threads' home is CEO-only, so reaching the
    directory is the whole question; there is no business part below it to
    keep searchable. `descends` says whether the search continues below where
    the expression ends (Grep does; a Glob lists only what its pattern names).
    """
    for i, head in enumerate(expression):
        if i == len(target):
            return True                   # inside the home
        if head == "**":
            return True                   # crosses any depth, so it crosses the home
        if not _segment_can_be(head, target[i].lower()):
            return False
    return descends or len(expression) >= len(target)


def _search_reaches_personal_home(tool_name: str, fields: dict, cwd: str) -> bool:
    """Can this Grep or Glob reach the personal threads at their registry home?

    HOS-235. C3 moved that home under the personal subject root, where no
    `threads` segment comes before `personal`, so `_search_reaches_personal`
    and `_sweep_descends_from_above` cannot see it: they anchor on the old
    order. This asks about the resolved directory instead, from wherever the
    search really starts (its `path`, else `cwd`, else an absolute pattern).
    The home must exist, the same carve-out `_threads_roots` makes: a directory
    that is not there holds nothing to leak, and walling it would refuse every
    search of the data root's personal subject tree for no gain.
    """
    import posixpath

    home = _personal_home_dir()
    if home is None or not home.is_dir():
        return False
    pattern = fields["pattern"] if tool_name == "Glob" else ""
    try:
        base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        start = (base / fields["path"]).resolve() if fields["path"] else base
    except OSError:  # pragma: no cover - unresolvable root
        return False
    joined = posixpath.join(start.as_posix(), pattern) if pattern else start.as_posix()
    expression = [s for s in posixpath.normpath(joined).split("/") if s]
    target = [s for s in home.as_posix().split("/") if s]
    return _glob_can_enter(expression, target, descends=(tool_name == "Grep"))


_BUSINESS_DIRS_CACHE: list[tuple[str, ...]] | None = None


def _business_threads_dirs() -> list[tuple[str, ...]]:
    """The segments of every existing directory of the registry group `business-threads`.

    HOS-236. Since C3 every business-thread home is a directory named `threads`
    (`threads-business`, each `account-threads`, `system-threads`), so the
    anchor in `_search_reaches_personal` took a search of any of them for a
    search of a whole threads root and refused it. A failure to resolve the
    group is said on stderr and answers with no directory, which leaves every
    search to the anchored test, so the wall stays as tight as it was.
    """
    global _BUSINESS_DIRS_CACHE
    if _BUSINESS_DIRS_CACHE is None:
        try:
            from scripts.utils.threads_lib import business_thread_dirs
            dirs = business_thread_dirs()
        except Exception as exc:  # noqa: BLE001 (said; the anchored test still refuses)
            print(f"dispatch: 'business-threads' unresolved ({type(exc).__name__}: {exc}); "
                  f"a search of a business-thread directory is judged as a threads root",
                  file=sys.stderr)
            dirs = []
        _BUSINESS_DIRS_CACHE = [tuple(s for s in d.resolve().as_posix().split("/") if s)
                                for d in dirs]
    return _BUSINESS_DIRS_CACHE


def _resolved_search(tool_name: str, fields: dict, cwd: str) -> list[str]:
    """The absolute segments a Grep or Glob will expand, `..` collapsed.

    Resolved from where the search really starts, as in
    `_search_reaches_personal_home`. A Grep descends below its `path` whatever
    its `glob` says, so its expression is `path`, then `**`, then the `glob`
    filter, the shape the anchored test builds. An unresolvable start
    answers with no segments, which no business directory contains.
    """
    import posixpath

    try:
        base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        start = (base / fields["path"]).resolve() if fields["path"] else base
    except OSError:  # pragma: no cover - unresolvable root
        return []
    if tool_name == "Glob" and fields["pattern"]:
        joined = posixpath.normpath(posixpath.join(start.as_posix(), fields["pattern"]))
    else:
        joined = start.as_posix() + "/**"
        if tool_name == "Grep" and fields["glob"]:
            joined += "/" + fields["glob"]
    return [s for s in joined.split("/") if s]


def _confined_to_business_threads(expression: list[str]) -> bool:
    """Is a search over these ABSOLUTE segments confined to one business-thread directory?

    True only when the expression's literal part (everything before the first
    wildcard) lies at or below a member of `business-threads`, nothing below
    that member is spelled `personal`, and, when a wildcard or a recursive
    search can range below it, no directory whose name carries `personal`
    exists there on disk. A wildcard can only reach what exists, and every
    member is a business directory the operator already reads freely, so it
    is walked, never a personal one. Anything else is left to the anchored test.
    """
    literal: list[str] = []
    for segment in expression:
        if segment == "**" or _GLOB_META_RE.search(segment):
            break
        literal.append(segment)
    for member in _business_threads_dirs():
        if tuple(literal[:len(member)]) != member:
            continue
        rest = expression[len(member):]
        if any("personal" in s.lower() for s in rest):
            return False
        if len(literal) < len(expression):
            root = Path("/", *member)
            for _dirpath, dirnames, _files in os.walk(root):
                if any("personal" in d.lower() for d in dirnames):
                    return False
        return True
    return False

# Order of patterns is irrelevant for correctness (any match blocks).
# The list follows the original protect-personal-threads.py order to
# preserve git blame lineage. Adding new patterns: append to the end
# or group with related shell-builtin / language-specific variants.
#
# One spelling of "either CEO-only threads directory", shared by every pattern
# below. Until 2026-08-29 the archive shape was pasted onto exactly two of them
# (`cp` and `git add`), so `cp threads/archive/2026/personal/x.md` was refused  # leak-guard: ok (history: the command the old patterns refused)
# while `cat` of the same file was allowed - the wall gave two answers about one
# file. A fragment cannot drift the way a hand-copied clause did.
#
# C3 (2026-10-13): the subject-tree archive, `archive/threads/<year>/personal`,  # leak-guard: ok (guard vocabulary: the shape the regex below matches)
# puts `personal` after a YEAR directly under `threads`; matched by that shape
# (`scripts/utils/personal_paths.py` carries the same one for the read side).
_BASH_CEO_THREADS = r"threads[/\\](?:personal|archive[/\\][^\s'\"]+[/\\]personal|\d{4}[/\\]personal)"

def _bash_patterns(target: str) -> list[re.Pattern[str]]:
    """The verbs that move or print a file, each paired with `target`.

    One template list, so the personal threads' registry home (HOS-235) is
    refused by exactly the verbs the static shapes are.
    """
    return [
        re.compile(rf"\b(cp|mv|rsync|scp|xcopy|robocopy)\b.*{target}", re.IGNORECASE),
        re.compile(rf"\b(tar|zip|7z|gzip)\b.*{target}", re.IGNORECASE),
        re.compile(rf"\bcat\b.*{target}.*>", re.IGNORECASE),
        re.compile(rf"\bgit\s+(add|stash\s+push)\b.*{target}", re.IGNORECASE),
        re.compile(rf"<\s*{target}", re.IGNORECASE),
        re.compile(rf"\btee\b.*{target}", re.IGNORECASE),
        re.compile(rf"\bcat\b.*{target}.*\|\s*tee", re.IGNORECASE),
        re.compile(rf"\bdd\b.*\bif={target}", re.IGNORECASE),
        re.compile(rf"\bcd\b.*{target}", re.IGNORECASE),
        re.compile(rf"\b(Copy-Item|Move-Item|Get-Content)\b.*{target}", re.IGNORECASE),
        re.compile(rf"\bshutil\.(copy|copy2|move|copytree)\b.*{target}", re.IGNORECASE),
        re.compile(rf"\bopen\s*\(\s*['\"]{target}", re.IGNORECASE),
        # Read-then-emit exfil: a plain read utility pointed at the personal subtree
        # dumps CEO-only content into the transcript (a leak by itself, no redirect
        # needed). Added 2026-06-09 audit (hooks finding 2 — guard was narrower than
        # secure-projects.md/security.md claim of technical enforcement).
        #
        # THIS IS A DENY-LIST, and a deny-list of utility names can never be
        # complete. An unlisted reader passes: `busybox cat`, a shell function, a
        # compiled helper, a name invented after this line was written. Read the
        # alternation as "the common ones are refused", never as "reading is
        # impossible". The structural alternative is a default-deny on any Bash
        # command naming that directory, and it is the operator's decision, not this
        # file's.
        #
        # `cat` was missing from 2026-06-09 to 2026-08-27. The other two `cat`
        # patterns above both require a redirect or a pipe to tee, so `head` on a
        # personal thread was refused while the plainest read of the same file was
        # allowed. Added with its neighbours: tac, rev, sort, uniq, shuf, paste, pr,
        # fmt, expand, unexpand, column, tr, hexdump.
        re.compile(
            r"\b(cat|tac|head|tail|sed|awk|base64|b64encode|xxd|hexdump|od|strings|"
            r"nl|fold|cut|less|more|grep|rg|rev|sort|uniq|shuf|paste|pr|fmt|expand|"
            rf"unexpand|column|tr)\b.*{target}",
            re.IGNORECASE),
        re.compile(rf"\bopen\s*\(\s*['\"][^'\"]*{target}", re.IGNORECASE),
    ]


DANGEROUS_BASH_PATTERNS = _bash_patterns(_BASH_CEO_THREADS)

_NAMED_BASH_CACHE: list[re.Pattern[str]] | None = None


def _named_bash_patterns() -> list[re.Pattern[str]]:
    """The same verbs over the registry name `threads-personal`.

    C3 put the personal threads under the personal subject root, so the
    segments `_BASH_CEO_THREADS` looks for arrive in the other order there and
    only the registry knows the address. Built lazily, once per process, and
    only for a command that names the home at all; with no address the list is
    empty and `personal_paths` has already said why on stderr.
    """
    global _NAMED_BASH_CACHE
    if _NAMED_BASH_CACHE is None:
        fragment = _personal_home_fragment()
        _NAMED_BASH_CACHE = _bash_patterns(fragment) if fragment else []
    return _NAMED_BASH_CACHE


# HOS-238. The patterns above read the command's TEXT, so a read that reaches a
# CEO-only file through a change of directory and a relative path carried no
# shape for them to find: `cd <personal subject root> && cat <relative path>`,
# its `pushd`, subshell, `git -C`, `tar -C` and `env -C` spellings, and a bare
# `cat <file>` issued from inside the home all went through. MEASURED
# 2026-10-13 on invented file names: 19 of 22 such shapes allowed. The answer
# below rewrites each command segment once per directory it can run in, with
# every relative word made absolute, and hands those rewrites to the SAME
# patterns, so the verbs refused stay one list.
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"})
_CHDIR_FLAGS = frozenset({"-C", "--directory", "--chdir"})
# A word the shell computes, which this file cannot read.
_OPAQUE_WORD_RE = re.compile(r"[$`]")
# What a word that may land in a CEO-only directory is replaced by. It is no
# path at all, so no gate reads it as an address, and `_REACH_PATTERNS` pair it
# with the same verbs the path shapes are paired with.
_REACH_MARKER = "@@HOS238-CEO-ONLY-REACHED@@"
_REACH_PATTERNS = _bash_patterns(re.escape(_REACH_MARKER))
_MAX_PLACES = 16


def _shell_words(command: str) -> list[str]:
    """The words and operators of a command, quotes removed.

    A backtick and `$(` open a command the shell runs, so each becomes a
    separator here. A command that does not tokenise (an unbalanced quote) is
    split on whitespace instead, and said on stderr; the shell would refuse to
    run it, so the cruder split costs nothing.
    """
    import shlex

    text = command.replace("`", " ; ").replace("$(", " ( ").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        print(f"dispatch: command not tokenised ({exc}); judged word by word",
              file=sys.stderr)
        return text.split()


def _absolute_word(word: str, place: str) -> str:
    """`word` with a relative path made absolute against `place`; anything else as it is."""
    import posixpath

    if not word or set(word) <= set("<>&|0123456789"):
        return word                       # a redirection operator or a descriptor
    if word.startswith("-") or re.match(r"[A-Za-z_][\w-]*=", word):
        key, sep, value = word.partition("=")
        if not sep or not value:
            return word
        return key + sep + _absolute_word(value, place)
    if _OPAQUE_WORD_RE.search(word):
        return word
    if word.startswith("~"):
        word = os.path.expanduser(word)
    if word.startswith("/"):
        return posixpath.normpath(word)
    return posixpath.normpath(posixpath.join(place, word))


def _word_may_reach(word: str) -> bool:
    """Could this absolute word, wildcards expanded, name a CEO-only path?

    The literal spellings are the patterns' job. This answers for a wildcard,
    which spells nothing: against the registry home, and against the static
    shapes unless the word is confined to a business-thread directory.
    """
    if not word.startswith("/") or not (_GLOB_META_RE.search(word) or "**" in word):
        return False
    segments = [s for s in word.split("/") if s]
    home = _personal_home_dir()
    if home is not None:
        target = [s for s in home.as_posix().split("/") if s]
        if _glob_can_enter(segments, target, descends=False):
            return True
    return _search_reaches_personal(segments) and not _confined_to_business_threads(segments)


def _cd_target(segment: list[str]) -> "tuple[bool, str | None]":
    """(is this segment a `cd`/`pushd`, its directory argument or None for none)."""
    words = list(segment)
    while words and (re.match(r"[A-Za-z_]\w*=", words[0]) or words[0] in ("builtin", "command")):
        words = words[1:]
    if not words or words[0] not in ("cd", "pushd"):
        return False, None
    args = [w for w in words[1:] if w not in ("-L", "-P", "-e", "-@", "--")]
    return True, (args[0] if args else None)


_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")


def _expand_known(word: str, known: dict) -> str:
    """`word` with every `$NAME` / `${NAME}` that `known` holds replaced; the rest left."""
    if "$" not in word:
        return word
    return _VAR_RE.sub(lambda m: known.get(m.group(1) or m.group(2), m.group(0)), word)


def _bash_resolved_haystacks(command: str, cwd: str, depth: int = 0,
                             env: "dict | None" = None) -> list[str]:
    """`command` again, once per directory it can run in, every relative path made absolute.

    The directories are every one the command stands in at any point: the
    session's `cwd`, every `cd` or `pushd` target, and every `-C`,
    `--directory` or `--chdir` value, never removed. Each rewrite applies ONE
    of them to the whole command, separators kept, so a verb and a path that a
    separator splits (`cat $(echo <relative path>)`) still meet in one string.
    That over-approximates on purpose. A subshell, `popd` and `cd -` need no
    model of their own, because each returns to a directory already in the
    set, and the cost is a refusal of a command that leaves a CEO-only
    directory and then names, relative to the wrong one, a path inside it.

    A variable the command assigns literally, or the environment holds, is
    substituted first. A directory the command otherwise computes (`cd "$X"`
    with `X` unknown, `cd $(...)`) cannot be read.
    Once one appears, a relative word that could reach a CEO-only directory
    under SOME value of it (naming a `threads` or `personal` segment, or
    climbing with `..`) is replaced by a stand-in the patterns refuse; so is
    every relative word when the command also spells `personal` inside a path,
    a variable or an assignment, or names the home's registry name. A bare
    file name after such a `cd`, in a command that spells nothing personal, is
    let through: that is the residual, said here rather than left to be found.

    A quoted word with a space in it may be a command run by `bash -c`, `eval`
    or `ssh`, so it is read as one too, to a depth of three.
    """
    import posixpath

    start = posixpath.normpath(cwd) if cwd and cwd.startswith("/") else os.getcwd()
    words = _shell_words(command)
    # A variable the command assigns a literal value, or one the environment
    # holds, is read rather than treated as computed: `D=<dir>; cd $D` and
    # `cat $D/<relative path>` are the same read as the spelled-out path.
    known = dict(env) if env is not None else dict(os.environ)
    for w in words:
        m = re.fullmatch(r"([A-Za-z_]\w*)=(.*)", w)
        if m and not _OPAQUE_WORD_RE.search(m.group(2)):
            known[m.group(1)] = m.group(2)
    words = [_expand_known(w, known) for w in words]
    places = [start]
    opaque = False

    def add_place(target: str) -> None:
        nonlocal opaque
        if _OPAQUE_WORD_RE.search(target) or _GLOB_META_RE.search(target):
            opaque = True
            return
        bases = list(places)
        if not target.startswith(("/", "~", ".")):
            bases += [d for d in os.environ.get("CDPATH", "").split(":") if d]
        for base in bases:
            new = _absolute_word(target, base)
            if new not in places:
                places.append(new)

    segment: list[str] = []
    for word in words + [";"]:
        if word not in _SHELL_SEPARATORS:
            segment.append(word)
            continue
        for i, w in enumerate(segment):
            if w in _CHDIR_FLAGS and i + 1 < len(segment):
                add_place(segment[i + 1])
            elif w.startswith(("--directory=", "--chdir=")):
                add_place(w.partition("=")[2])
        is_cd, target = _cd_target(segment)
        if is_cd and target != "-":       # `cd -` returns to a directory in the set
            add_place(os.path.expanduser("~") if target is None else target)
        segment = []
    if len(places) > _MAX_PLACES:
        opaque = True                     # more places than are judged: unknown

    # Only a word that LANDS in a CEO-only directory is rewritten, and to the
    # stand-in rather than to its absolute path; every other word stays as
    # typed. Rewriting all of them put a checkout path into the string once per
    # word, and a checkout named like this one carries `cd` and `grep` as
    # words, so every pattern scanned to the end from each of them. MEASURED
    # 2026-10-13: a 227,712-character heredoc took 1.97 s through the hook
    # before this change and did not finish in 100 s with every word rewritten.
    home = _personal_home_dir()
    needles = {"personal"} | ({home.parent.name.lower()} if home is not None else set())
    out: list[str] = []
    for place in places[:_MAX_PLACES]:
        rendered, changed = [], False
        lowered = place.lower()
        for w in words:
            # Every static CEO-only shape has a `personal` segment and the
            # home has its parent's name; a resolved path holds each segment
            # of its place or of its word, so a word that spells neither with
            # its place, and has no wildcard to make one, is left unresolved.
            if not any(n in lowered or n in w.lower() for n in needles) \
                    and not _GLOB_META_RE.search(w):
                rendered.append(w)
                continue
            a = _absolute_word(w, place)
            if _names_personal_threads(a) or _word_may_reach(a):
                # A bare word is kept beside the stand-in: inside the home
                # every bare word lands there, the verb included.
                rendered.append(_REACH_MARKER if "/" in w else f"{w} {_REACH_MARKER}")
                changed = True
            else:
                rendered.append(w)
        if changed:
            out.append(" ".join(rendered))
    if opaque:
        spells_personal = PERSONAL_HOME_NAME in command.lower() or any(
            "personal" in w.lower() and any(c in w for c in "/$=") for w in words)

        def may_reach_somewhere(w: str) -> bool:
            if (w in _SHELL_SEPARATORS or w.startswith(("-", "/", "~"))
                    or _OPAQUE_WORD_RE.search(w) or set(w) <= set("<>&|0123456789")):
                return False
            parts = w.split("/")
            return (spells_personal or parts[0] == ".."
                    or any(_segment_can_be(p, "threads") or _segment_can_be(p, "personal")
                           for p in parts))
        # A path-like word is replaced; a bare word is kept beside the
        # stand-in, because it may be the verb the patterns pair it with.
        out.append(" ".join(
            (_REACH_MARKER if "/" in w or "." in w else f"{w} {_REACH_MARKER}")
            if may_reach_somewhere(w) else w
            for w in words))
    if depth < 3:
        for w in words:
            # Only a word that names a path or chains commands is read again:
            # inside a CEO-only directory any other word already landed there
            # as a whole in the rewrite above, and quoted prose is most words.
            if " " in w.strip() and any(c in w for c in "/;&|$`"):
                for place in places[:_MAX_PLACES]:
                    out.extend(_bash_resolved_haystacks(w, place, depth + 1, known))
    return out

# Directories whose files legitimately QUOTE a CEO-only path: the specs and
# plans that designed the wall, the scrutiny reports that audit it, the skills
# and rules that name it, and the tests that drive it.
#
# Anchored at the workspace root, not at any `/`. A `(?:^|/)` regex matched the
# segment ANYWHERE, so a decoy directory exempted a write: MEASURED 2026-08-31,
# `outputs/scratch/reference/leak.md`, `knowledge/templates/leak.md` and  # leak-guard: ok (history: measured 2026-08-31 decoys)
# `outputs/scratch/tests/leak.md` were all ALLOWED to carry a `threads/personal/`  # leak-guard: ok (history: measured 2026-08-31 decoys)
# reference, while the control `outputs/reports/leak.md` was blocked. Creating a  # leak-guard: ok (history: measured 2026-08-31 control)
# directory called `reference` is not a privilege.
#
# Same hole shape, and the same fix, as `SECRETS_ALLOW_DIR_SEGMENTS` on
# 2026-07-31. That one was measured and anchored; this allowlist, twelve lines
# away, was left matching a segment anywhere.
ALLOWED_DOC_DIRS = (
    ".claude/skills/",
    ".claude/rules/",
    ".claude/hooks/",
    "reference/",
    "tests/",
)


# The data-repository doc directories, by REGISTRY NAME since the C3
# subject-tree move (2026-10-13), so a move that repoints a name moves the
# carve-out with it. Each name is spelled as a literal at its call in
# `_doc_named_prefixes`, so the leak guard reads it.

_DOC_NAMED_CACHE: list[str] | None = None


def _doc_named_prefixes() -> list[str]:
    """The doc directories by registry name, each as an absolute and a data-relative prefix.

    Fails toward the WALL: a name that does not resolve exempts nothing, and
    says so on stderr. Imported here, not at module scope, because this file
    loads on every tool call and only a doc write that failed every cheaper
    test reaches this.
    """
    global _DOC_NAMED_CACHE
    if _DOC_NAMED_CACHE is not None:
        return _DOC_NAMED_CACHE
    out: list[str] = []
    try:
        from scripts.utils.data_path import data_path
        from scripts.utils.workspace import get_data_root
        root = Path(get_data_root()).resolve()
    except Exception as exc:  # noqa: BLE001 - reported, never raised, in a hook
        print(f"[_dispatch:doc_paths] no data root, so the doc-directory registry "
              f"names exempt nothing: {exc}", file=sys.stderr)
        _DOC_NAMED_CACHE = out
        return out
    named = (
        (lambda: data_path("scrutiny-sheets"), ""),
        (lambda: data_path("scrutiny-reports"), ""),
        (lambda: data_path("spec-docs"), "superpowers/plans/"),
        (lambda: data_path("spec-docs"), "superpowers/specs/"),
        (lambda: data_path("reference-docs"), ""),
        (lambda: data_path("templates"), ""),
    )
    for ask, tail in named:
        try:
            base = ask().resolve()
            rel = base.relative_to(root).as_posix()
        except Exception as exc:  # noqa: BLE001 - reported, never raised, in a hook
            print(f"[_dispatch:doc_paths] a doc-directory name is unresolved, so it "
                  f"exempts nothing: {exc}", file=sys.stderr)
            continue
        out += [f"{base.as_posix()}/{tail}", f"{rel}/{tail}"]
    _DOC_NAMED_CACHE = out
    return out


_DOC_BASES_CACHE: list[str] | None = None


def _doc_bases() -> list[str]:
    """The roots under which one of `ALLOWED_DOC_DIRS` is a real directory.

    BOTH repositories, because the two-part topology can put `reference/` in
    the private overlay and `tests/` and `.claude/` in the engine clone. Anchoring to the engine root alone would have refused a
    legitimate `<data-root>/reference/x.md`, which is the over-tightening a
    narrower fix would have shipped. Same base set, and the same reasoning, as
    `_threads_roots` above.
    """
    global _DOC_BASES_CACHE
    if _DOC_BASES_CACHE is not None:
        return _DOC_BASES_CACHE
    bases = [WORKSPACE.as_posix()]
    try:
        from scripts.utils.workspace import get_data_root
        bases.append(Path(get_data_root()).as_posix())
    except Exception as exc:  # noqa: BLE001 - reported, never raised, in a hook
        # Fail toward the WALL: an unresolvable overlay means one fewer exempt
        # root, so a doc write there is refused rather than silently allowed.
        print(f"[_dispatch:doc_paths] overlay root unresolvable, so only the "
              f"engine tree exempts a doc path: {exc}", file=sys.stderr)
    _DOC_BASES_CACHE = [b.rstrip("/") for b in bases if b]
    return _DOC_BASES_CACHE


def _is_allowed_doc_path(normalized: str) -> bool:
    """True for one of `ALLOWED_DOC_DIRS` inside a root this machine really has.

    Relative spellings are exempt as before. An ABSOLUTE spelling is exempt only
    under one of `_doc_bases()`, so a decoy `reference/` anywhere else is not a
    privilege, and an absolute path into some OTHER workspace entirely is not
    this wall's business to exempt. The data-repository doc directories are
    exempt at their registry addresses, `_doc_named_prefixes`.
    """
    for seg in ALLOWED_DOC_DIRS:
        if normalized.startswith(seg):
            return True
        if any(normalized.startswith(f"{base}/{seg}") for base in _doc_bases()):
            return True
    return any(normalized.startswith(prefix) for prefix in _doc_named_prefixes())

def check_protect_personal_threads(payload: dict) -> dict | None:
    """Block leaks of personal-thread content. Each block carries the
    `_policy_deny: True` flag — the dispatcher's main loop renders these as a
    PreToolUse permission deny (hookSpecificOutput / exit 0), so the CLI shows
    an intentional policy block with its reason, NOT a "hook error". The block
    is just as binding as the exit-2 path; only the presentation differs."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    if tool_name == "Bash":
        command = tool_input.get("command", "") or ""
        # Raw first, then with every path-like run collapsed. The patterns pair
        # a verb with a literal path shape, and a `.`, `//` or `..` segment
        # broke the path half while the verb half still matched: `cat` of a
        # CEO-only file was refused and `cat` of `<threads>/./<personal>/x.md`
        # was allowed. Canonicalising the command, rather than widening ten
        # regexes, keeps one answer for one file.
        canonical = _canonicalise_paths(command)
        haystacks = (command,) if canonical == command else (command, canonical)
        # HOS-238: and once more per directory the command can run in, with
        # every relative path resolved against it, so a `cd` followed by a
        # relative path is judged by where it lands.
        haystacks = haystacks + tuple(
            _bash_resolved_haystacks(command, payload.get("cwd") or ""))
        patterns = DANGEROUS_BASH_PATTERNS
        if any(_names_personal_home(h) for h in haystacks):
            patterns = patterns + _named_bash_patterns()
        if any(_REACH_MARKER in h for h in haystacks):
            patterns = patterns + _REACH_PATTERNS
        for pattern in patterns:
            if any(pattern.search(h) for h in haystacks):
                cmd_display = command[:200] + ("..." if len(command) > 200 else "")
                return {
                    "decision": "block",
                    "reason": (
                        f"Personal-threads protection — intentional policy block, "
                        f"not an error. This shell command targets the personal threads "
                        f"(CEO-only content kept out of the transcript): {cmd_display}"  # leak-guard: ok (string in a message/log, not a path)
                    ),
                    "_policy_deny": True,
                }
        return None

    if tool_name == "Read":
        # Collapsed, not just backslash-folded. `re.sub(r"\\+", "/")` answered
        # about the spelling: three ordinary spellings of one CEO-only file
        # opened it while this branch said nothing. Measured 2026-08-29.
        target = normalize_path(tool_input.get("file_path") or "")
        if (PERSONAL_PATH_RE.search(target) or _names_personal_archive(target)
                or _names_personal_home(target)):
            return {
                "decision": "block",
                "reason": (
                    "Personal-threads protection — intentional policy block, not an "
                    "error. Reading a personal-thread file is not allowed: CEO-only "
                    "content must not enter the transcript."
                ),
                "_policy_deny": True,
            }
        return None

    if tool_name in ("Grep", "Glob"):
        # Grep returns MATCHING LINES, which is reading. Its Bash twin was
        # refused throughout (`grep` and `rg` are both in DANGEROUS_BASH_PATTERNS
        # above), so the native tool was the one spelling of the same read that
        # went through. Verified 2026-08-28: this function returned None for both
        # tools, AND `_dispatch.py` was not even registered for them —
        # `data-path-redirect.py` was, on the same two tools, which is how the
        # gap stayed invisible. Glob returns only paths, but a personal thread's
        # FILENAME is CEO-only too, and the pair is cheaper to reason about than
        # a carve-out.
        #
        # Every field that can point the tool at the subtree is checked: `path`
        # (both tools), `pattern` (a Glob pattern is a path, and a Grep pattern
        # can carry one), and `glob` (Grep's file filter).
        # Folded, NOT collapsed. The collapse has to happen after the fields are
        # joined, and doing it here as well is worse than redundant: with
        # `path="<threads>/business"` and `pattern="../personal/*.md"`, a
        # per-field collapse drops the `..` against nothing and composes
        # `<threads>/business/personal/*.md`, which reaches nowhere and is
        # allowed. Joined first, the same two fields collapse onto the CEO-only
        # directory. Found by mutation, 2026-08-29: reverting this line changed
        # no verdict, which is what a redundant guard looks like.
        fields = {key: re.sub(r"\\+", "/", tool_input.get(key) or "")
                  for key in ("path", "pattern", "glob")}
        for key in ("path", "pattern", "glob"):
            if _names_personal_threads(fields[key]):
                return {
                    "decision": "block",
                    "reason": (
                        f"Personal-threads protection — intentional policy block, "
                        f"not an error. {tool_name} targets the personal threads via "
                        f"its {key!r} argument: CEO-only content must not enter "
                        f"the transcript."
                    ),
                    "_policy_deny": True,
                }
        # The fields COMPOSE, and a wildcard names the subtree in none of them.
        # Testing each argument on its own let three ordinary spellings through:
        # `Glob("threads/**/*.md")`, `Grep(path="threads")` and  # leak-guard: ok (history: the calls that got through)
        # `Grep(path="threads", glob="personal/*.md")` all reached CEO-only  # leak-guard: ok (history: the calls that got through)
        # files while every field passed its own test. Build the expression the
        # tool will actually expand, then ask where it can land.
        if tool_name == "Glob":
            # A Glob pattern is relative to `path` when both are given.
            expression = fields["path"].split("/") + fields["pattern"].split("/")
        else:
            # ripgrep applies `--glob` at ANY depth below `path`, not only at the
            # top of it, which is what the `**` stands for here. With no filter,
            # the search is the whole tree under `path`.
            expression = fields["path"].split("/")
            if fields["glob"]:
                expression = expression + ["**"] + fields["glob"].split("/")
        cwd = payload.get("cwd") or ""
        # HOS-236: the anchor above reads every `threads` directory as a threads
        # ROOT, and since C3 every business-thread home is one, so a search of
        # exactly such a home was refused. It is asked of the resolved search
        # instead, and only a search confined to a business-thread directory
        # with nothing personal below it is spared the anchor; the two checks
        # after it still run.
        if ((_search_reaches_personal(expression)
                and not _confined_to_business_threads(
                    _resolved_search(tool_name, fields, cwd)))
                or _sweep_descends_from_above(tool_name, fields, cwd)
                or _search_reaches_personal_home(tool_name, fields, cwd)):
            return {
                "decision": "block",
                "reason": (
                    f"Personal-threads protection — intentional policy block, "
                    f"not an error. {tool_name} can expand into the CEO-only "
                    f"threads subtree, so its result set is not safe for the "
                    f"transcript. Name the subtree you want, for example "
                    f"threads/business/, instead of searching the whole tree."  # leak-guard: ok (string in a message/log, not a path)
                ),
                "_policy_deny": True,
            }
        return None

    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return None

    target = normalize_path(
        tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    contents = []
    if tool_name == "Write":
        contents.append(tool_input.get("content") or "")
    elif tool_name == "Edit":
        contents.append(tool_input.get("new_string") or "")
    elif tool_name == "MultiEdit":
        for edit in (tool_input.get("edits") or []):
            contents.append(edit.get("new_string") or "")
    elif tool_name == "NotebookEdit":
        contents.append(tool_input.get("new_source") or "")

    if (PERSONAL_PATH_RE.search(target) or _names_personal_archive(target)
            or _names_personal_home(target)):
        return None
    if _is_allowed_doc_path(target):
        return None
    for c in contents:
        if _names_personal_threads(c):
            return {
                "decision": "block",
                "reason": (
                    f"Personal-threads protection — intentional policy block, not an "  # leak-guard: ok (string in a message/log, not a path)
                    f"error. Non-personal target {target!r} contains a "
                    f"threads/personal/ path reference."  # leak-guard: ok (string in a message/log, not a path)
                ),
                "_policy_deny": True,
            }
    return None

# ============================================================
# check_protect_docs: block direct edits to the synced copies of shared docs
# ============================================================
# Must match SYNC_FILES in `.claude/hooks/sync-docs.py`.
SYNCED_FILES = {
    "GETTING-STARTED.md",
    "GETTING-STARTED.html",
}


def check_protect_docs(payload: dict) -> dict | None:
    """Block a direct Write/Edit to a synced copy of a shared document.

    `sync-docs.py` (PostToolUse) copies the 2 files in SYNCED_FILES (count the
    set when you change it) from their template under the registry name
    `templates` to the registry name `spec-docs`, so a direct edit of the copy is
    overwritten on the next template change. This steers the edit to the
    template. The copies are found where `sync-docs.py` writes them, by registry
    name; a tree whose registry cannot name `spec-docs` (a public clone with no
    data repository) has no copy, and the wall stays inert.

    EMERGENCY-PROCEDURES left the set on 2026-09-22 and must not come back: its
    private template names real overlay addresses, so the two copies could
    never be byte-identical (two separate documents since HOS-131, 2026-09-25).
    """
    tool_name = payload.get("tool_name", "")
    # Reads excluded by name (an operator Read was refused when they were not,
    # 2026-08-11); every other tool shape arrives inside the check.
    if tool_name in ("Bash", "Read"):
        return None

    tool_input = payload.get("tool_input", {}) or {}
    # `notebook_path` too: NotebookEdit carries its target there (2026-08-31).
    file_path = (tool_input.get("file_path", "")
                 or tool_input.get("notebook_path", "") or "")
    if not file_path:
        return None

    # The basename of the NORMALISED path: a Windows-spelled path is not split
    # by `os.path.basename` on this host (2026-08-28).
    norm_path = file_path.replace("\\", "/")
    file_name = norm_path.rsplit("/", 1)[-1]
    if file_name not in SYNCED_FILES:
        return None

    try:
        from scripts.utils.data_path import data_path
        home = data_path("spec-docs").resolve()
    except Exception as exc:  # noqa: BLE001 - reported, never raised, in a hook
        print(f"[_dispatch:protect_docs] spec-docs unresolved, so no synced copy "
              f"is walled: {exc}", file=sys.stderr)
        return None
    target = Path(norm_path).expanduser()
    if not target.is_absolute():
        target = Path(payload.get("cwd") or WORKSPACE) / target
    try:
        target = target.resolve()
    except (OSError, RuntimeError) as exc:
        print(f"[_dispatch:protect_docs] {file_path!r} unresolvable, not walled: "
              f"{exc}", file=sys.stderr)
        return None
    if target.parent != home:
        return None

    return {
        "decision": "block",
        "reason": (
            f"BLOCKED: {target} is the synced copy of the shared document "
            f"{file_name} (registry name `spec-docs`). sync-docs.py overwrites it "
            f"from its template on the next template change, so a direct edit "
            f"here is lost. Edit the template {file_name} under the registry name "
            f"`templates` (python scripts/data-path.py path templates) instead; "
            f"sync-docs.py copies it here and re-renders the HTML."
        ),
    }


# ============================================================
# check_cwd_anchor — catch root-relative workspace scripts run from a drifted shell
# ============================================================
# The Bash tool persists its working directory across calls. A command that
# launches a workspace script by a root-relative path (.claude/skills/.../x.py,
# scripts/x.py, .claude/hooks/x.py) fails with a cryptic ENOENT when an earlier
# `cd` left the shell parked in a subdirectory — the failure that motivated this
# check (a skill run that inherited a shell parked in a data subdirectory).
#
# It fires ONLY when all three hold: (a) the live shell cwd (from the hook
# payload, which reflects real drift) is a subdirectory of the workspace root,
# (b) the command runs a root-relative workspace .py path, and (c) that path
# resolves from root but NOT from the current cwd. Condition (c) is a filesystem
# check on the exact token the shell will resolve, so the block fires only on a
# path that really is unreachable from that cwd, and no permission posture
# changes (it blocks with the anchored command to run instead, rather than
# force-allowing a rewrite).
#
# The left anchor is load-bearing and was missing until 2026-08-25. Without it
# the pattern matched the `scripts/...py` TAIL inside a fully-qualified absolute
# path, so `.venv/bin/python /home/.../heading-os/scripts/run-tests.py` extracted
# `scripts/run-tests.py`, found it under root and not under the drifted cwd, and
# refused a command that would have run perfectly - while telling the operator it
# "would fail with ENOENT", a cause the code had not established, since an
# absolute path resolves from any directory. `.logs/denials/denials.jsonl` records
# a real refusal of this shape at ts 1785739896.95. The lookbehind admits only a
# separator, so a match can no longer begin in the middle of a path.

WORKSPACE_REL_SCRIPT_RE = re.compile(
    r"""(?:^|(?<=[\s"'=(|&;]))["']?((?:\./)?(?:\.claude/(?:skills|hooks)|scripts)/[^\s"';|&)]+\.py)"""
)

def _cds_to_workspace_root(command: str, norm_root: str) -> bool:
    """True when a segment of `command` changes directory to the workspace root.

    The second self-anchoring spelling, and the one an operator actually types.
    Only the literal `git rev-parse --show-toplevel` was recognised, so
    `cd <root> && .venv/bin/python scripts/run-tests.py` was refused from a
    drifted shell, with the message saying the command "would fail with ENOENT".
    It would not: the `cd` fixes the cwd before the script is reached. MEASURED
    2026-08-31 from `<root>/tests`, that exact command BLOCKED.

    Same false-cause claim as the 2026-08-25 absolute-path defect recorded above,
    reached by the other door. Exempting is the right direction for a friction
    guard: a wrong refusal is the documented harm, and it is what teaches the
    operator to reach for the escape.
    """
    for segment in _SEGMENT_SPLIT_RE.split(command):
        words = segment.split()
        if len(words) < 2 or words[0] != "cd":
            continue
        target = words[1].strip("\"'")
        if not target:
            continue
        try:
            if os.path.realpath(target) == norm_root:
                return True
        except (OSError, ValueError):
            continue
    return False


def check_cwd_anchor(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command:
        return None
    # Self-anchored commands already cd to root — leave them alone.
    if "git rev-parse --show-toplevel" in command:
        return None
    cwd = payload.get("cwd") or ""
    try:
        norm_cwd = os.path.realpath(cwd) if cwd else ""
        norm_root = os.path.realpath(str(WORKSPACE))
    except (OSError, ValueError):
        return None
    # The second self-anchoring spelling. See `_cds_to_workspace_root`.
    if _cds_to_workspace_root(command, norm_root):
        return None
    if not norm_cwd or norm_cwd == norm_root:
        # `norm_cwd == norm_root` changes no verdict and is kept for cost: when
        # the shell IS at root, condition (c) below resolves the path from the
        # same directory twice and always answers "reachable", so the outcome is
        # this same None after a regex scan of the whole command. This hook runs
        # on every Bash call, so the scan is worth skipping. Noted because a
        # mutation sweep will keep surfacing the line as untested - it is
        # untestable through behaviour, being an equivalent short-circuit.
        return None  # at root (or cwd unknown) — nothing to anchor
    if not norm_cwd.startswith(norm_root + os.sep):
        return None  # shell is outside the workspace — do not interfere

    for match in WORKSPACE_REL_SCRIPT_RE.finditer(command):
        rel = match.group(1)
        # There was an `if os.path.isabs(rel): continue` here. It never fired and
        # never could: the group can only begin at `.claude/`, `./` or `scripts`,
        # so `rel` is relative by construction. It read as the guard against the
        # absolute-path false positive while doing nothing about it — the left
        # anchor above is what actually enforces this now, and the test named
        # below fails if that anchor is loosened.
        # Only act when the path resolves from root but is unreachable from cwd —
        # i.e. the command is about to fail purely because of shell drift.
        if os.path.exists(os.path.join(norm_root, rel)) and not os.path.exists(
            os.path.join(norm_cwd, rel)
        ):
            anchored = f'cd "$(git rev-parse --show-toplevel)" && {command}'
            return {
                "decision": "block",
                "reason": (
                    f"BLOCKED: the shell is parked in {norm_cwd}, but this command runs "
                    f"the root-relative path '{rel}', which only resolves from the "
                    f"workspace root — it would fail with ENOENT. Re-run it anchored to "
                    f"root:\n\n{anchored}"
                ),
            }
    return None


# ============================================================
# check_slow_shell — the two Bash shapes that spend the operator's wall clock
# ============================================================
#
# Measured over the six sessions ending 2026-08-22, from the session transcripts:
# the Bash tool held 4.85 h of wall time, median call 0.4 s, and two shapes owned
# 65% of the total.
#
#   blocking waiters      2.07 h / 19 calls / avg 393 s
#   the suite run serially 1.05 h / 107 calls, the long ones 434-601 s each
#
# Both have a ready replacement that was already in the tree. `run_in_background:
# true` returns at once and wakes the turn on exit, so a wait costs nothing.
# `scripts/run-tests.py` has passed `-n auto` since the push gate was
# parallelized; measured 2026-08-22 on 16 cores, the full suite finishes in
# 88.88 s there against 434-601 s for a bare serial `pytest`.
#
# This is a habit guard, so it is written as a wall rather than a note: recall
# across sessions is the thing that failed, and a rule that depends on the same
# recall would fail with it. The `slow-shell-ok` marker keeps a deliberate
# exception possible and greppable — a wall with no door gets torn down.

SLOW_SHELL_ESCAPE = "slow-shell-ok"

_SHELL_OPERATORS = ";|&\n"


def _shell_segments(command: str) -> list:
    """Split a compound command into the parts a shell would run separately.

    Quote-aware, because a regex is not. The first cut here split on `|`
    unconditionally, and on 2026-08-22 it refused
    `ls auto-memory/ | grep -iE "test|pytest|shell"` — the alternation inside the
    quoted pattern broke into a segment whose only word was `pytest`, so a
    directory listing read as a suite run. A metacharacter inside quotes is data.

    Only the separators matter here, so `>` and `<` are left in place; `2>&1`
    splitting at the `&` is harmless, since every caller looks at where a word
    sits, not at redirection.
    """
    segments, buf = [], []
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            buf.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(command):
                buf.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            buf.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            buf.append(char)
            buf.append(command[index + 1])
            index += 2
            continue
        if char in _SHELL_OPERATORS:
            segments.append("".join(buf))
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    segments.append("".join(buf))
    return [segment for segment in (part.strip() for part in segments) if segment]

# A polling loop: any while/until that sleeps inside its body. Duration is not the
# question here — the loop holds the turn for as long as the watched thing runs.
#
# Run this over `_unquoted_skeleton(command)`, never over the raw text. A shell
# keyword is only a keyword outside quotes and outside a comment, and this
# pattern reaches across the whole string by design (a real loop puts `while` and
# `sleep` in different segments, so `_shell_segments` cannot be used here). Over
# raw text that reach turned `echo "while you wait"; sleep 1` into a policy deny
# whose own message promises that short sleeps pass. Reproduced 2026-08-28.
_POLL_LOOP_RE = re.compile(r"\b(?:while|until)\b[\s\S]*?\bsleep\b")


def _unquoted_skeleton(command: str) -> str:
    """`command` with quoted spans and comments blanked out, structure preserved.

    Quote contents become empty, so a shell keyword written inside a string
    cannot be read as one; separators, redirections and unquoted words are kept
    exactly where they were, so a pattern that reaches across segments still
    does. Trailing `#` comments are dropped for the same reason: `sleep 1 # a
    while later` is not a poll loop.

    A HERE-DOCUMENT body is blanked for the same reason. It is data being fed to
    a program's stdin, not shell syntax, and this hook's own commit message was
    refused by it on 2026-08-28: the message describes the poll-loop defect, so
    it contains the words `while` and `sleep` in ordinary prose. Both spellings
    (`<<WORD` and `<<-WORD`, delimiter quoted or bare) end at a line holding the
    delimiter alone. An UNTERMINATED heredoc consumes the rest of the string,
    which matches what the shell would do with it.

    Deliberately shares the quote-walk shape of `_shell_segments` above rather
    than calling shlex: shlex drops the quotes AND keeps the words, which is the
    opposite of what this needs.
    """
    out = []
    quote = None
    pending_heredocs: list = []
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(command):
                index += 2
                continue
            if char == quote:
                quote = None
                out.append(char)
            index += 1
            continue
        if char in "'\"":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            # An escaped character is data, never a keyword letter.
            out.append(" ")
            out.append(" " if command[index + 1] != "\n" else "\n")
            index += 2
            continue
        if char == "#" and (not out or out[-1].isspace()):
            # Comment to end of line. A newline is a shell separator, so keep it.
            while index < len(command) and command[index] != "\n":
                index += 1
            continue
        heredoc = _HEREDOC_START_RE.match(command, index)
        if heredoc:
            # The redirection itself stays; only the BODY is data. The body does
            # not start here, it starts after this line ends, so the delimiter is
            # queued and the walk carries on with the rest of the line.
            out.append("<<")
            pending_heredocs.append(heredoc)
            index = heredoc.end()
            continue
        if char == "\n" and pending_heredocs:
            out.append("\n")
            index += 1
            for opened in pending_heredocs:
                index = _skip_heredoc_body(command, index, opened)
            pending_heredocs = []
            continue
        out.append(char)
        index += 1
    return "".join(out)


# `<<` or `<<-`, optional space, then the delimiter word: bare, 'single' or
# "double" quoted. `<<<` is a here-STRING, one word on the same line, and is not
# matched here: the negative lookahead keeps it out so it falls through to the
# ordinary character walk.
# `(?<!<)` as well as `(?!<)`, and the lookBEHIND is the half that works.
#
# A here-STRING is `<<<word`, and the trailing `(?!<)` only stops the match that
# starts at its FIRST `<`. `re` then retries one character along, where `<<` is
# the second and third angle brackets, the lookahead sees `w`, and `word` is
# read as a heredoc delimiter. `_skip_heredoc_body` looks for a line equal to
# `word`, never finds one, and returns len(command) - so everything after the
# here-string is dropped from the skeleton.
#
# MEASURED 2026-09-01 against the shipped pattern:
#
#     grep x <<<needle\nwhile true; do sleep 1; done
#         skeleton  'grep x <<<\n'      _blocking_wait  False
#
# The poll loop the guard exists to refuse is invisible to it. That is the
# failure the heredoc case was written against - "a heredoc reader that never
# recognises its terminator swallows the rest of the command" - reached through
# the one opener nobody tested. Every here-string case that existed was QUOTED,
# and the quote scanner empties those before this pattern is consulted, so the
# bare form went unseen. Those cases were parked in the 2026-09-21 test-suite
# split, so the bare here-string is now held by the pattern below and by
# nothing else.
_HEREDOC_START_RE = re.compile(
    r"(?<!<)<<(?!<)(?P<dash>-?)\s*(?P<quote>['\"]?)(?P<word>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")


def _skip_heredoc_body(command: str, position: int, match) -> int:
    """Index just past the body `match` opened, body and terminator removed.

    `position` is the first character of the body (the walk has already consumed
    the newline that ends the opening line). Returns len(command) for an
    unterminated heredoc, which is what the shell does with it too. `<<-` strips
    leading TABS from the terminator line, per POSIX; the plain form requires the
    delimiter alone on its line.
    """
    delimiter = match.group("word")
    while position <= len(command):
        line_end = command.find("\n", position)
        line = command[position:line_end if line_end != -1 else len(command)]
        candidate = line.lstrip("\t") if match.group("dash") else line
        if candidate.rstrip("\r") == delimiter:
            return len(command) if line_end == -1 else line_end + 1
        if line_end == -1:
            return len(command)
        position = line_end + 1
    return len(command)

# A bare wait long enough that the operator feels it. Short sleeps (a daemon
# socket coming up) are ordinary and stay allowed.
_BLOCKING_SLEEP_SECONDS = 30

# Flags that narrow a pytest run to something that finishes in seconds, or that
# already distribute it. Either way there is nothing to correct.
_PYTEST_PARALLEL_FLAGS = ("-n", "--numprocesses")
_PYTEST_NARROWING_FLAGS = (
    "-k", "--collect-only", "--co", "--lf", "--last-failed", "--ff", "--failed-first",
)

_SERIAL_SUITE_REASON = """\
BLOCKED: this runs the suite in one process. Measured 2026-08-22 on this machine, \
the same 6164 tests finish in 88.88 s across 16 workers and 434-601 s serially — \
the serial shape spent 1.05 h of Bash wall time over the six sessions ending \
2026-08-22.

Use the runner, which has passed `-n auto` since the push gate was parallelized:

    .venv/bin/python scripts/run-tests.py

Or add the flag to the command you had: `-n auto`.

Narrow runs are untouched — a file path, a directory below the suite root such \
as `tests/security`, a node id, `-k`, or `--collect-only` all pass. An \
UNEXPANDED shell variable (`pytest $T`) is not one of them: this hook reads the \
command text, not the shell's environment, so it cannot tell `$T` from the whole \
suite and refuses. Write the path out. \
For a deliberate serial run (a baseline measurement, a plugin that will not \
distribute), append `# {escape}` and it goes through."""

_WAITER_REASON = """\
BLOCKED: this command waits, and the wait holds the turn. Waiters of this shape \
cost 2.07 h of Bash wall time over 19 calls (avg 393 s) in the six sessions \
ending 2026-08-22, and none of that time did anything.

Run the long command itself with `run_in_background: true` instead. The tool \
returns immediately and the turn is re-invoked when the command exits, so the \
wait is free and the output is still delivered.

Short sleeps are untouched — under {threshold} s with no polling loop passes. \
For a deliberate blocking wait, append `# {escape}` and it goes through."""


# Environment runners that execute their next non-flag word as a program:
# `uv run pytest ...` runs pytest exactly as a bare `pytest` would. `uv` is this
# repo's canonical toolchain, so its spelling of a serial suite run has to land
# inside the guard. Deliberately a short, named list: a runner is recognised by
# name, never guessed, so an ordinary program whose second word happens to be
# `run` cannot be mistaken for one.
_ENV_RUNNERS = ("uv", "uvx", "poetry", "pdm", "hatch", "rye", "pipenv")


def _is_runner_invocation(tokens: list, index: int) -> bool:
    """True when `tokens[index]` is the program a `<runner> run ...` executes.

    Flags between `run` and the program are allowed (`uv run --frozen pytest`),
    because they configure the runner, not the run. Anything that is not a flag
    ends the search: in `uv run python -m pytest`, the program is `python`, and
    the `-m` clause in the caller already recognises that shape.
    """
    if index < 2 or tokens[0] not in _ENV_RUNNERS or tokens[1] != "run":
        return False
    return all(word.startswith("-") for word in tokens[2:index])


def _pytest_argv(command: str) -> list | None:
    """The argv of a pytest invocation in `command`, or None if there is none.

    Judged positionally: `pytest` must be the first word of a shell segment,
    follow `-m` on an interpreter, or follow a named environment runner
    (`uv run pytest`). A `pytest` that is merely an argument to some other
    program — a grep pattern, a path being echoed — is not an invocation and
    must not be treated as one.

    The runner form was missing until 2026-08-28, and `uv` is this repo's own
    canonical toolchain (CLAUDE.md § Setup). `uv run python -m pytest tests/` was
    caught by the `-m` clause while `uv run pytest tests/` — the shorter spelling
    of the same serial full-suite run — went through untouched. A guard the
    prescribed tool walks around stops being a guard.
    """
    import shlex  # local: this hook runs on every Bash, Read and write call

    for segment in _shell_segments(command):
        if "pytest" not in segment:
            continue
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            continue  # unbalanced quotes — not something to reason about
        if not tokens:
            continue
        # `word`, not `token`: ruff's S105 reads a variable called `token` as a
        # credential and flags the comparison as a hardcoded password.
        for index, word in enumerate(tokens):
            if word != "pytest" and not word.endswith("/pytest"):
                continue
            if index == 0:
                return tokens
            if tokens[index - 1] == "-m":
                return tokens
            if _is_runner_invocation(tokens, index):
                return tokens
        # `python scripts/run-tests.py` reaches pytest through the runner, which
        # already distributes; it is the prescribed form, not a finding.
    return None


# Flags whose NEXT argv word is a value, not a test target. Without this, the
# value was read as a named target: `pytest --rootdir /a/b` counted `/a/b` as a
# narrow run and the whole suite went through serially, having named no target
# at all. The attached spelling (`--rootdir=/a/b`) was already handled, because
# it starts with `-`.
_PYTEST_VALUE_FLAGS = ("--rootdir", "-p", "-k", "-o", "--basetemp",
                       "-c", "--override-ini", "--junitxml", "-W")


def _pytest_marker_expression(argv: list) -> str | None:
    """The value of pytest's OWN `-m`, or None.

    Located after the pytest token, never by scanning the whole argv.
    `python -m pytest` puts an unrelated `-m` in the same list, so reading the
    first one would have made every `python -m pytest tests/` look narrow and
    deleted the Bash half of this guard.
    """
    start = next((i for i, w in enumerate(argv)
                  if w == "pytest" or w.endswith("/pytest")), None)
    if start is None:
        return None
    rest = argv[start + 1:]
    for i, word in enumerate(rest):
        if word == "-m":
            return rest[i + 1] if i + 1 < len(rest) else None
        if word.startswith("-m") and len(word) > 2 and not word.startswith("--"):
            return word[2:]
    return None


def _marker_selects_a_subset(expr: str) -> bool:
    """True when `-m <expr>` PICKS a subset, not when it merely deselects.

    `-k` was exempt here and `-m` was not, so `.venv/bin/python -m pytest -m
    acceptance` was policy-denied. That shape is not exotic: this repo defines
    the markers itself (`pyproject.toml`, `slow` and `acceptance`) and
    `scripts/run-tests.py` runs `-m acceptance` as one of its own lanes. MEASURED
    2026-08-31, before the fix: `-m acceptance` and `-m "not slow"` both BLOCKED
    while `-k test_foo` passed. A guard that refuses the exact shape its own
    refusal text exempts is the failure mode `_is_subdirectory_target` above was
    written about, and it teaches the operator to reach for the escape comment.

    The rule is per NAME, because the two directions are genuinely different.
    `-m acceptance` runs a couple of tests. `-m "not slow"` is the whole suite
    with a handful removed, so it stays blocked: a marker reached through `not`
    only deselects, and deselecting from six thousand tests is still six
    thousand tests. `-m "not slow and acceptance"` selects, so it passes.
    """
    tokens = expr.replace("(", " ").replace(")", " ").split()
    negate = False
    for tok in tokens:
        if tok == "not":
            negate = not negate
            continue
        if tok in ("and", "or"):
            negate = False
            continue
        if not negate:
            return True
        negate = False
    return False


def _is_serial_full_suite(argv: list) -> bool:
    marker = _pytest_marker_expression(argv)
    if marker and _marker_selects_a_subset(marker):
        return False
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token.startswith(_PYTEST_PARALLEL_FLAGS):
            return False  # -n, -n8, -nauto, --numprocesses=auto
        if token in _PYTEST_NARROWING_FLAGS or token.startswith("--collect-only"):
            return False
        if token in _PYTEST_VALUE_FLAGS:
            skip_next = True
            continue
        if index == 0:
            continue
        # A named target — a file, a directory below tests/, a node id — is a
        # narrow run whose duration is not the problem this guard was built for.
        if token.endswith(".py") or "::" in token:
            return False
        if _is_subdirectory_target(token):
            return False
    return True


def _is_subdirectory_target(token: str) -> bool:
    """True for a directory BELOW the suite root, e.g. `tests/security`.

    The comment above has claimed since this guard shipped that a directory
    below tests/ counts as narrow. The code did not implement it: only `.py`
    and `::` were accepted, so `pytest tests/security` was policy-denied with a
    message promising narrow runs are untouched. Found by the 2026-08-23 audit
    and reproduced. A guard that refuses the exact shape its own text exempts
    teaches the operator to reach for `# slow-shell-ok`, which is the one
    outcome a habit guard must never produce.

    The distinction that matters is depth, not existence: `tests/` IS the full
    suite and must stay blocked, while `tests/security` is a fast subset.

    Depth is measured AFTER stripping the workspace root, because counting raw
    segments made the spelling decide the verdict:
    `pytest /home/.../heading-os/tests` has six segments and read as narrow,
    so the identical 6000-test serial run passed by absolute path while
    `pytest tests/` was blocked. Agent threads reset cwd between calls and this
    workspace instructs absolute paths, so that was the likely spelling, not an
    exotic one. Stripping the root is string work; the filesystem is still not
    touched.
    """
    if token.startswith("-"):
        return False  # `--rootdir=/x` is a flag, not a target
    candidate = token
    root = str(WORKSPACE)
    if os.path.isabs(candidate):
        normalized = os.path.normpath(candidate)
        if normalized == root or normalized.startswith(root + os.sep):
            candidate = normalized[len(root):]
        else:
            # Outside this workspace entirely; not the suite this guard bounds.
            return True
    segments = [s for s in candidate.split("/") if s not in ("", ".")]
    return len(segments) > 1


def _blocking_wait(command: str) -> bool:
    if _POLL_LOOP_RE.search(_unquoted_skeleton(command)):
        return True

    import shlex  # local, same reason as above

    for segment in _shell_segments(command):
        if not segment.startswith("sleep"):
            continue
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            continue
        if len(tokens) < 2 or tokens[0] != "sleep":
            continue
        try:
            if float(tokens[1]) >= _BLOCKING_SLEEP_SECONDS:
                return True
        except ValueError:
            continue
    return False


def check_slow_shell(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input", {}) or {}
    command = tool_input.get("command", "") or ""
    if not command or SLOW_SHELL_ESCAPE in command:
        return None

    argv = _pytest_argv(command)
    if argv is not None and _is_serial_full_suite(argv):
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": _SERIAL_SUITE_REASON.format(escape=SLOW_SHELL_ESCAPE),
        }

    # A backgrounded waiter is the prescribed fix, not the defect: it returns at
    # once and holds nothing. Only a foreground wait is worth refusing.
    if not tool_input.get("run_in_background") and _blocking_wait(command):
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": _WAITER_REASON.format(
                threshold=_BLOCKING_SLEEP_SECONDS, escape=SLOW_SHELL_ESCAPE
            ),
        }
    return None


# ============================================================
# check_rate_limit — daily Write/Edit cap + runaway-loop detection
# ============================================================
#
# Closes P2.6 from the 2026-05-14 workspace deep audit. The dispatcher previously
# had no rate-limit / loop-detection. A runaway skill writing 10,000 files would
# only be stopped by Claude's own context window. This check catches the pattern
# at the hook layer with file-based daily counters.
#
# Every read-modify-write of the state file runs under `_rate_state_lock`, here
# and in `check_tool_budget`: without it overlapping calls lost 29% of their
# updates (MEASURED 2026-09-04; the measurement is in the lock's docstring).

import time
from datetime import datetime

# `WS_RATE_LIMIT_STATE` redirects the counter, and the test suite is the only
# caller that sets it (`tests/conftest.py`). Tests drive this hook in a
# subprocess exactly as production does, so before this seam existed every
# fixture write they made
# was counted against the operator's real daily allowance. Measured 2026-08-07:
# the production counter stood at 1033 and was BLOCKING, and the writes it had
# stored were fixtures — a thread file that does not exist, a Windows path that
# cannot exist on this machine, a scratch probe. The suite went red on volume
# nobody produced, and the runaway-loop guard's own numerator was filling with
# work nobody did, which is the worse half: a real runaway would arrive
# indistinguishable from a week of testing.
#
# The same shape as the denial log's `WORKSPACE_LOG_DIR` pin one guard along
# (retired 2026-09-25 with the engine's `.logs/`), and for the reason the denial
# log needed it on 2026-08-01. It weakens nothing that was not already
# open: the block message this check prints already tells the operator to delete
# the state file, so a redirected path is not a new way to reset the count.
RATE_LIMIT_STATE_FILE = Path(
    os.environ.get("WS_RATE_LIMIT_STATE")
    or _state_dir() / "dispatch-rate.json"
)
RATE_LIMIT_SOFT = int(os.environ.get("WS_RATE_LIMIT_SOFT", "200"))   # advisory at N writes/day
RATE_LIMIT_HARD = int(os.environ.get("WS_RATE_LIMIT_HARD", "1000"))  # block at N writes/day
RATE_LIMIT_LOOP_WINDOW = 20      # how many recent calls to inspect
RATE_LIMIT_LOOP_THRESHOLD = 6    # same (tool, path) >= N times in window → advisory


def _load_rate_state() -> dict:
    if not RATE_LIMIT_STATE_FILE.exists():
        return {"date": "", "count": 0, "recent": []}
    try:
        data = json.loads(RATE_LIMIT_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            # Valid JSON that is not an object reached `.get` in BOTH counter
            # checks and raised AttributeError, which `main()` then caught and
            # failed open on. MEASURED 2026-08-31 with the state file holding
            # `[]`: `check_rate_limit` and `check_tool_budget` both raised.
            print(f"[_dispatch:rate_limit] state was a "
                  f"{type(data).__name__}, not an object; counters reset to zero",
                  file=sys.stderr)
            return {"date": "", "count": 0, "recent": []}
        return data
    except Exception as e:  # noqa: BLE001 - a hook must not break the tool call
        # Say it. This reset the daily write cap, the runaway-loop window AND
        # check_tool_budget's rolling history to empty, and printed nothing at
        # all, so the one event these counters exist to catch would arrive
        # looking like a fresh day. `_save_rate_state` has always reported its
        # own failure; this side did not.
        print(f"[_dispatch:rate_limit] state unreadable, counters reset to zero: {e}",
              file=sys.stderr)
        return {"date": "", "count": 0, "recent": []}


def _save_rate_state(state: dict) -> None:
    # Atomic write (tmp + os.replace) per the global atomic-state-write rule: a torn
    # write would silently reset the runaway-loop counter. Added 2026-06-09 audit.
    #
    # The staging name carries the pid. `os.replace` is atomic for ONE writer;
    # the fixed `.json.tmp` was shared, so two hook processes - which this file's
    # own comments say do race - each opened the same staging path with mode "w"
    # and interleaved their writes, and whichever replaced second promoted torn
    # JSON to the live file. Per-process staging makes the atomicity claim above
    # true for the concurrency that actually happens here.
    #
    # The staging file is REMOVED when the write does not complete. Until
    # 2026-09-01 the handler below only printed, so a failed write left the
    # staging file behind for good. MEASURED that day: the filesystem filled to
    # 100% while eight hook processes were saving, and `.claude/state/` was left
    # holding eight zero-byte `dispatch-rate.json.<pid>.tmp` files, all stamped
    # 12:05. Nothing ever removed them, and nothing would have: the pid is in
    # the name, so no later run reuses that path on purpose. This directory is
    # read on every Write and Edit, so the litter is unbounded growth in a hot
    # path.
    #
    # `finally` rather than an `except` arm, because a KeyboardInterrupt between
    # the create and the replace leaves exactly the same orphan and does not
    # derive from `Exception`.
    tmp = RATE_LIMIT_STATE_FILE.with_suffix(f".json.{os.getpid()}.tmp")
    try:
        RATE_LIMIT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, RATE_LIMIT_STATE_FILE)
    except Exception as e:
        print(f"[_dispatch:rate_limit] state save failed: {e}", file=sys.stderr)
    finally:
        # A successful `os.replace` consumed the staging path, so this is a
        # no-op on the happy path. `missing_ok` rather than a check, because a
        # check would race with the replace it is meant to follow.
        try:
            tmp.unlink(missing_ok=True)
        except OSError as e:
            print(f"[_dispatch:rate_limit] could not remove {tmp.name}: {e}",
                  file=sys.stderr)


# Two seconds, matching `file_lock`'s own default and for the same reason: this
# runs inside a hook the harness is waiting on. The critical section it covers is
# one small read and one small write, so legitimate contention is resolved in
# microseconds and this bound is never reached by honest traffic. MEASURED
# 2026-09-04 on an idle machine: sixteen hook processes fanned out at once
# finish in 433-547 ms of wall clock end to end, subprocess startup included,
# and across five such trials none of the 160 acquisitions reached this bound
# (each process takes it twice, once per check). Two seconds means something is
# wrong rather than busy.
RATE_LOCK_WAIT_SECONDS = 2.0


def _rate_lock_path() -> Path:
    """The sidecar `<state file>.lock`, resolved at CALL time.

    A sidecar rather than the state file itself, because `_save_rate_state`
    promotes a staging file with `os.replace`: a lock taken on the state file
    holds an INODE, and after the first save that inode is no longer the file
    anyone reads. The sidecar is never replaced, so its identity is stable for as
    long as the state file has a name. Not the DIRECTORY either: the checkout's
    state directory (`_state_dir()`) is shared with the checkpoint store and the
    graph-first markers, and locking it would serialise writers that have
    nothing to do with each other.

    Read from the module global on every call rather than frozen beside
    `RATE_LIMIT_STATE_FILE`, because an in-process caller may move that
    constant with `monkeypatch.setattr` after import. A lock path frozen at
    import would keep guarding the file the module was imported with, which is
    the one file the caller is no longer using.
    """
    return RATE_LIMIT_STATE_FILE.with_name(RATE_LIMIT_STATE_FILE.name + ".lock")


@contextlib.contextmanager
def _rate_state_lock():
    """Serialise one whole read-modify-write of the rate-limit state file.

    `_save_rate_state` makes each WRITE indivisible, not the read and the write
    that follows it. MEASURED 2026-09-04, sixteen hook processes at once against
    one state file: 70 of 240 calls left no trace (29%); after this lock, 400
    calls and none lost. Overlap is what a runaway loop produces, so without the
    lock the cap undercounted hardest in the case it exists to catch.

    `checkpoint_paths.file_lock` is the workspace's one exclusive-lock
    primitive; imported inside the function because this hook runs on every
    tool call.

    **Bounded, and on expiry it counts UNLOCKED** (as does a host without
    `fcntl`): this counter is telemetry with a cap, not an authorisation
    boundary, and a lock that could not be taken must not block every Write.
    `file_lock` says so on stderr.
    """
    try:
        from scripts.utils.checkpoint_paths import file_lock
    except Exception as e:  # noqa: BLE001 - a hook must not break the tool call
        print(f"[_dispatch:rate_limit] no lock primitive ({e}); counting unlocked",
              file=sys.stderr)
        yield False
        return

    with file_lock(_rate_lock_path(), wait=RATE_LOCK_WAIT_SECONDS,
                   label="_dispatch:rate_limit") as held:
        yield held


def check_rate_limit(payload: dict) -> dict | None:
    """Daily Write/Edit cap + runaway-loop detection.

    Soft limit emits advisory. Hard limit blocks. Loop detection (same tool + same
    file_path repeating in a short window) always emits advisory; never blocks
    (legitimate iterative refactors hit this too).

    Bash is excluded - it has its own surface and inflating Bash counts would mask
    the file-write loops this check exists to catch.
    """
    tool_name = payload.get("tool_name", "")
    if tool_name == "Bash":
        return None
    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return None

    tool_input = payload.get("tool_input", {}) or {}
    file_path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")

    # The load, the mutation and the save are one indivisible step. Read
    # `_rate_state_lock` for what was measured before they were.
    with _rate_state_lock():
        state = _load_rate_state()
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        if state.get("date") != today:
            # Reset only the keys THIS check owns. Rebinding `state` to a fresh
            # three-key literal also dropped `tool_history`, which
            # check_tool_budget owns and which is a rolling 30-minute window,
            # not a daily one. The first write after local midnight therefore
            # emptied that window, so a runaway loop straddling midnight
            # restarted its count at 1 and neither the soft nor the hard tool
            # cap could fire on the calls already made.
            state.update({"date": today, "count": 0, "recent": [],
                          "actor_recent": {}})

        state["count"] = int(state.get("count", 0)) + 1
        # The loop window is per actor, like `check_tool_budget`'s advisory
        # tier, since 2026-09-27. Machine-wide, six writes to one file by
        # another yard fired "if it looks like a loop, stop" in whichever
        # session made the next write. The daily count above stays
        # machine-wide: it feeds the hard cap and its own soft notice, and both
        # describe the machine.
        actor_key = _state_key(str(payload.get("session_id", "")).strip(),
                               actor_id(payload))
        actor_recent = state.get("actor_recent")
        if not isinstance(actor_recent, dict):
            actor_recent = {}
        own_recent = actor_recent.get(actor_key)
        if not isinstance(own_recent, list):
            own_recent = []
        own_recent = (own_recent + [[tool_name, file_path, int(time.time())]])[-RATE_LIMIT_LOOP_WINDOW:]
        actor_recent[actor_key] = own_recent
        state["actor_recent"] = actor_recent
        state.pop("recent", None)
        _save_rate_state(state)

    # Hard limit - block
    if state["count"] > RATE_LIMIT_HARD:
        return {
            "decision": "block",
            "reason": (
                f"BLOCKED: workspace daily write cap ({RATE_LIMIT_HARD}) exceeded "
                f"({state['count']} writes today). Pause and review what is producing "
                f"this volume. Override: `export WS_RATE_LIMIT_HARD={RATE_LIMIT_HARD * 2}` "
                f"if intentional, or delete {RATE_LIMIT_STATE_FILE} to reset "
                f"the counter."
            ),
        }

    # Soft limit - advisory
    if state["count"] == RATE_LIMIT_SOFT + 1:  # fire once per crossing
        return {
            "additionalContext": (
                f"NOTICE: {state['count']} file writes today (soft cap {RATE_LIMIT_SOFT}). "
                f"If this is an intentional batch operation, ignore. Hard cap is {RATE_LIMIT_HARD}."
            ),
        }

    # Loop detection - same (tool, file_path) repeating
    if file_path and len(own_recent) >= RATE_LIMIT_LOOP_THRESHOLD:
        signature = (tool_name, file_path)
        recent_signatures = [(r[0], r[1]) for r in own_recent
                             if isinstance(r, list) and len(r) >= 2]
        repeat_count = recent_signatures.count(signature)
        if repeat_count >= RATE_LIMIT_LOOP_THRESHOLD:
            return {
                "additionalContext": (
                    f"NOTICE: {tool_name} on '{os.path.basename(file_path)}' fired "
                    f"{repeat_count} times in this session's last {RATE_LIMIT_LOOP_WINDOW} writes. "
                    f"If this is iterative refactoring, continue. If it looks like a loop, stop."
                ),
            }

    return None


# ============================================================
# check_tool_budget — total-tool-call cap + same-args repeat detection
# ============================================================
#
# Closes P2.5 from the 2026-05-14 workspace deep audit. Complements check_rate_limit
# (which only counts Write/Edit) by tracking ALL tool calls (including Bash, Read,
# Grep, etc.) in a rolling time window. Catches the "agent in a loop" pattern that
# check_rate_limit misses when the loop happens through Read+Bash rather than writes.
#
# Token-usage half of the audit P2.5 acceptance criteria (soft 100K / hard 500K) is
# handled by Langfuse Cloud dashboards rather than at the hook layer - hooks don't
# see Claude's token usage, but Langfuse captures every messages.create() call with
# full token data via the @observe decorator. Set a Langfuse alert on cost/tokens
# per dashboard for the proper signal.

TOOL_BUDGET_WINDOW_MINUTES = 30
TOOL_BUDGET_SOFT = int(os.environ.get("WS_TOOL_BUDGET_SOFT", "75"))    # advisory at N
# Raised 1200 -> 4000 on 2026-08-27, on the operator's instruction, after the cap
# blocked a legitimate run. Workflows are the standing working method here, and a
# workflow's subagent tool calls all count against this one window: a 66-agent
# audit workflow made 1849 calls in 29 minutes and tripped a cap set when the main
# loop was the only caller. The cap exists to catch a runaway loop, and a runaway
# loop reaches 4000 in a window just as surely as it reaches 1200 - it just no
# longer catches deliberate fan-out on the way. Cost of the higher number: the
# rolling history is bounded at cap+100, so the state file this hook rewrites on
# every tool call grows from about 48 KB to about 152 KB at 37 bytes an entry.
TOOL_BUDGET_HARD = int(os.environ.get("WS_TOOL_BUDGET_HARD", "4000"))  # block at N
# Raised 3 -> 4 on 2026-08-20, in the same change that made the signature stable.
# The detector had never fired, so 3 was never tested against real traffic; a
# legitimate edit-then-recheck cycle produces three identical calls easily. This
# branch is advisory-only and can never block, so the cost of it being slightly
# loose is a missed notice, and the cost of it being tight is noise on every
# ordinary retry. Lower it again only against a measured false-negative.
TOOL_REPEAT_THRESHOLD = 4  # same (tool, args-digest) N in a row → advisory


def _stable_args_signature(tool_name: str, tool_input: dict) -> str:
    """Build a stable signature for tool+args to detect identical repeats.

    sha256, not the builtin `hash`. Every hook invocation is a fresh interpreter
    and PYTHONHASHSEED is randomised per process, so `hash` gave a different
    value for identical input every time and no two stored signatures could ever
    match. Measured 2026-08-20: `.claude/state/dispatch-rate.json` held 344
    tool_history entries and 344 DISTINCT signatures, and the denial log records
    zero firings of this check across its whole history. The function name
    already promised stability; now it holds. The digest is a dedupe key here, never a
    security boundary — and sha256 rather than sha1 because the repo's ruff
    profile runs flake8-bandit, which flags sha1 (S324). A pragma to silence a
    gate is the move this workspace forbids, and the cost of the wider digest
    here is nothing: it is truncated to 16 hex characters either way.
    """
    try:
        # Sort keys so dict ordering doesn't fool the digest
        canonical = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        canonical = str(tool_input)
    digest = hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


def check_tool_budget(payload: dict) -> dict | None:
    """Total-tool-call cap in 30-min rolling window + same-args repeat detection.

    Counts every tool invocation THIS DISPATCHER SEES, which is the matcher set
    named in the module docstring and nothing else (do not re-list it here; a
    copy went stale on 2026-08-29). Tools outside that set, e.g. WebFetch and
    MCP servers other than codegraph and Hindsight, never reach this hook, so a
    runaway loop built out of those is invisible here.

    Soft cap warns, counted per session actor; hard cap blocks, counted
    machine-wide. TOOL_REPEAT_THRESHOLD identical calls in a row (same tool,
    same args) give an advisory only: the pattern signals a stuck loop, but
    re-running the same `python script --check` is not a bug.
    """
    tool_name = payload.get("tool_name", "")
    if not tool_name:
        return None
    tool_input = payload.get("tool_input", {}) or {}

    # Under the same lock as `check_rate_limit`, and it is the same lock rather
    # than a second one: both checks read-modify-write ONE file, so a sibling
    # left racing would have gone on overwriting this one's entries through the
    # shared state dict. Both run in this process on the same tool call, one
    # after the other, and neither hold nests inside the other.
    with _rate_state_lock():
        state = _load_rate_state()
        now_ts = int(time.time())
        window_seconds = TOOL_BUDGET_WINDOW_MINUTES * 60
        cutoff = now_ts - window_seconds

        # Keep tool-call history separately from write history (check_rate_limit owns "recent")
        tool_history = [
            entry for entry in state.get("tool_history", [])
            if isinstance(entry, list) and len(entry) >= 2 and entry[1] >= cutoff
        ]
        signature = _stable_args_signature(tool_name, tool_input)
        tool_history.append([signature, now_ts])
        # Keep history bounded, but the bound MUST stay above the hard cap or the
        # cap can never fire: count is len(tool_history), and truncating storage
        # below the cap means a reloaded window can never reach it. Margin of
        # +100 over the cap.
        state["tool_history"] = tool_history[-(TOOL_BUDGET_HARD + 100):]

        # The ADVISORY tier is per actor: one session, or one agent inside it.
        # The machine-wide `tool_history` above feeds the hard cap only. Until
        # 2026-09-27 the soft notice and the repeat detector read that same
        # machine-wide window, so with several yards running, "76 tool calls ...
        # stop and reset" could land in whichever session made the 76th call on
        # the machine, and "N times in a row" was broken by other sessions'
        # calls in between. A payload with no session id shares one "unknown"
        # key, which is what it had before.
        actor_key = _state_key(str(payload.get("session_id", "")).strip(),
                               actor_id(payload))
        per_actor = state.get("actor_tool_history")
        if not isinstance(per_actor, dict):
            per_actor = {}
        pruned = {}
        for key, entries in per_actor.items():
            kept = [e for e in entries if isinstance(e, list)
                    and len(e) >= 2 and isinstance(e[1], (int, float))
                    and e[1] >= cutoff] if isinstance(entries, list) else []
            if kept:
                pruned[key] = kept
        own = pruned.get(actor_key, []) + [[signature, now_ts]]
        # The bound only has to keep the soft crossing and the repeat run
        # visible, so it sits a margin above the larger of the two.
        own = own[-(max(TOOL_BUDGET_SOFT, TOOL_REPEAT_THRESHOLD) + 50):]
        pruned[actor_key] = own
        state["actor_tool_history"] = pruned
        _save_rate_state(state)

    count = len(tool_history)
    own_count = len(own)

    if count > TOOL_BUDGET_HARD:
        return {
            "decision": "block",
            "reason": (
                f"BLOCKED: {count} tool calls in the last {TOOL_BUDGET_WINDOW_MINUTES} "
                f"minutes exceeded hard cap ({TOOL_BUDGET_HARD}). The agent loop looks "
                f"runaway. Pause, review what's driving this volume. Override: "
                f"`export WS_TOOL_BUDGET_HARD={TOOL_BUDGET_HARD * 2}` if intentional, "
                f"or delete {RATE_LIMIT_STATE_FILE} to reset."
            ),
        }

    if own_count == TOOL_BUDGET_SOFT + 1:  # fire once per crossing, per actor
        return {
            "additionalContext": (
                f"NOTICE: this session has made {own_count} tool calls in the last "
                f"{TOOL_BUDGET_WINDOW_MINUTES} min (soft cap {TOOL_BUDGET_SOFT}). The "
                f"machine-wide hard cap is {TOOL_BUDGET_HARD}. If this is a large "
                f"batch operation, ignore; if the work feels stuck, stop and reset."
            ),
        }

    # Same-args repeat detection - this actor's last N entries
    recent_sigs = [entry[0] for entry in own[-TOOL_REPEAT_THRESHOLD:]]
    if len(recent_sigs) >= TOOL_REPEAT_THRESHOLD and all(s == signature for s in recent_sigs):
        return {
            "additionalContext": (
                f"NOTICE: {tool_name} fired {TOOL_REPEAT_THRESHOLD} times in a row with "
                f"identical args. If this is a deliberate retry, continue. If you're "
                f"stuck on the same operation, change approach."
            ),
        }

    return None


# ============================================================
# check_graph_first — a code lookup asks the graph before it greps
# ============================================================
#
# The operator's standing instruction, given four times: 2026-08-27 twice (the
# second in capitals), then 2026-08-29 twice, the last one asking for a
# MECHANISM rather than another note. "decide yourself how to record it, so
# that you cannot let yourself break the rules." (translated)
#
# A written rule had already failed three times. There is also an always-on
# UserPromptSubmit hook that names matching indexed symbols on every code-shaped
# prompt, and the relapses happened anyway, with that text on screen. A reminder
# that is ignored while visible is not a weaker control than this one, it is a
# different kind of thing: advice. This is a wall.
#
# What it refuses: the FIRST code-shaped search of a session, when no
# `codegraph_explore` has been attempted yet. One explore unlocks the session.
# The bar is deliberately low, because the failure being fixed is skipping the
# graph ENTIRELY on a question, not under-using it later in a long session.
#
# What it never refuses, by construction:
#   - a repo with no `.codegraph/` index, matching the global instruction to
#     skip CodeGraph entirely where it is not indexed;
#   - a search over `.tmp/`, `/tmp`, logs, markdown or JSON, which is reading
#     output rather than locating code;
#   - anything at all once an explore has been ATTEMPTED. A failed or empty
#     explore still unlocks, because the rule is "ask the graph first", not
#     "the graph must answer". A graph that is down must not wedge the session.

_GRAPH_STATE_DIR = _state_dir() / "graph-first"

# Bash utilities that reach source code. Two groups, one list.
#
# SEARCHING: grep and its kin, plus `find ... -name` over source. `find` was
# named in this comment and absent from the tuple until 2026-08-29, so the
# comment described a rule the code did not have.
#
# READING: sed, awk, cat, head, tail. These are not searches, and leaving them
# out was not an oversight in reasoning but a measured bypass: earlier in this
# same session, with the wall armed, `sed -n '1,40p' tests/<file>.py` opened a
# source file and the wall never saw it. A rule that stops `grep scripts/` and
# allows `cat scripts/x.py` is not a rule. Log and scratch uses of all five are
# filtered by `_NOT_CODE_HINTS` below, which is why they can be listed here at
# all.
_SEARCH_BINARIES = ("grep", "rg", "egrep", "fgrep", "ag", "ack", "ast-grep", "sg",
                    "find", "sed", "awk", "cat", "head", "tail")

# The shell form of the graph, documented in the global instructions as the one
# that "always works". A Bash call carrying it unlocks the session exactly as
# the MCP tool does, and that redundancy is what lets this wall be absolute:
# the MCP matcher lives in gitignored machine-local settings and can be absent,
# but the `Bash` matcher is present in every platform template.
# Two independent unlock doors mean a missing matcher can never cage a session,
# so the rule needs no escape hatch. See `check_graph_first`.
#
# Resolved in PROGRAM POSITION, not matched anywhere in the command line. A
# whitespace-delimited regex over the whole command made the word `codegraph`
# the key to its own wall: MEASURED 2026-08-31, `grep -rn codegraph scripts/`
# was allowed AND stamped the marker, so the session's first code grep both
# passed and disarmed the wall permanently. So did
# `echo codegraph ; grep -rn foo scripts/`. Heredoc bodies are blanked first,
# because a body is data the command writes, never a program it runs.
def _bash_asked_the_graph(command: str) -> bool:
    """True when a segment of `command` actually RUNS the graph CLI."""
    return any("codegraph" in _program_candidates(seg) for seg in _SEGMENT_SPLIT_RE.split(strip_heredocs(command)))

# Reading output, not locating code. Any of these in the target and the search
# is none of this check's business.
# `tmp/` covers the checkout's scratch (`runtime:checkout-scratch/tmp/<checkout>` in the
# data repository since 2026-09-25, `.tmp/` inside the checkout before), `/tmp/`
# and any scratch directory under another name, in one token. Spelling the system path out drew a hardcoded-temp-path finding
# from both linters, and two suppression comments to say "this is a substring I
# match, not a path I open" is worse than one token that needs neither.
_NOT_CODE_HINTS = ("tmp/", ".log", ".json", ".jsonl", ".md",
                   ".txt", ".html", ".csv", "node_modules", ".venv",
                   "__pycache__", ".git/")

# The trees whose contents are the code this rule is about. Matched as whole
# path segments: `path="scripts"` carries no trailing slash and a substring test
# for "scripts/" missed it, which the parametrised case caught on the first run.
_CODE_TREE_RE = re.compile(r"(^|[\s/\\'\"])(scripts|tests)([\s/\\'\"]|$)")
_CODE_HINTS = (".py", ".claude/hooks", ".claude\\hooks")


def actor_id(payload: dict) -> str:
    """Who is making this call: the main session, or one dispatched agent.

    MEASURED on live payloads, 2026-08-29. A subagent's PreToolUse carries the
    SAME `session_id` AND the same `transcript_path` as the session that
    dispatched it. The only field separating them is `agent_id`, which the main
    session does not carry at all. Neither wall knew that. Both keyed their
    per-session state on `session_id` alone, so five dispatched agents and the
    session that dispatched them shared one budget and one stamp: 36 hook calls
    arrived in 25 seconds and 2 of them were the session's own.

    One shared key, two opposite failures:

    - the graph wall was UNLOCKED by an agent. A subagent calling
      `codegraph_explore` stamped the dispatching session's marker, so the
      session never had to ask the graph itself. The hole closed on the morning
      of 2026-08-29 reopened on every dispatch.
    - the fan-out wall was TRIPPED by an agent. The agents' own reading filled
      the parent's budget, so the wall refused the session for having done the
      exact thing the wall exists to demand.

    Pure and public, so both directions are measurable on synthetic input.
    """
    return str(payload.get("agent_id") or "").strip() or "main"


def _state_key(session_id: str, actor: str) -> str:
    """One filesystem-safe name per (session, actor) pair."""
    return re.sub(r"[^A-Za-z0-9_-]", "_",
                  f"{session_id or 'unknown'}~{actor or 'main'}")[:96]


def _graph_marker(session_id: str, actor: str = "main") -> Path:
    return _GRAPH_STATE_DIR / f"{_state_key(session_id, actor)}.stamp"


def is_code_search(tool_name: str, tool_input: dict) -> bool:
    """True when this call is a code lookup the graph should have answered.

    Pure apart from its arguments, so both directions are measurable on
    synthetic input. That matters here more than usual: over a session where
    the marker already exists this predicate decides nothing, so deleting its
    body would change no live result and the rule would quietly stop existing.
    """
    if tool_name in ("Grep", "Glob"):
        target = " ".join(str(tool_input.get(k, ""))
                          for k in ("pattern", "path", "glob"))
    elif tool_name == "Read":
        # Opening a source file to learn what it does is the same lookup as
        # grepping for it, and the operator's instruction names both: "before
        # any grep or Read". The first version of this predicate answered False
        # for every Read, and `tests/..._four_times_and_obeyed_none.py` pinned
        # `Read scripts/sentinel.py` in the NOT-a-search list, so the hole was
        # asserted as correct. Measured 2026-08-29 on the live wall: a fresh
        # session refused a Grep of `scripts/` and allowed a Read of
        # `scripts/sentinel.py`, which is the whole rule walked around in one
        # tool call.
        target = str(tool_input.get("file_path", ""))
    elif tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        # Program position, per segment, AND the segment must carry something to
        # search. The variable here was named `first` while `re.split` returned
        # EVERY word, so a reader binary anywhere in the line marked the call a
        # code lookup, and the whole line then became the target that
        # `_CODE_HINTS` matched `.py` in. MEASURED 2026-08-31:
        # `python scripts/thread.py list | tail -5` answered True and was refused
        # as the session's first code lookup. It runs a CLI and reads its output.
        #
        # The non-flag argument is the second half of the rule. `tail -5` and
        # `head -20` at the end of a pipe read a stream, not a file, so a search
        # binary with nothing but flags is not a search. Only the matching
        # segments become the target, so a path elsewhere in the line no longer
        # decides the verdict.
        # `_SEGMENT_SPLIT`, not `_SEGMENT_SPLIT_RE`. The latter also splits on
        # parentheses, for the release wall's subshells, and an `ast-grep
        # --pattern 'os.kill($P, 0)' scripts/x.py` then lost `scripts/x.py` into
        # a third segment and stopped reading as a code search. Caught by the
        # existing fixture list on the first run.
        searching = []
        for seg in _SEGMENT_SPLIT.split(strip_heredocs(command)):
            progs = _program_candidates(seg)
            if not set(progs) & set(_SEARCH_BINARIES):
                continue
            words = seg.split()
            at = next((i for i, w in enumerate(words)
                       if w.rsplit("/", 1)[-1] in _SEARCH_BINARIES), None)
            if at is None:
                continue
            if not any(not w.startswith("-") for w in words[at + 1:]):
                continue  # flags only: reading a stream, not a corpus
            searching.append(seg)
        if not searching:
            return False
        target = " ".join(searching)
    else:
        return False

    lowered = target.lower()
    if any(hint in lowered for hint in _NOT_CODE_HINTS):
        return False
    if any(hint in lowered for hint in _CODE_HINTS):
        return True
    if _CODE_TREE_RE.search(lowered):
        return True
    # A Grep with no path at all sweeps the whole repo, which is the case the
    # graph answers best and the one most often reached for by reflex.
    return tool_name == "Grep" and not tool_input.get("path")


# THE RULE IS UNCONDITIONAL, with no refusal count to wait out, on the
# operator's instruction of 2026-08-29: "if there is a hard rule, it is ALWAYS
# carried out, unconditionally" (translated). Two independent doors unlock it, so a session is
# caged only if BOTH are shut:
#   1. the MCP tool, via the `mcp__codegraph__.*` matcher in the tracked
#      platform templates;
#   2. `codegraph explore` in a Bash command, via the `Bash` matcher.
# Either one stamps the marker BEFORE the call runs, so a graph that is down, an
# MCP server that is not connected, and a CLI that is not installed all still
# unlock. The rule is "ask the graph first", never "the graph must answer".


# ============================================================
# Ask the memory first
#
# The sibling of `check_graph_first`, one layer up: that wall is about locating
# CODE, this one is about recalling what the operator already decided. Operator
# instruction 2026-09-10, in the same terms he used for the graph: this
# workspace goes to its memory first, by default, for what it holds. Until
# 2026-10-04 that memory was a different store; the operator then removed it
# from the workspace (HOS-198), in
# his words, translated: "Let Hindsight be the main memory, and codegraph only
# for work with the code (the engine)." and "Reconfigure EVERYTHING onto Hindsight." So the wall
# now asks for a Hindsight query, and nothing else opens it.
#
# WHY A WALL AND NOT A SENTENCE. The identical instruction about CodeGraph was
# given four times in prose and relapsed three times anyway, with an always-on
# hook naming matching symbols on screen while it happened (see the block above
# `check_graph_first`). A reminder that is ignored while visible is advice. The
# operator asked for a workspace that ALWAYS uses this, and only a wall spells
# always.
#
# THE TRIGGER, and it is deliberately narrow. `check_graph_first` fires on a
# code-shaped search, which is a mechanical signal. "About to answer from memory"
# is not: the situational probe measured exactly that blindness, and no tool call
# announces it. So this fires on the one shape that IS mechanical: a lookup
# aimed at the memory corpus itself, by Read, by Grep, by Glob, or by running the
# other index over it. Reaching for what the operator recorded, without having
# asked the store that holds it, is the case this catches. Everything else is
# left to the resident rule, which is honest about being advice. The trigger did
# not change on 2026-10-04; only the door did.
#
# TWO DOORS OPEN IT, and neither requires Hindsight to answer:
#   1. an MCP call to a QUERY tool on a server whose name carries `hindsight`.
#      The server is matched by pattern, because its name is configuration
#      (this machine registers two, one per MCP route, and a rename must not
#      cage a session); the TOOL is matched against a closed list of read
#      verbs, `_HINDSIGHT_MCP_QUERIES`, because a retain or a delete is not
#      asking the memory anything and must never be the key. That call
#      reaches this dispatcher only through the `mcp__.*hindsight.*`
#      PreToolUse matcher, added to the three tracked platform templates the
#      same day. MEASURED 2026-10-04 before it: the templates and the live
#      settings file registered `_dispatch.py` under six matchers and none of
#      them matched a Hindsight tool, so this door would have been a door the
#      dispatcher never sees, and the previous store's own MCP door (any tool
#      whose name carried that store's name) had been exactly that since
#      2026-09-11.
#   2. a Bash segment that RUNS the `hindsight` CLI with a query subcommand,
#      `_HINDSIGHT_CLI_QUERIES`. It rides the `Bash` matcher, present in every
#      settings file in the repository, so a clone whose live settings predate
#      the MCP matcher still has a way out.
# The stamp lands at PreToolUse, BEFORE the call runs, so a server that is down,
# an MCP route that is not connected and a CLI that errors all still unlock. The
# rule is "ask", never "the memory must answer": a control that can wedge a
# session gets switched off, and then it protects nothing.
_HINDSIGHT_STATE_DIR = _state_dir() / "hindsight-first"

# The MCP tools that READ the memory, by their name with the server prefix and
# an optional `hindsight_` tool prefix removed (the plugin's server prefixes
# its own tool names, the bridge's does not). Read from the tool lists of both
# servers registered on this machine, 2026-10-04. Deliberately absent: retain,
# sync_retain, every create/update/delete/clear/invalidate/refresh, and the
# plugin's ingest_document, capture_initiative, diagnose and sync_status, which
# write or report on the server rather than query what it holds.
_HINDSIGHT_MCP_QUERIES = frozenset({
    "recall", "reflect",
    "search_knowledge_base", "search_knowledge_pages",
    "get_knowledge_base_tree", "get_knowledge_page",
    "read_knowledge_page", "list_knowledge_pages",
    "list_documents", "get_document",
    "list_memories", "get_memory",
    "list_mental_models", "get_mental_model",
})

# The CLI's query subcommands as (group, verb), read from `hindsight --help`
# and each group's own `--help` on 2026-10-04. Same rule as the MCP list: a
# verb that stores, deletes, refreshes or exports is not a query.
_HINDSIGHT_CLI_QUERIES = frozenset({
    ("memory", "recall"), ("memory", "reflect"), ("memory", "list"),
    ("memory", "get"), ("memory", "history"),
    ("document", "list"), ("document", "get"),
    ("knowledge-base", "search"), ("knowledge-base", "get-page"),
    ("knowledge-base", "tree"),
    ("mental-model", "list"), ("mental-model", "get"),
    ("mental-model", "history"),
    ("entity", "list"), ("entity", "get"),
})

# Global options of the CLI that take a value, so the value is never read as a
# subcommand. From `hindsight --help`, 2026-10-04.
_HINDSIGHT_CLI_VALUED = frozenset({"-o", "--output", "-p", "--profile"})


def _hindsight_present() -> bool:
    """Is Hindsight configured on this machine at all?

    The question the sibling `.claude/hooks/hindsight-recall-inject.py` already
    asks, asked the same way: its API key file, `HINDSIGHT_INJECT_KEY_FILE`
    when set, else `~/.config/hindsight/secrets/hindsight-api-key`. That hook
    is silent when the file is absent, "which is the state of every machine
    that does not run a Hindsight server", and this wall is silent for the same
    reason: a public clone with no Hindsight must not be caged behind a query
    it cannot make. Only the file's existence is read, never its content, so no
    secret passes through this process.
    """
    try:
        key_file = Path(os.environ.get(
            "HINDSIGHT_INJECT_KEY_FILE",
            str(Path.home() / ".config" / "hindsight" / "secrets"
                / "hindsight-api-key"))).expanduser()
        return key_file.is_file()
    except (OSError, RuntimeError) as exc:
        # RuntimeError: `Path.home()` with no resolvable home. Either way the
        # answer is "not configured", which leaves the wall silent.
        print(f"[_dispatch:hindsight_first] key file unreadable: {exc}",
              file=sys.stderr)
        return False

# The corpus this wall is about. A path naming it is a memory lookup whoever
# spelled it; `auto-memory` is the directory, `MEMORY.md` its index.
_MEMORY_CORPUS_HINTS = ("auto-memory", "memory.md")

# Running the OTHER index over the same corpus is the case worth catching: it
# means the reader knew to look something up and reached past the store that was
# built for it.
_MEMORY_CLI_HINTS = ("memory-index.py", "memory.py")


def _hindsight_marker(session_id: str, actor: str = "main") -> Path:
    return _HINDSIGHT_STATE_DIR / f"{_state_key(session_id, actor)}.stamp"


def _mcp_asked_hindsight(tool_name: str) -> bool:
    """True when `tool_name` is a Hindsight MCP tool that QUERIES the memory.

    `mcp__<server>__<tool>`. The server is matched on the substring
    `hindsight`, case folded; the tool, with an optional `hindsight_` prefix
    removed, must be in `_HINDSIGHT_MCP_QUERIES`. A tool name merely carrying
    the word, as the previous store's door accepted, is not enough: that let a
    write tool on the same server be the key.
    """
    if not tool_name.startswith("mcp__"):
        return False
    server, sep, tool = tool_name[len("mcp__"):].partition("__")
    if not sep or "hindsight" not in server.lower():
        return False
    tool = tool.lower()
    if tool.startswith("hindsight_"):
        tool = tool[len("hindsight_"):]
    return tool in _HINDSIGHT_MCP_QUERIES


def _bash_asked_hindsight(command: str) -> bool:
    """True when a segment of `command` actually RUNS a Hindsight CLI query.

    PROGRAM POSITION, never a substring of the line. The previous store's
    version of this function was first written as a substring test on its
    script name and answered True for `echo` of that name, so the sentence
    "ask first" was satisfiable by typing the name. Caught by the negative case
    on the first run of the wall's own fixtures, and kept here: `_program_candidates` must END on `hindsight`,
    so `echo hindsight memory recall` (program `echo`) does not register.

    Then the subcommand, because `hindsight memory retain` runs the CLI and
    asks nothing. The words after the program are read with global options
    and their values skipped, and the first two positionals must be a pair in
    `_HINDSIGHT_CLI_QUERIES`.
    """
    for seg in _SEGMENT_SPLIT_RE.split(strip_heredocs(command)):
        progs = _program_candidates(seg)
        if not progs or progs[-1] != "hindsight":
            continue
        words = [w.strip(_WORD_TRIM) for w in seg.split()]
        try:
            start = next(i for i, w in enumerate(words)
                         if w.rsplit("/", 1)[-1] == "hindsight")
        except StopIteration:
            continue
        positionals: list[str] = []
        skip_value = False
        for word in words[start + 1:]:
            if skip_value:
                skip_value = False
                continue
            if word.startswith("-"):
                skip_value = word in _HINDSIGHT_CLI_VALUED
                continue
            positionals.append(word)
            if len(positionals) == 2:
                break
        if tuple(positionals) in _HINDSIGHT_CLI_QUERIES:
            return True
    return False


def is_memory_lookup(tool_name: str, tool_input: dict) -> bool:
    """True when this call reaches the operator's recorded memory.

    Pure apart from its arguments, for the same reason `is_code_search` is: over
    a session where the marker already exists this predicate decides nothing, so
    a hollowed-out body would change no live result and the rule would quietly
    stop existing while every test still passed.
    """
    if tool_name in ("Grep", "Glob"):
        target = " ".join(str(tool_input.get(k, ""))
                          for k in ("pattern", "path", "glob"))
    elif tool_name == "Read":
        target = str(tool_input.get("file_path", ""))
    elif tool_name == "Bash":
        command = strip_heredocs(str(tool_input.get("command", "")))
        # A segment that RUNS one of the memory CLIs, not one that merely
        # mentions it. `grep memory-index.py notes.txt` reads a file about the
        # tool; it is not a memory lookup, and the program-position test is what
        # keeps the two apart.
        for seg in _SEGMENT_SPLIT.split(command):
            progs = _program_candidates(seg)
            if any(h in p for p in progs for h in _MEMORY_CLI_HINTS):
                return True
            if any(h in seg for h in _MEMORY_CLI_HINTS) and " query" in seg:
                return True
        return any(h in command.lower() for h in _MEMORY_CORPUS_HINTS)
    else:
        return False
    return any(h in target.lower() for h in _MEMORY_CORPUS_HINTS)


def check_hindsight_first(payload: dict):
    tool_name = payload.get("tool_name", "")
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        return None

    marker = _hindsight_marker(session_id, actor_id(payload))

    asked_hindsight = _mcp_asked_hindsight(tool_name) or (
        tool_name == "Bash"
        and _bash_asked_hindsight(str((payload.get("tool_input") or {})
                                      .get("command", "")))
    )
    if asked_hindsight:
        try:
            _HINDSIGHT_STATE_DIR.mkdir(parents=True, exist_ok=True)
            marker.write_text("unlocked", encoding="utf-8")
        except OSError as exc:
            print(f"[_dispatch:hindsight_first] could not stamp: {exc}",
                  file=sys.stderr)
        return None

    # No Hindsight, no rule. A clone with no Hindsight configured must not be
    # caged behind a query that cannot succeed.
    if not _hindsight_present():
        return None
    if marker.exists():
        return None
    if not is_memory_lookup(tool_name, payload.get("tool_input") or {}):
        return None

    return {
        "decision": "block",
        "_policy_deny": True,
        "reason": (
            "Ask Hindsight first. This call reaches the operator's recorded "
            "memory and no Hindsight query has run in this session yet.\n\n"
            "Hindsight is this workspace's main memory for records: operator "
            "decision 2026-10-04 (HOS-198). Operator instruction, 2026-09-10: this workspace goes "
            "to its memory first, by default, for what it holds.\n\n"
            "THE WAY OUT, which unlocks the whole session. Either query "
            "Hindsight through its MCP tools (recall, reflect, "
            "search_knowledge_base, or the plugin's "
            "hindsight_search_knowledge_pages), or run the CLI:\n"
            "  hindsight memory recall <bank> \"<what you are after>\"\n"
            "Any ATTEMPT unlocks, including one that errors or returns "
            "nothing, so a server that is down or a route that is not "
            "connected can none of them wedge you. A retain, a delete or "
            "`echo hindsight ...` is not a query and does not unlock.\n\n"
            "Then open the records it names. A hit is a POINTER, not a fact "
            "(.claude/rules/memory-discipline.md), and reading the file it "
            "points at is still the step that settles anything consequential."
        ),
    }


def check_graph_first(payload: dict):
    tool_name = payload.get("tool_name", "")
    session_id = str(payload.get("session_id", "")).strip()

    # No session, no rule. "The first code search of the SESSION" needs a
    # session to be the first of, and every real PreToolUse payload carries one.
    # A payload without it is another test driving a different wall, and keying
    # them all on one shared "unknown" marker would make those suites depend on
    # each other's order. That is not hypothetical: the rate limiter's single
    # shared state file did exactly this to a wall test earlier on 2026-08-29.
    if not session_id:
        return None

    # Per ACTOR, not per session. An agent must ask the graph for itself; its
    # stamp must not unlock the session that dispatched it. See `actor_id`.
    marker = _graph_marker(session_id, actor_id(payload))

    # Stamp on the explore ITSELF, at PreToolUse, before it runs. Stamping on
    # success instead would mean a graph outage locks the session out of grep
    # too, and a control that wedges a session gets switched off.
    #
    # Both doors are read here: the MCP tool by name, and the shell CLI by
    # command. The second is what makes the rule safe to enforce absolutely.
    asked_the_graph = "codegraph" in tool_name.lower() or (
        tool_name == "Bash"
        and _bash_asked_the_graph(str((payload.get("tool_input") or {})
                                      .get("command", "")))
    )
    if asked_the_graph:
        try:
            _GRAPH_STATE_DIR.mkdir(parents=True, exist_ok=True)
            marker.write_text("unlocked", encoding="utf-8")
        except OSError as exc:
            print(f"[_dispatch:graph_first] could not stamp: {exc}",
                  file=sys.stderr)
        return None

    if not (WORKSPACE / ".codegraph").is_dir():
        return None
    if marker.exists():
        return None
    if not is_code_search(tool_name, payload.get("tool_input") or {}):
        return None

    return {
        "decision": "block",
        "_policy_deny": True,
        "reason": (
            "Ask the graph first. This call reaches source code and no "
            "codegraph query has run in this session yet.\n\n"
            "One call to `codegraph_explore` returns the verbatim source of "
            "the relevant symbols PLUS who calls them, which grep cannot "
            "produce at any number of round trips. The operator gave this "
            "instruction four times before it became a wall, and made it "
            "unconditional on the fifth: there is no refusal count to wait "
            "out and no number of attempts that lets this through.\n\n"
            "TWO WAYS OUT, both of which unlock the whole session:\n"
            "  - call `codegraph_explore` with the symbol or file names in "
            "this lookup, or\n"
            "  - run `codegraph explore \"<names>\"` in Bash.\n"
            "Any ATTEMPT unlocks, including one that errors or returns "
            "nothing, so a graph that is down, an MCP server that is not "
            "connected and a missing CLI can none of them wedge you.\n\n"
            "Never refused at all: anything under this checkout's scratch "
            "(`python scripts/data-path.py scratch`) or another scratch "
            "directory, logs, markdown, JSON, CSV and HTML. This wall is "
            "about locating and reading CODE."
        ),
    }


# ============================================================
# check_fanout_first — a wide stretch of work considers agents
# ============================================================
#
# "Did not use an agent" is an ABSENCE, not an action, so there is no single
# tool call to refuse. This check watches the SHAPE OF A STRETCH instead: how
# many distinct files this session has investigated by hand since it last
# considered fanning out.
#
# Distinct paths, not call count, and that choice is the whole precision of the
# rule. Thirty calls against one file is deep work and inherently serial;
# twelve calls against twelve files is a fan-out that was not dispatched. A
# counter keyed on calls would fire on the first kind, which is the sort of
# false refusal that gets a control switched off.
#
# Two things reset the count: dispatching an Agent or a Workflow, or
# `python scripts/fanout-note.py "<why this is serial>"`, which also appends the
# reason to a log the operator can read.
#
# ADVISORY SINCE 2026-09-27 (operator's approval of the prompting audit, item
# 10). It refuses nothing: when the budget is crossed it adds one note carrying
# Anthropic's criterion for when subagents help, and starts the count again.

_FANOUT_STATE_DIR = _state_dir() / "fanout"

# The tools that ARE fanning out. `Agent` is this harness's name; `Task` is the
# older one and `Workflow` the orchestrated form. `SendMessage` continues an
# agent already dispatched, which is delegation as much as a fresh dispatch.
# All four clear the budget.
#
# `SendMessage` joined 2026-09-27, and it needs its own matcher group to reach
# this check at all: the Claude Code hooks reference says PreToolUse fires on
# every tool call except `EndConversation`, and that a matcher of letters and
# `|` is a list of EXACT names, so `Agent|Task|Workflow` never saw it. Until
# then continuing an agent left the count running and the note could fire on a
# session that was delegating. A clone whose settings.local.json predates the
# group still has that gap until `bash scripts/setup-platform.sh` is re-run.
FANOUT_TOOLS = ("Agent", "Task", "Workflow", "SendMessage")

# The tools whose paths `investigated_paths` charges for, and so the only tools
# the advisory note may land on. A write, an edit or a notebook never gets it.
FANOUT_COUNTED_TOOLS = ("Read", "Grep", "Glob", "Bash")

# How many distinct files may be investigated by hand before the question is
# forced. A first setting, tunable by env rather than by editing this file: the
# right number is a property of how the operator works, and it should be moved
# on evidence rather than on argument.
FANOUT_PATH_BUDGET = int(os.environ.get("WS_FANOUT_BUDGET", "12"))

# Reading output, not investigating a corpus. Same list the graph wall uses for
# the same reason: a session must be able to read its own logs and scratch
# files without spending budget it did not mean to spend.
_FANOUT_IGNORE = ("tmp/", ".log", ".jsonl", "node_modules", ".venv",
                  "__pycache__", ".git/",
                  # Device and kernel nodes are plumbing in a command line, not
                  # a corpus: `2>/dev/null` on a real search charged a file.
                  "/dev/", "/proc/", "/sys/")

# A FORWARD slash, and only a forward slash. The separator class used to
# accept a backslash as well, for Windows paths, and the test below
# normalised backslashes to slashes before looking -- so every regex escape
# in a command (`\s`, `\n`, `\t`, `\d`) and every line continuation was
# counted as a distinct file. MEASURED 2026-08-29: one heredoc patching ONE
# file reported fourteen. This workspace is WSL-only and runs no Windows
# tooling, so nothing real is lost by dropping the backslash form.
#
# The GUARD in `investigated_paths` is the enforcement, not this class. Putting
# the backslash back here alone is a mutation that survives, because the guard
# tests for a literal forward slash and rejects `\s` whatever this matched. The
# narrower class only avoids building junk tokens to iterate.
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_./-]*/[A-Za-z0-9_./-]*")


def _fanout_marker(session_id: str, actor: str = "main") -> Path:
    return _FANOUT_STATE_DIR / f"{_state_key(session_id, actor)}.json"


# The second of three copies of the heredoc opener in this file, and it had
# neither guard against a here-string. See `_HEREDOC_START_RE` for the
# measurement: `<<<word` is read as a heredoc opening on `word`, the terminator
# is never found, and `strip_heredocs` drops every line after it. Here that
# means the fan-out counter stops seeing the paths those lines name, so the
# wall under-counts a session's hand-reads - a guard weakened by a typo the
# operator did not make.
_HEREDOC_OPEN = re.compile(r"(?<!<)<<(?!<)-?\s*[\"\']?([A-Za-z_][A-Za-z0-9_]*)[\"\']?")


def strip_heredocs(command: str) -> str:
    """The command line with every heredoc BODY removed.

    A heredoc body is content the command writes; it is not a corpus the caller
    is reading. MEASURED 2026-08-29: a `cat > msg.txt <<'EOF' ... EOF` holding a
    commit message spent the whole fan-out budget on the file names the message
    NAMED, and the wall then refused the commit. The same shape charges a patch
    script for the paths it edits, twice: once in the redirect and once in the
    body.

    Pure, so both directions are measurable on synthetic input. The delimiter is
    matched at the start of a line, per the shell, and an unterminated heredoc
    runs to the end, which is also what the shell does.
    """
    lines = command.split("\n")
    kept, i = [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        markers = _HEREDOC_OPEN.findall(line)
        i += 1
        for marker in markers:
            while i < len(lines) and lines[i].strip() != marker:
                i += 1
            i += 1        # drop the terminator line too
    return "\n".join(kept)


# The commands that READ a file so a human can look at it. An allowlist, not a
# denylist, and that is the whole design: the wall exists to notice a session
# opening file after file by hand, and a denylist has to anticipate every
# command that is not that. MEASURED 2026-08-29, twice in one hour: `pytest a b
# c` and `ruff a b` were charged as five hand-reads and the wall refused the
# next step, and a `git commit` was charged for the files its message named.
# Running a suite, linting a set, committing a change: each names paths, none
# is a person reading them.
_READER_BINARIES = (
    "grep", "rg", "egrep", "fgrep", "ag", "ack", "ast-grep", "sg",
    "find", "sed", "awk", "cat", "head", "tail", "less", "more",
    "nl", "wc", "diff", "od", "xxd", "strings", "jq", "yq",
)

# Where one command ends and the next begins. Only the FIRST word of a segment
# is the binary, so `git commit -F msg` and `grep -n x a.py` split cleanly.
_SEGMENT_SPLIT = re.compile(r"(?:\|\||&&|[;|&\n])")


def reader_path_tokens(command: str) -> list[str]:
    """Path-looking tokens from the READER segments of a shell command.

    Pure, so both directions are measurable on synthetic input, which matters
    here because the allowlist decides everything: a body that returned every
    token would restore the false refusals, and one that returned none would
    delete the Bash half of the rule without failing anything else.
    """
    found: list[str] = []
    for segment in _SEGMENT_SPLIT.split(strip_heredocs(command)):
        words = segment.split()
        if not words:
            continue
        # Skip a leading `sudo`, `time`, or `VAR=value` assignment.
        #
        # `.split()[0]` used to sit on the end of the basename read. A word
        # coming out of `segment.split()` holds no whitespace, so it was
        # redundant on every input except one: a word ENDING in `/` has the
        # empty string as its basename, `"".split()` is `[]`, and `[0]` raised
        # IndexError. MEASURED 2026-08-31 on `grep -rn foo \` + newline +
        # `  scripts/`, which is an ordinary backslash-continued grep: the
        # continuation segment is a bare directory. `check_fanout_first` died,
        # `main()` caught it, printed one stderr line, allowed the tool, and the
        # path was never charged to the budget the wall exists to keep.
        i = 0
        while i < len(words):
            word = words[i]
            if word in ("sudo", "time", "command", "nohup"):
                i += 1
                continue
            if "=" in word.rsplit("/", 1)[-1] and not word.startswith("-"):
                i += 1
                continue
            break
        if i >= len(words):
            continue
        binary = words[i].rsplit("/", 1)[-1]
        if binary in _READER_BINARIES:
            # The ARGUMENTS, not the command word. Scanning the whole segment
            # charged `/bin/cat` and `/usr/bin/grep` as files being read, which
            # is how a reader invoked by absolute path spent two budget slots
            # for one file.
            found.extend(_PATH_TOKEN.findall(" ".join(words[i + 1:])))
    return found


def _runs_fanout_note(command: str) -> bool:
    """True when a segment of `command` actually RUNS `scripts/fanout-note.py`.

    Program position, so a reader binary in the same segment is not a door. See
    the measurement at the call site in `check_fanout_first`.
    """
    return any("fanout-note.py" in _program_candidates(seg) for seg in _SEGMENT_SPLIT_RE.split(strip_heredocs(command)))


def investigated_paths(tool_name: str, tool_input: dict) -> set[str]:
    """The distinct files this one call investigates by hand.

    Pure, so both directions are measurable on synthetic input. That matters
    more than usual here: over a session already under budget this function
    decides nothing, so a body that returned an empty set would change no live
    result and the rule would quietly stop existing.
    """
    if tool_name in ("Read", "Grep", "Glob"):
        candidates = [str(tool_input.get(k, ""))
                      for k in ("file_path", "path", "glob", "pattern")]
    elif tool_name == "Bash":
        candidates = reader_path_tokens(str(tool_input.get("command", "")))
    else:
        return set()

    found = set()
    for raw in candidates:
        token = raw.strip().strip("'\"")
        # `alnum` as well as the slash: a bare `/`, or `../`, is a separator
        # someone typed, not a file that was opened.
        if not token or "/" not in token or not any(c.isalnum() for c in token):
            continue
        lowered = token.lower()
        if any(hint in lowered for hint in _FANOUT_IGNORE):
            continue
        found.add(token.replace("\\", "/"))
    return found


def _fanout_state(marker: Path) -> set[str]:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, list) else set()


def check_fanout_first(payload: dict):
    tool_name = payload.get("tool_name", "")
    session_id = str(payload.get("session_id", "")).strip()
    if not session_id:
        return None          # same reason as check_graph_first: no session, no rule

    # A subagent IS the fan-out, and its reading is not the session's
    # hand-work. The marker is keyed by actor as well, so removing this early
    # return still cannot let an agent spend the session's budget.
    actor = actor_id(payload)
    if actor != "main":
        return None

    marker = _fanout_marker(session_id, actor)

    # Clearing the budget. Both doors write the same empty state, so the rule
    # cannot tell them apart afterwards and does not need to: what it enforces
    # is that ONE of them happened.
    command = str((payload.get("tool_input") or {}).get("command", ""))
    # Resolved in PROGRAM POSITION. A substring test made every command that
    # merely NAMED the script a door, and the refusal text asserts the door is
    # audited: "It appends the reason with a timestamp to a log the operator
    # reads." MEASURED 2026-08-31 with the budget at 12 and the state seeded at
    # 20 paths, `grep -n fanout-note.py scripts/*.py` and
    # `ls -la scripts/fanout-note.py` each reset the count to zero and appended
    # nothing. The first of those is itself the hand-reading the wall bounds.
    cleared = tool_name in FANOUT_TOOLS or _runs_fanout_note(command)
    if cleared:
        try:
            _FANOUT_STATE_DIR.mkdir(parents=True, exist_ok=True)
            marker.write_text("[]", encoding="utf-8")
        except OSError as exc:
            print(f"[_dispatch:fanout] could not clear: {exc}", file=sys.stderr)
        return None

    seen = _fanout_state(marker) | investigated_paths(
        tool_name, payload.get("tool_input") or {})
    if not seen:
        return None

    try:
        _FANOUT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    except OSError as exc:
        print(f"[_dispatch:fanout] could not count: {exc}", file=sys.stderr)

    # Note only on the tools this check charges for, never on an edit.
    if tool_name not in FANOUT_COUNTED_TOOLS:
        return None

    if len(seen) <= FANOUT_PATH_BUDGET:
        return None

    # Advisory, once per crossing: the count restarts after the note, so the
    # next note needs another full budget of hand reading rather than arriving
    # on every call from here on.
    try:
        marker.write_text("[]", encoding="utf-8")
    except OSError as exc:
        print(f"[_dispatch:fanout] could not reset after notice: {exc}",
              file=sys.stderr)

    return {
        "additionalContext": (
            f"Delegation note (advisory, nothing was blocked). This session has "
            f"read {len(seen)} distinct files by hand since its last agent or "
            f"workflow dispatch, serial note or notice like this one. "
            f"Subagents help when the remaining work splits into pieces that "
            f"can run in parallel, need isolated context, or are independent "
            f"workstreams; give each one a self-contained brief. For simple "
            f"tasks, sequential steps, single-file edits, or work that needs "
            f"context carried across steps, keep working directly. If you "
            f"continue directly and want the reason on record for the "
            f"operator: python scripts/fanout-note.py \"<why this is serial>\". "
            f"Not counted: scratch directories, logs and JSONL. Budget "
            f"{FANOUT_PATH_BUDGET} files (WS_FANOUT_BUDGET)."
        ),
    }


# ============================================================
# Release gate: no commit and no push without a fresh operator word
# ============================================================
#
# WHY THIS IS A WALL AND NOT A RULE.
#
# "Commit or push only when the user asks" has been written down twice: in
# `~/.claude/CLAUDE.md` and in the operator's auto-memory as
# the rule that approval to do the work is never approval to commit or push it,
# whose own text records the first breach on 2026-08-20. It happened again on 2026-08-30, and the mechanism is worth
# stating exactly, because it is not forgetfulness and a third copy of the
# sentence would not have stopped it:
#
#   The operator authorised ONE push. That authorisation was then written into
#   a handoff summary as "the operator's approval is already given", survived a
#   context compaction, and was read back on the other side as a STANDING FACT
#   about the world rather than as a spent event. Every later decision cited the
#   summary, not the operator. An approval had been promoted from an event into
#   a state.
#
# A rule cannot defend against that, because the rule was never contradicted in
# the moment: at the point of the second push the model sincerely believed
# permission existed. What is needed is a check that re-reads the operator's
# ACTUAL most recent words at the instant of the action, so belief is replaced
# by evidence and a stale belief cannot survive a single turn.
#
# The transcript carries two records of what the operator typed, and the wall
# needs BOTH. `last-prompt` records re-emit it at every leaf and the model does
# not write them, so they establish PROVENANCE -- a task notification, Stop-hook
# feedback or a tool result is not a last-prompt, and a synthetic message cannot
# authorise anything. But `lastPrompt` is TRUNCATED at 200 characters plus an
# ellipsis: MEASURED 2026-09-03 over one session's transcript, 166 records, max
# length exactly 201, 83 of them on the cap. So the TEXT comes from the same
# turn's `type: "user"` record with `promptSource: "typed"`, whose
# `message.content` is the full string, and the capped record confirms it by
# prefix. They disagree, or the typed record is missing its confirmation, and
# the wall refuses.
#
# Fail-closed everywhere: an unreadable transcript, a missing field or an
# unrecognised command shape all refuse. A gate that opens when it cannot see is
# not a gate.

# THE AUDIT LOG DOES NOT USE `_state_dir()`, AND MUST NOT BE PUT BACK ON IT.
# `_state_dir()` is per checkout and right for GUARD state; this is an AUDIT
# TRAIL that must outlive the yard it describes (MEASURED 2026-09-08: a yard's
# record vanished with `git worktree remove`), and `authorised_by` holds the
# operator's own words, which never live in any engine checkout (operator
# directive 2026-09-08). It lives in the data repository under the registry name
# `release-journal` (`runtime:release/cache`, gitignored there, so the appends
# never touch the shared overlay index and the journal is not pushed), settled
# by the operator on 2026-09-22 (HOS-42); the history is in `HOOKS-REFERENCE.md`
# (registry name `spec-docs`). The name is a LITERAL at the call in
# `_release_log_default`: `scripts/leak-guard.py` refuses a front-door call
# whose argument is a variable.
_RELEASE_LOG_PIN = "HEADING_OS_RELEASE_LOG"

#: Spelled once, here, because this module reaches the data-root seam by
#: supplying the root rather than by letting it be read out of the ambient
#: environment alone. See `_release_log_default`.
_DATA_ROOT_PIN = "HEADING_OS_DATA"


def _release_log_default() -> Path | None:
    """The journal's registered address on THIS checkout's tree, or None.

    THE ROOT IS DERIVED FROM THIS CHECKOUT, and that is the whole reason this
    function is not one call. `get_data_root()` answers from `HEADING_OS_DATA`
    and then from the SIBLING of the workspace root, and a yard has no sibling
    overlay: MEASURED 2026-09-22 in a worktree at `<yards>/<repo>/<branch>` with
    the variable cleared, `get_data_root()` answered `<the yard>/examples`, the
    bundled demo tree, and every write under it refuses. The variable was set in
    this session (MEASURED the same day by reading the harness process's own
    environment), but it was set by the operator's login shell, so relying on it
    makes the journal a property of how the session was launched. The yard's own
    `.env` carries it, so the checkout can answer for itself.

    Order matches `load_env`, which uses `setdefault`: the ambient value wins,
    and the checkout's `.env` only fills a gap. That keeps a test's sandbox pin
    and an operator's deliberate override ahead of a file on disk, and it means
    this function never disagrees with the environment that same file produces.

    A REFUSAL IS NOT AN EXCEPTION HERE. The trees with no writable data
    root, a public clone on the bundled demo tree and the CI runner, resolve
    nothing and record nothing, with a line on stderr. That
    is a real reduction against the home-directory address, which always had
    somewhere to write, and it is the consequence of the decision that the
    journal is operator data: a tree with no data repository has nowhere for it.

    Imported inside the function, never at module scope, because this file is
    loaded on EVERY tool call and `scripts.utils.data_path` pulls in the HEADING
    OS 2 resolver behind it. `_record_denial` is the precedent and the reasoning
    is identical: this runs on releases only, which are rare, so the
    overwhelming majority of tool calls never pay for the import.
    """
    try:
        if str(WORKSPACE) not in sys.path:
            sys.path.insert(0, str(WORKSPACE))
        from scripts.utils.data_path import data_path
        from scripts.utils.paths import read_env_value

        root = None
        if not os.environ.get(_DATA_ROOT_PIN):
            value = read_env_value(WORKSPACE / ".env", _DATA_ROOT_PIN)
            if value:
                candidate = Path(value).expanduser()
                if candidate.is_dir():
                    root = candidate
        return data_path("release-journal", data_root=root)
    except Exception as exc:
        print(f"dispatch: 'release-journal' has no address on this tree "
              f"({type(exc).__name__}: {exc}); the authorised-release journal "
              f"is not written.", file=sys.stderr)
        return None


def _release_log_path() -> Path | None:
    """Where an authorised release is recorded, or None to record nothing.

    THIS RESOLVER ANSWERS ABOUT THE CHECKOUT IT SITS IN, on purpose, and the
    reason is in `_release_log_default` below with the 2026-09-22 measurement it
    stands on: a yard has no sibling overlay, so the checkout's own `.env` is
    the only thing that can answer for it. Recorded here on 2026-09-23 because a
    mechanical survey of resolvers that ignore the workspace root they are given
    found this one and `_release_log_default` among six, and a resolver whose
    PURPOSE is its own machine has to say so where the next reader will look
    rather than leave the next survey to re-decide it (HOS-95). It reads that
    `.env` with `read_env_value`, one key, and writes nothing to `os.environ`;
    the two sites repaired under HOS-95 are the ones that did.

    Resolved at CALL time, not frozen at import like `_RELEASE_STATE_DIR` was,
    and that is the containment. A constant computed at import means a test that
    sets the pin after importing this module appends to the operator's real
    audit log; with a call-time read, `monkeypatch.setenv` is enough and an
    in-process test cannot miss the seam.

    THE TRAP THIS ANSWERS. A path outside the checkout is exactly a path a test
    run can reach. `tests/conftest.py` contains `.claude/state/` by SETTING
    `HEADING_OS_STATE_DIR`, and containment that depends on a variable being set
    fails open the moment a test clears it or a child is spawned with a trimmed
    environment — the conftest's own docstrings record that happening. So there
    are two independent legs and both must fail before a real record is
    poisoned: the explicit pin, and a refusal to touch the default at all when
    this process can tell it is under pytest. An audit log is worse than most
    state to poison, because a forged line destroys the only artefact that
    answers "who authorised this".

    A THIRD LEG ARRIVED WITH THE 2026-09-22 MOVE, and it removes neither of the
    two above. The default now resolves inside the DATA repository, so a test
    that points `HEADING_OS_DATA` at a scratch tree, which is what this suite's
    isolation fixtures do, moves the resolved address with it. The pytest check
    still comes first, because that one is this process's own answer about
    itself and the other two are a caller's to set.

    Order matters. The pin wins over the pytest check, because a test that
    asserts a record IS written needs somewhere to write it. A pin that is
    RELATIVE records nothing rather than falling back to the default, which is
    the opposite of what `_state_dir()` does with a relative value and is
    deliberate: there, falling back reaches a scratch directory; here, it would
    reach the operator's live trail.
    """
    pin = os.environ.get(_RELEASE_LOG_PIN)
    if pin:
        candidate = Path(pin).expanduser()
        if candidate.is_absolute():
            return candidate
        print(f"dispatch: {_RELEASE_LOG_PIN}={pin!r} is relative; recording "
              f"nothing rather than falling back to the operator's audit log.",
              file=sys.stderr)
        return None
    if ("PYTEST_CURRENT_TEST" in os.environ
            or "PYTEST_VERSION" in os.environ
            or "PYTEST_XDIST_WORKER" in os.environ
            or "pytest" in sys.modules):
        print(f"dispatch: under pytest with no {_RELEASE_LOG_PIN}; the "
              f"authorised-release log is not written.", file=sys.stderr)
        return None
    return _release_log_default()


def _checkout_branch(root: Path) -> str:
    """The branch `root` has checked out, read from HEAD without a subprocess.

    A worktree's `.git` is a FILE holding `gitdir: <path>`, so the naive
    `root / ".git" / "HEAD"` reads nothing in the exact case this log exists
    for. Read rather than shelled out because `_record_release` is telemetry on
    a wall that runs on every Bash call, and spawning `git` here would put a
    process on the release path.
    """
    try:
        dot = root / ".git"
        if dot.is_file():
            line = dot.read_text(encoding="utf-8", errors="replace").strip()
            if not line.startswith("gitdir:"):
                return "unknown"
            gitdir = Path(line.split(":", 1)[1].strip()).expanduser()
            if not gitdir.is_absolute():
                gitdir = root / gitdir
        else:
            gitdir = dot
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: refs/heads/"):
            return head[len("ref: refs/heads/"):]
        return head          # detached: the raw object name
    except OSError:
        return "unknown"


# Quoted spans are stripped before matching, so `grep "git commit" file` is not
# read as a commit. Without this the wall blocks ordinary searches, and a wall
# that blocks ordinary work is a wall somebody routes around.
# Escape-aware. A quote character preceded by a backslash is a literal, not the
# start of a quoted span, and reading it as one let a real push hide between two
# of them: MEASURED 2026-08-31, `echo can\'t; git push; echo won\'t` is valid
# shell, the two escaped apostrophes paired as a span, and `release_action`
# returned None for a command that pushes. Contrived, and still a hole in a wall.
#
# The lookbehind is the conservative direction for a WALL: it blanks LESS, so a
# missed span costs a false refusal (visible, annoying) rather than a missed push
# (invisible, and the whole point). `\\'` (an escaped backslash then a real
# quote) is still read as a literal, which errs the same safe way.
_QUOTED_RE = re.compile(r"(?<!\\)'[^']*'|(?<!\\)\"(?:\\.|[^\"\\])*\"")

# The push-capable scripts of this workspace, by BASENAME.
#
# This list is not hand-curated and must not be extended by guesswork.
# A derivation stood behind it: every `scripts/**.py` with a `__main__` guard
# that reaches `supervised_push()` or builds a literal `git`/`push` argv, found
# by AST and followed transitively through imports and subprocess fan-out, and
# any derived entry point missing from here was a failure. That derivation was
# parked in the 2026-09-21 test-suite split, so a new push script no longer
# reddens anything and this list can silently fall behind the tree. Re-derive
# it by hand before trusting it, and never extend it by guesswork instead.
#
# MEASURED 2026-08-30, before the derivation existed, calling `release_action`
# directly: `python scripts/safe-push.py --repo engine` returned None. That
# script IS the workspace's deterministic supervised push. Four more real push
# paths were open the same way. The list had been written by hand and had fallen
# behind the code, which is the failure mode this whole comment exists to close.
#
# The unit of this wall is the SCRIPT, never the flag. Carving out a read-only
# mode would put flag parsing inside a release wall, and a flag can be
# reordered, aliased, or defaulted differently by a later edit. Do not "fix" a
# refused read-only invocation by adding a flag exception.
#
# Matched by basename, so `scripts/dev/publish-marketplace.py` is covered. A
# name no file carries guards nothing: keep the set to scripts that exist.
_PUSH_SCRIPTS = frozenset({
    "push-all.py",
    "safe-push.py",
    "publish-service.py",
    "publish-marketplace.py",
    "create-data-repo.py",
})

# Programs that stand IN FRONT of the program actually being run. Skipping them
# is what lets the wall ask "what is this segment RUNNING" instead of "does this
# string contain a name somewhere".
_WRAPPERS = frozenset({
    "env", "sudo", "doas", "nohup", "command", "exec", "time", "nice",
    "stdbuf", "timeout", "xargs", "uv", "uvx", "poetry", "pipenv", "pdm",
    "hatch", "run", "npx",
    "python", "python2", "python3", "py", "pypy", "pypy3",
    "bash", "sh", "zsh", "dash", "ksh", "fish", "perl", "ruby", "node",
})

# A heredoc body is a command line only when a SHELL is what reads it.
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})

# Authorising words. A PUSH word also authorises the commit that carries it:
# asking for the push is asking for the work to leave the machine, and refusing
# the commit underneath it would only teach the operator to type two words.
_PUSH_WORDS = (
    "push", "пуш", "запуш", "отправь", "backup", "бэкап", "бекап",
    "publish", "опубликуй", "выложи", "залей",
)
_COMMIT_WORDS = (
    "commit", "коммит", "закоммить", "зафиксируй",
    # A MERGE word authorises the commit it needs, and only that. MEASURED
    # 2026-09-06: the operator typed `merge`, then `merge` again, and both times
    # this wall refused, because the one verb `CLAUDE.md` names as HELM's own
    # duty, beside the commit and the push, was absent from its vocabulary.
    # A wall whose word list cannot express the cycle it guards teaches the
    # operator to type a different word than the one they mean, which is the
    # same defect the per-action split above was written to remove.
    #
    # In `_COMMIT_WORDS` and deliberately NOT in `_PUSH_WORDS`: a merge is
    # local, and nothing about asking for one asks for the result to leave the
    # machine.
    #
    # Both spellings of the Russian root, because the match is a substring and
    # `ё` is a different character: "мерж" catches "смержить", "мёрж" catches
    # "смёржить", and the operator writes both. Blunt in the same direction as
    # the rest of this list: "fix the merge conflict" now authorises a commit,
    # exactly as "check the commit" already did.
    "merge", "мерж", "мёрж",
    # The Russian half of the same word, and the next turn of the same screw.
    # MEASURED 2026-09-06: the operator typed `сливай фикс w5M, удаляй ярд` and
    # this wall refused the commit. `слить` is what a Russian speaker says for
    # "merge a branch" at least as often as `смержить`, and the list above held
    # only the borrowed spelling, so the wall heard one of the two words the
    # operator actually uses for one action.
    #
    # WHY THIS IS STILL AN ENUMERATION, AND WHY THAT IS ACCEPTABLE HERE. The
    # daemon rule's list of verbs was a defect because a verb missing from it
    # was a REFUSAL missing: the hole was silent and permissive. This list fails
    # the other way round. A word absent from it costs a false refusal, which
    # the operator sees immediately and can answer by typing another word, and
    # nothing that should have been stopped gets through. So an incomplete list
    # here is a nuisance with a visible symptom rather than an invisible hole,
    # which is the whole reason it is tolerable to write one down.
    #
    # What is enumerated is also narrower than it looks: these are the
    # INFLECTIONS of one root, and a Russian root has a closed set of them,
    # unlike the open set of verbs a rule about daemons has to cover. The open
    # part is the list of ROOTS, and that stays open; the answer to a new one is
    # to add it, the same way `merge` was added.
    #
    # `слив` catches `сливай` and `сливаю`; `слить` and `слей` are the other two
    # imperatives; `сольё`/`солью` are the perfective future (`давай сольём`).
    # Deliberately NOT a bare `сли`, which is inside `слишком`.
    "слить", "слив", "слей", "сольё", "солью",
)

# PER ACTION, since 2026-09-03. These were one list, and any entry refused the
# WHOLE prompt, so `"закоммить всё и не пушь"` refused the COMMIT as well. The
# wall's vocabulary could not express "commit, but do not push", which is the
# most common instruction in this workspace; the operator had to authorise
# commits in a separate message that omitted the prohibition, which is a wall
# teaching people to drop their own safety words.
#
# Deliberately blunt, and blunt in the REFUSING direction: "не пушь пока" and
# "push" differ by one token, and a wall that has to parse intent gets it wrong
# in the expensive direction. A false refusal is visible and cheap; a missed
# push is invisible and is the whole point.
_PUSH_NEGATIONS = (
    "не пуш", "не отправляй", "не заливай", "не публикуй", "без пуша",
    "don't push", "dont push", "do not push", "no push",
)
_COMMIT_NEGATIONS = (
    "не коммить", "не комить", "без коммита",
    "don't commit", "dont commit", "do not commit", "no commit",
    # The refusing half of the merge words above. A permission word that has no
    # negation beside it cannot be withdrawn in the same sentence it is granted,
    # and the operator writes "merge but don't commit anything else" constantly.
    "не мерж", "не мёрж", "без мержа", "без мёржа",
    "don't merge", "dont merge", "do not merge", "no merge",
    # One negation per permission added above, written in the same change. A
    # permission word shipped without its refusing pair means `не сливай` reads
    # as `сливай` and authorises exactly what the sentence forbids.
    "не слить", "не слив", "не слей", "не сольё", "не солью", "без слива",
)

# A prohibition said in words the lists above do not enumerate. MEASURED
# 2026-09-03 against the previous version: `"пуш запрещён, только коммить"`
# returned True for a PUSH, and so did `"push is forbidden here"` -- writing the
# prohibition was indistinguishable from writing the permission, because a push
# WORD appearing anywhere returned True for every action.
#
# These refuse a PUSH only. They deliberately do not touch a commit: the operator
# writes "пуш запрещён" beside a commit instruction constantly, and reading that
# as a refusal of both is the defect above facing the other way.
_PROHIBITIONS = (
    "запрещ", "запрет", "нельзя", "forbidden", "prohibited", "not allowed",
)

# The line HELM prepends to every brief it delivers into a YARD, and the only
# thing in this wall that can tell a machine's words from the operator's.
#
# Everything above rested on a premise that does not hold: that a `type: "user"`
# record with `promptSource: "typed"` is a human at a keyboard. A prompt injected
# into a session by ANOTHER Claude session via `herdr agent prompt` lands as
# exactly that record. MEASURED 2026-09-05 on one transcript holding both kinds,
# a brief delivered from HELM at line 11 and three prompts the operator typed
# into that same yard by hand at lines 839, 1157 and 1285: every field identical
# except `parentUuid`, `promptId`, `uuid` and `timestamp`. `promptSource` is
# "typed" for all four and `sessionKind` is "bg" for all four. No field in the
# record says who produced the text, so no discriminator exists inside it.
#
# The discriminator therefore has to be IN the text, put there by the sender.
# `scripts/herdr-brief.py` prepends this line and is the only sanctioned way to
# send a brief. Matched as a SUBSTRING, because a brief that is quoted, indented
# or re-wrapped by whatever carried it must still be inert.
#
# WHAT THIS ESTABLISHES, AND WHAT IT DOES NOT. It establishes that a prompt
# CARRYING the marker does not authorise a release. It does NOT establish that a
# prompt WITHOUT the marker was typed by a human; nothing in the record can
# establish that. This is fail-OPEN by construction -- an unmarked brief still
# authorises -- and the only thing standing behind that is the sender plus its
# test.
#
# Two designs that look like they would close it, both rejected on measurement,
# recorded here so they are not rebuilt. A local challenge-response ("2FA") does
# not work: the verifier and the adversary run as the SAME UID on the SAME
# machine, so any secret this hook can read to check an answer, a peer session
# can read to produce one, and reading from the tty does not help because
# `herdr pane run` types into a pane. A LENGTH CEILING on the authorising prompt
# was refuted on the operator's own data: MEASURED 2026-09-06 over 97 of his
# typed prompts containing a release word, across six HELM transcripts, min 3,
# median 26, p90 248, max 5272, and 12 of the 97 longer than 200 characters. The
# two populations do not separate by length.
_BRIEF_MARKER = "X-HEADING-BRIEF: machine-to-machine, not an operator authorisation"


def _strip_quoted(cmd: str) -> str:
    """The command with quoted spans blanked, for matching only."""
    return _QUOTED_RE.sub(" ", cmd or "")


def _loads_or_none(raw: bytes) -> dict | None:
    """One transcript line as a dict, or None.

    A partly-written or truncated line is ORDINARY here: the transcript is
    appended to by the harness while this hook reads it, so the last line is
    routinely half a record. It is skipped, not logged, because this runs on
    every release command and a warning per torn line would be noise the
    operator learns to scroll past. The consequence of skipping is covered: if
    no `last-prompt` is found at all, the caller REFUSES.
    """
    try:
        rec = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return rec if isinstance(rec, dict) else None


# Commands that only LOOK at bytes. A segment led by one of these cannot
# release anything, whatever the rest of it spells.
_INSPECTORS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk", "cat", "head",
    "tail", "less", "more", "wc", "ls", "find", "file", "stat", "diff", "cmp",
    "cut", "sort", "uniq", "tr", "nl", "basename", "dirname", "realpath",
    "readlink", "echo", "printf", "md5sum", "sha256sum", "test", "which",
})

# A NEWLINE separates commands exactly as `;` does, and it was missing here
# until 2026-08-31. MEASURED with the old splitter: the two-line command
# `cd /repo` then `git push origin main` returned None, because the anchors were
# `^` without re.MULTILINE and no operator preceded the push. A multi-line Bash
# call is an ordinary shape, so that was the wall failing open on an ordinary
# push. `)` is here so `(cd x && git push)` yields a clean final word.
_SEGMENT_SPLIT_RE = re.compile(r"(?:\|\||&&|[;&|\n\r()]|\$\()")

# `<<EOF`, `<< EOF`, `<<'EOF'`, `<<\"EOF\"`, `<<-EOF`.
#
# The `_RELEASE_` prefix keeps this from rebinding `_HEREDOC_START_RE` further
# up, whose groups are NAMED: one shared name broke every heredoc walk with
# `IndexError: no such group` (MEASURED 2026-08-31). Same here-string guard as
# `_HEREDOC_START_RE`: `_heredoc_body_spans` blanks from the newline after the
# match to the terminator, or to the end when there is none, so without it a
# `<<<word` blanked the remainder of the command.
_RELEASE_HEREDOC_RE = re.compile(
    r"(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# Shell punctuation clinging to a word. `git push)` must compare equal to
# `push`, or the wall fails open on a subshell.
_WORD_TRIM = "()[]{};`\"'"

# Flags whose remaining words are a command line, not arguments.
_NESTED_CMD_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _heredoc_body_spans(cmd: str) -> list[tuple[int, int]]:
    """(start, end) of every heredoc BODY in `cmd`."""
    spans = []
    for m in _RELEASE_HEREDOC_RE.finditer(cmd):
        nl = cmd.find("\n", m.end())
        if nl == -1:
            continue
        start = nl + 1
        term = re.compile(rf"^[ \t]*{re.escape(m.group(2))}[ \t]*$", re.MULTILINE)
        t = term.search(cmd, start)
        spans.append((start, t.start() if t else len(cmd)))
    return spans


def _blank_spans(cmd: str, spans: list[tuple[int, int]]) -> str:
    """`cmd` with those spans replaced by spaces, newlines preserved."""
    buf = list(cmd)
    for start, end in spans:
        for i in range(start, min(end, len(buf))):
            if buf[i] not in "\r\n":
                buf[i] = " "
    return "".join(buf)


def _program_candidates(seg: str) -> list[str]:
    """The basenames that could be the program this segment RUNS.

    Walks from the left, skipping flags, `VAR=value` prefixes and bare numbers
    (`timeout 30 ...`, `nice 10 ...`), and keeps walking through wrappers, so
    `uv run python scripts/push-all.py` yields `[uv, run, python, push-all.py]`
    and `cp a.md .claude/skills/push-updates/b.md` yields just `[cp]`.

    A later ARGUMENT never appears here, and that is the whole point. MEASURED
    2026-08-30, when the script names were matched anywhere in a segment:
    `.venv/bin/python scripts/ste-check.py .claude/skills/push-updates/SKILL.md`
    and `cp a.md .claude/skills/push-updates/b.md` were both refused as pushes.
    Neither releases anything, and the first cost a real lint run that day. That
    is how a wall teaches agents a detour, after which it guards nothing.
    """
    out: list[str] = []
    for raw in seg.split():
        word = raw.strip(_WORD_TRIM)
        if not word or word.startswith("-"):
            continue
        if "=" in word:
            # `env GIT_DIR=/r/.git git push`. The scan only ever reaches here
            # while everything seen so far is a wrapper (it breaks on the first
            # program), so an `=` token at this point is an assignment prefix,
            # not a program. Restricting the skip to the FIRST word let
            # `env VAR=v git push` resolve to `.git` and return None.
            continue
        if word.replace(".", "", 1).isdigit():
            continue
        base = word.rsplit("/", 1)[-1]
        out.append(base)
        if "." in base and not base.endswith(".py"):
            # `python -m scripts.<name>` runs `<name>.py`, and a basename match
            # never saw it (MEASURED 2026-08-31: None, for a push path of that
            # day). No push-capable entry point has a hyphen-free name today;
            # this keeps a future one inside the wall.
            out.append(base.rsplit(".", 1)[-1] + ".py")
        if base not in _WRAPPERS:
            break
    return out


def _strip_inert_heredocs(cmd: str) -> str:
    """Blank heredoc BODIES, unless a shell is what reads them.

    A heredoc feeds data to the program in front of it. For `python3 - <<EOF`,
    `tee f.py <<EOF` or `cat > f <<EOF` that data is a file being written, and
    matching release patterns inside it is pure over-friction: MEASURED
    2026-08-31, an agent writing a test file for THIS wall was refused twice
    because the strings "git push" and "git commit" appeared in its cases, and
    it routed around the wall with the Edit tool and a scratch file.

    For a shell the body IS a command line, so `bash <<'EOF' ... git push ...
    EOF` must stay caught -- and MEASURED at the same time, it was NOT: the old
    splitter never split on a newline, so that real push returned None. When any
    segment outside the bodies runs a shell, the bodies are returned untouched
    and the ordinary machinery reads them. Over-approximating "a shell runs
    here" is the safe direction and is chosen deliberately.

    Bound, stated rather than implied: this wall reads SHELL command lines. Code
    inside an interpreter payload that pushes (`subprocess.run(["git","push"])`
    in a `python3 -` heredoc) is not covered here and never was -- quoting
    already blanked it before this function existed. The gate for that is the
    same one that covers every script: the program being run.
    """
    spans = _heredoc_body_spans(cmd)
    if not spans:
        return cmd
    outside = _blank_spans(cmd, spans)
    for seg in _SEGMENT_SPLIT_RE.split(outside):
        if any(c in _SHELLS for c in _program_candidates(seg)):
            return cmd
    return outside


def _executable_segments(bare: str) -> list[str]:
    """The parts of a command line that could actually run something.

    A segment whose FIRST word is a pure inspector is dropped. Since the script
    names became program-position matches, that drop is belt-and-braces rather
    than load-bearing: `cat scripts/push-all.py` resolves to `cat` and would be
    silent anyway. It is kept because it costs nothing and says the intent out
    loud.

    The `-exec` extraction below is NOT belt-and-braces. It is the one place
    where a dropped inspector was hiding a real command.

    Splitting first means a compound line is still caught: `ls && push-all.py`
    keeps its second segment.
    """
    out = []
    for seg in _SEGMENT_SPLIT_RE.split(bare):
        words_raw = seg.split()
        # `find . -name x -exec git push \;` runs a real push, and `find` is an
        # inspector, so the whole segment used to be dropped. Whatever the outer
        # program is, the words after `-exec` are a command line of their own
        # and get judged as one. Extracted BEFORE the inspector drop, or the
        # nested command goes down with its host.
        for i, word in enumerate(words_raw):
            if word in _NESTED_CMD_FLAGS:
                out.append(" ".join(words_raw[i + 1:]))
                break
        # Strip before returning. A segment carved out of `a && git push` keeps
        # its leading space, and the first version of this function returned it
        # unstripped, so `^` matched nothing and a real push read as None.
        # Program resolution no longer depends on the anchor, but the strip
        # stays: it is what makes the segments readable in a failing test.
        seg = seg.strip()
        words = seg.split()
        if words and words[0].rsplit("/", 1)[-1] in _INSPECTORS:
            continue
        out.append(seg)
    return out


def _segment_words(seg: str) -> list[str]:
    return [w.strip(_WORD_TRIM) for w in seg.split()]


# git's OWN options, the ones that may stand between `git` and the subcommand.
# A closed, documented set, which is what makes resolving the subcommand
# position possible at all. Split by whether the value is a separate word.
_GIT_GLOBAL_VALUE_OPTS = frozenset({
    "-c", "-C", "--exec-path", "--git-dir", "--work-tree", "--namespace",
    "--super-prefix", "--config-env", "--attr-source",
})
_GIT_GLOBAL_FLAGS = frozenset({
    "-p", "--paginate", "-P", "--no-pager", "--bare", "--no-replace-objects",
    "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs",
    "--icase-pathspecs", "--no-optional-locks", "--no-lazy-fetch",
    "--no-advice", "--html-path", "--man-path", "--info-path", "--version",
    "-v", "--help", "-h",
})

# Subcommands that ARE a push, whatever follows them.
_GIT_PUSHING_SUBCOMMANDS = frozenset({"push", "send-pack"})

# Subcommands that carry a second command line of their own, or drive a
# separate transport: `git subtree push`, `git lfs push`, `git submodule
# foreach git push`, `git svn`, `git p4`, `git annex`. Inside one of these the
# word-anywhere test of the previous version is kept, because the word is no
# longer in the subcommand slot and nothing here can resolve what it belongs
# to. Over-approximating in the refusing direction, deliberately: `git remote`
# reaches no remote by itself and is in the set anyway, so that today's refusal
# of `git remote add push <url>` is not quietly dropped.
_GIT_NESTING_SUBCOMMANDS = frozenset({
    "subtree", "submodule", "lfs", "svn", "p4", "annex", "remote",
})


def _git_subcommand(words: list[str]) -> str | None:
    """The git SUBCOMMAND in this segment, or None when it cannot be resolved.

    `git stash push -m x` runs `stash`; the `push` is an argument to it, and a
    stash reaches no remote and is reversible. MEASURED 2026-09-21 in HELM
    against the previous version: `git stash push --staged -m "..."` was refused
    with "RELEASE GATE: the operator did not ask for a push in this turn",
    because the test was whether the word `push` appeared ANYWHERE beside `git`.

    None means "undeterminable", and every caller treats it as the old
    word-anywhere test rather than as a permission: an option shape this does
    not know is the one case where the position of the subcommand is a guess,
    and a wall guesses in the refusing direction. That is why an unrecognised
    flag returns None here instead of being skipped.

    The returned token is NOT trusted to be a real git subcommand. An alias
    resolves to whatever the operator configured, and nothing here can read
    `.gitconfig`. It is used only to ask whether the word `push` or `commit`
    occupies the subcommand slot, never to conclude that some other word is
    harmless on its own.
    """
    # The index of the `git` word, or None when this segment has none. Written
    # as a search rather than as a `for ... else` because the loop variable is
    # read AFTER the loop and not inside it, which ruff reports as B007 and
    # whose own remedy, renaming it to `_i`, would break the next line. The
    # absent case must stay a `return None`: that is what makes a segment with
    # no `git` word undeterminable rather than permitted.
    found = next((n for n, w in enumerate(words)
                  if w.rsplit("/", 1)[-1] == "git"), None)
    if found is None:
        return None
    i = found + 1
    while i < len(words):
        word = words[i]
        if not word.startswith("-"):
            return word
        name = word.split("=", 1)[0]
        if "=" in word:
            if name in _GIT_GLOBAL_VALUE_OPTS or name in _GIT_GLOBAL_FLAGS:
                i += 1
                continue
            return None
        if name in _GIT_GLOBAL_VALUE_OPTS:
            i += 2
            continue
        if name in _GIT_GLOBAL_FLAGS:
            i += 1
            continue
        return None
    return None


def _segment_pushes(seg: str) -> bool:
    """Whether this one segment releases work to a remote."""
    candidates = _program_candidates(seg)
    if any(c in _PUSH_SCRIPTS for c in candidates):
        return True
    words = _segment_words(seg)
    if "git" in candidates:
        # Deliberately option-blind about the VALUE of git's global options.
        # The old regex enumerated the global-option shapes it knew (`-x`,
        # `-c k=v`) and MEASURED 2026-08-31 it missed `git -C ../x push`, which
        # is how this workspace pushes the second repository. What is read here
        # is the POSITION of the subcommand, not a list of excused words: a word
        # is excused only by standing in a slot where it is an argument, and
        # `push` in the subcommand slot is a push whatever precedes it.
        sub = _git_subcommand(words)
        if sub is None or sub in _GIT_NESTING_SUBCOMMANDS:
            return "push" in words
        if sub in _GIT_PUSHING_SUBCOMMANDS:
            return True
    if "gh" in candidates:
        # An index walk, not `zip(words, words[1:])`: a strict zip over a
        # sliced pair can never raise, so the lint that asks for one would be
        # satisfied by a check that measures nothing.
        for i in range(len(words) - 1):
            if (words[i], words[i + 1]) in (("release", "create"), ("pr", "merge")):
                return True
    return False


# `git tag` options that only READ. The same defect as the one above, one slot
# further in: the previous version asked whether ANY flag followed the word
# `tag`, so `git tag -l` and `git tag --sort=-version:refname`, which list, were
# refused as commits. Anything not on this list writes, including `-d`, so an
# option nobody listed refuses.
_GIT_TAG_READ_ONLY_FLAGS = frozenset({
    "-l", "--list", "-n", "--contains", "--no-contains", "--points-at",
    "--merged", "--no-merged", "--sort", "--format", "--color", "-i",
    "--ignore-case", "--column", "--no-column", "--omit-empty", "-v",
    "--verify",
})


def _git_tag_writes(rest: list[str]) -> bool:
    """Whether `git tag <rest>` writes a tag object rather than listing them."""
    flags = [w for w in rest if w.startswith("-")]
    if not flags:
        # A bare `git tag` lists. `git tag v1` creates a lightweight tag, and
        # the previous version let that one through, because it asked for a
        # flag and there is none.
        return any(not w.startswith("-") for w in rest)
    for flag in flags:
        name = flag.split("=", 1)[0]
        if name in _GIT_TAG_READ_ONLY_FLAGS:
            continue
        if re.fullmatch(r"-n\d*", name):  # `git tag -n5`
            continue
        return True
    return False


def _segment_commits(seg: str) -> bool:
    if "git" not in _program_candidates(seg):
        return False
    words = _segment_words(seg)

    def _tag_writes_after(index: int) -> bool:
        return _git_tag_writes(words[index + 1:])

    def _word_anywhere() -> bool:
        if "commit" in words:
            return True
        if "tag" in words:
            return _tag_writes_after(words.index("tag"))
        return False

    # The same subcommand read as `_segment_pushes`, and for the same reason:
    # `git help commit` and `git log --grep commit` were refused as commits by
    # the word-anywhere test, and neither writes an object. Undeterminable or
    # nesting subcommands keep that test.
    sub = _git_subcommand(words)
    if sub is None or sub in _GIT_NESTING_SUBCOMMANDS:
        return _word_anywhere()
    if sub == "commit":
        return True
    if sub == "tag":
        return _tag_writes_after(words.index("tag"))
    return False


def release_action(command: str) -> str | None:
    """`"push"`, `"commit"`, or None. Pure, so both directions are measurable.

    Coverage this establishes, and no more: SHELL command lines. It reads what
    each segment RUNS -- git, gh, or a push-capable script -- through wrappers,
    interpreters, subshells, newlines, shell heredocs and `find -exec`.

    It does NOT read code inside an interpreter payload. `python3 -c 'from
    scripts.utils.git_push import supervised_push; supervised_push(".")'`
    returns None, and always has: quoting blanks the payload before anything
    looks at it. That is a deliberate boundary, not an oversight -- parsing
    Python out of a shell string would be a second language to get wrong -- but
    it is the shape to remember when reasoning about what this wall promises.
    """
    bare = _strip_quoted(_strip_inert_heredocs(command or ""))
    segments = _executable_segments(bare)
    for seg in segments:
        if _segment_pushes(seg):
            return "push"
    for seg in segments:
        if _segment_commits(seg):
            return "commit"
    return None


# An outbound message as the operator, through the clients that can send one:
# Mattermost (since 2026-10-12), Telegram and e-mail (engine audit F-2.A2 and
# A-4.14, 2026-10-16). Each is the script name followed, past any global flags,
# by a subcommand that reaches another person. `send-email.py` has no
# subcommand: every run sends, except `--dry-run` and `--help`.
#
# Read per command segment, with a quoted span kept only when it names one of
# these clients (the skills quote the script path) and blanked otherwise, so a
# message body cannot supply `--dry-run` or a second subcommand. A segment led by
# an inspector (`grep`, `cat`, ...) sends nothing. Same boundary as
# `release_action`: code inside an interpreter payload, or a hand-made request
# to a server, is not read here.
_SEND_CLIENT_RES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("Mattermost", re.compile(
        r"mattermost_client\.py(?:\s+--?[\w-]+)*\s+(?:send|reply)\b")),
    ("Telegram", re.compile(
        r"telegram_client\.py(?:\s+--?[\w-]+)*\s+(?:send|send-file|reply|forward|delete)\b")),
    ("Gmail", re.compile(r"gmail-send\.py(?:\s+--?[\w-]+)*\s+send\b")),
)
_SEND_EMAIL_INERT_RE = re.compile(r"(?:^|\s)(?:--dry-run|-h|--help)(?=\s|$)")
_SEND_CLIENT_NAMES = ("mattermost_client.py", "telegram_client.py",
                      "gmail-send.py", "send-email.py")
# A client counts only where it is the program a segment RUNS: the first word,
# or the script an interpreter (`python3 -u`, `uv run`, `exec`) is handed. A
# client path given as an argument (`git add`, `pre-commit run --files`,
# `stage.py --in-file`, `py_compile`) sends nothing, and until 2026-10-16 such a
# segment was refused as a send. An interpreter option that takes a value
# (`python3 -X dev x.py`) hides the script, the same boundary as code inside an
# interpreter payload.
_INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|pythonw|exec|nohup)$")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")


def _keep_client_quotes(match: "re.Match[str]") -> str:
    span = match.group(0)
    return f" {span[1:-1]} " if any(n in span for n in _SEND_CLIENT_NAMES) else " "


def _runs_client(words: list[str], i: int) -> bool:
    """True when `words[i]`, a client path, is the program the segment runs."""
    j = i - 1
    while j >= 0 and words[j].startswith("-"):
        j -= 1
    if j < 0 or all(_ENV_ASSIGN_RE.match(w) for w in words[: j + 1]):
        return True
    prev = Path(words[j]).name
    if _INTERPRETER_RE.match(prev):
        return True
    return prev == "run" and j > 0 and Path(words[j - 1]).name == "uv"


def outbound_send_target(command: str) -> str | None:
    """The channel a command sends a message through, or None. Pure."""
    bare = _QUOTED_RE.sub(_keep_client_quotes,
                          _strip_inert_heredocs(command or ""))
    for seg in _SEGMENT_SPLIT_RE.split(bare):
        words = seg.split()
        if not words or Path(words[0]).name in _INSPECTORS:
            continue
        for i, word in enumerate(words):
            name = Path(word).name
            if name not in _SEND_CLIENT_NAMES or not _runs_client(words, i):
                continue
            rest = " ".join([name, *words[i + 1:]])
            for channel, pattern in _SEND_CLIENT_RES:
                if pattern.match(rest):
                    return channel
            if name == "send-email.py" and not _SEND_EMAIL_INERT_RE.search(rest):
                return "e-mail"
    return None


def mattermost_send_action(command: str) -> str | None:
    """`"send"` when the command sends an outbound message, else None. Pure.

    Named for the first client it covered; it now covers every client in
    `_SEND_CLIENT_RES` and `send-email.py`.
    """
    return "send" if outbound_send_target(command) else None


# A send needs an IMPERATIVE ask, never a stem found anywhere. Until 2026-10-12
# this was a substring list ("send", "отправ", ...), and MEASURED that day in
# HELM `prompt_authorises("что отправили?", "send")` returned True: a question
# about messages already sent authorised a new Mattermost post. "sending" and
# "отправлено" matched the same way.
#
# Russian imperatives are distinct word forms, so a whole word is enough. The
# English verb is not ("I'll send it", "did you send it"), so it counts only at
# the head of a clause, optionally after a softener, and only with an object or
# with nothing after it. A sentence ending in "?" authorises nothing. Blunt in
# the refusing direction, like every list in this wall: "отправь?" and "can you
# send it" refuse, which is a visible false refusal the operator answers by
# typing the order.
_SEND_IMPERATIVES = re.compile(
    r"\b(?:отправь|отправьте|отправляй|отправляйте|отошли|отошлите"
    r"|посылай|посылайте|запости|запостите|запость)\b"
    r"|(?:^|[,;:]|\b(?:please|pls|ok|okay|now|then|and|just|go ahead and|yes|да|ок))"
    r"\s*(?:send|post)(?:\s+(?:it|this|that|them|these|those|the|a|an|my|our"
    r"|now|to|back|here|there|in|out|over)\b|\s*[.!]*$)"
)
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")
_SEND_NEGATIONS = (
    "не отправ", "не посылай", "не отсылай", "без отправки", "не надо", "не нужно",
    "don't send", "dont send", "do not send", "no send", "not send",
)


def prompt_authorises(prompt: str, action: str) -> bool:
    """Whether the operator's own most recent words authorise this action.

    Pure. `prompt` is the verbatim `lastPrompt`; `action` is from
    `release_action`. A negation anywhere refuses.

    A prompt carrying `_BRIEF_MARKER` refuses before any word is looked at: it
    is a brief from another Claude session, and no wording of one authorises a
    release. That check establishes only its own direction. A prompt WITHOUT the
    marker is not thereby established as the operator's, because no field in the
    transcript record distinguishes the two; read `_BRIEF_MARKER` for the
    measurement and for the two designs that were rejected.
    """
    if not prompt or not action:
        return False
    if _BRIEF_MARKER in prompt:
        return False
    low = prompt.lower()

    push_refused = (any(w in low for w in _PUSH_NEGATIONS)
                    or any(w in low for w in _PROHIBITIONS))
    commit_refused = any(w in low for w in _COMMIT_NEGATIONS)

    if action == "send":
        if any(w in low for w in _SEND_NEGATIONS) or any(w in low for w in _PROHIBITIONS):
            return False
        return any(_SEND_IMPERATIVES.search(s.strip())
                   for s in _SENTENCE_RE.findall(low)
                   if not s.rstrip().endswith("?"))

    if action == "push":
        return not push_refused and any(w in low for w in _PUSH_WORDS)

    if action == "commit":
        if commit_refused:
            return False
        if any(w in low for w in _COMMIT_WORDS):
            return True
        # A push the operator asked for implies the commit that precedes it --
        # but only while the push is not itself refused.
        return not push_refused and any(w in low for w in _PUSH_WORDS)

    return False


def _flatten(text: str) -> str:
    """Whitespace collapsed, for comparing two spellings of one prompt."""
    return " ".join((text or "").split())


#: Why the operator's words could not be produced. The caller turns these into
#: DIFFERENT refusals, and that is the whole reason they exist as separate
#: values rather than as one None.
#:
#: MEASURED 2026-09-06: with one message for all of them, establishing which of
#: the two records was missing meant opening the reader, then the raw JSONL of a
#: live transcript, by hand. The two states have nothing to do with each other.
#: `_NO_TRANSCRIPT` and `_NO_RECORDS` are a broken or absent file, which is a
#: machine problem. `_UNCONFIRMED` and `_MISMATCH` are the harness having written
#: one of the turn's two prompt records and not yet the other, which is a
#: TIMING problem the operator resolves by asking again in a fresh turn.
_PROMPT_OK = "ok"
_PROMPT_NO_TRANSCRIPT = "no-transcript"
_PROMPT_NO_RECORDS = "no-records"
_PROMPT_UNCONFIRMED = "unconfirmed"
_PROMPT_MISMATCH = "mismatch"
#: A `last-prompt` record with no typed record to un-truncate it, AND the text
#: is sitting on the harness's 200-character cap. Added 2026-09-21. It is not a
#: timing state and not a broken file: it is the one input shape in which the
#: words the operator wrote LAST are provably not in front of this wall, which
#: is where a prohibition goes.
_PROMPT_TRUNCATED = "truncated"
#: The newest capped record is a slash-command line written AFTER the newest
#: typed record and disagreeing with it. Added 2026-09-25 under HOS-128. A slash
#: command leaves a string user record with no `promptSource` and a capped record
#: carrying the command line, and never a typed record, so the typed record in
#: front of this reader belongs to the turn BEFORE the command. That is a turn
#: boundary, not a disagreement between two records of one prompt: the typed
#: reader has nothing to say for this turn, and the mid-turn reader is asked
#: instead. MEASURED 2026-09-25 over the 25 largest transcripts on this machine:
#: 4064 of 19094 capped records are slash-command lines, 3601 of them `/compact`,
#: and 11762 of 60838 tool_use records sat behind one that disagreed with the
#: newest typed record, every one of them refused as `_PROMPT_MISMATCH`.
_PROMPT_COMMAND = "command"

#: The first word of a slash command. A path such as `/home/...` is not one: the
#: second slash stops the match before the end of the word.
_SLASH_COMMAND = re.compile(r"/[A-Za-z][\w:-]*")


def _is_slash_command(capped: str) -> bool:
    words = (capped or "").split()
    return bool(words) and _SLASH_COMMAND.fullmatch(words[0]) is not None


def _is_truncated(capped: str) -> bool:
    """Whether this `lastPrompt` is provably not the whole of what was typed.

    The harness cuts `lastPrompt` at 200 characters and appends one ellipsis.
    MEASURED 2026-09-03 over one session's 166 `last-prompt` records: maximum
    length exactly 201, and 83 of them on that cap.

    The ellipsis it appends when it cuts, and nothing else. LENGTH is
    deliberately not evidence here, in either direction: a record longer than
    the cap did not come out of the cutting path at all, so it is whole, and
    testing `len(...) > 200` would have read the one shape that cannot be
    truncated as the one shape that is. A prompt of exactly 200 characters
    arrives without the ellipsis and is whole.

    The cost of reading the ellipsis is a prompt that genuinely ends in one,
    under 200 characters, refusing a release. That is a false refusal, which is
    visible and cheap, and it is the direction this wall errs in everywhere
    else.
    """
    return (capped or "").rstrip().endswith("…")


def _reconcile_detail(typed: str | None,
                      capped: str | None) -> tuple[str | None, str]:
    """The operator's words, full length, and why not when there are none.

    `capped` is the harness-written `last-prompt`, which the model cannot forge
    and which is therefore the provenance guarantee this wall rests on. `typed`
    is the same turn's `type: "user"` record with a `promptSource` in
    `_OPERATOR_PROMPT_SOURCES`, whose
    `message.content` is the SAME TEXT UNTRUNCATED.

    Both, not either: reading `message.content` alone would give up the
    provenance, so the capped record has to confirm it. A prefix, because that
    is exactly the relationship between them.
    """
    if typed is None:
        # A session recorded before `promptSource` existed. The capped text is
        # all there is, and it is CUT AT 200 CHARACTERS PLUS AN ELLIPSIS, so
        # anything the operator wrote past that is invisible here.
        #
        # WHICH IS WHY A CAPPED ONE NO LONGER AUTHORISES, since 2026-09-21.
        # Returning it kept exactly the hole this wall's own 2026-09-03 fix was
        # written to close: a 303-character prompt that asks for a push and
        # forbids it at the end authorises the push through its 201-character
        # copy. The fix that day reached the case where a typed record EXISTS;
        # this branch is the same defect with nothing to un-truncate it, and it
        # is reachable. MEASURED 2026-09-21 over the 25 largest transcripts on
        # this machine, replaying this reader at all 42,095 tool calls whose
        # turn could be resolved: 5,347 of them would have been answered from a
        # capped record with no typed record beside it, 1,811 of those from a
        # capped record that is not a prefix of the prompt of the turn the tool
        # call belongs to. A short capped record is whole, so it still answers.
        if capped is None:
            return None, _PROMPT_NO_RECORDS
        if _is_truncated(capped):
            return None, _PROMPT_TRUNCATED
        return capped, _PROMPT_OK
    if capped is None:
        return None, _PROMPT_UNCONFIRMED  # unconfirmed origin: refuse
    head = _flatten(capped).rstrip("…").rstrip()
    if head and _flatten(typed).startswith(head):
        return typed, _PROMPT_OK
    return None, _PROMPT_MISMATCH


#: The `promptSource` values whose user record carries the operator's own
#: keystrokes. "queued" is a prompt he typed while the model was still working,
#: delivered at the next turn boundary. It was absent until 2026-10-12, and the
#: wall then took the PREVIOUS typed prompt as his word and refused with a
#: mismatch: MEASURED 2026-10-12 by HELM in its own transcript, record 656 carried his
#: merge instruction as "queued" while `last-prompt` carried the same text. Over
#: the 25 largest transcripts on this machine the same day: 111 queued records,
#: 97 of the 107 with string content confirmed by a `last-prompt` prefix.
#: "system" stays out: 1,215 records, task notifications, cross-session
#: deliveries and loop prompts, none confirmed by a `last-prompt` record. The
#: capped prefix check in `_reconcile_detail` still applies to both, and it is
#: what stops a forged record.
_OPERATOR_PROMPT_SOURCES = ("typed", "queued")


def _last_operator_prompt_reason(transcript_path: str) -> tuple[str | None, str]:
    """The operator's verbatim most recent typed prompt, and why not when absent.

    IT USED TO RETURN A TRUNCATED COPY. `lastPrompt` is capped: MEASURED
    2026-09-03 over this session's own transcript, 166 `last-prompt` records,
    maximum length exactly 201, and 83 of them on that cap. Half the operator's
    prompts arrived here cut off, and with a 303-character prompt that asks for
    a push and forbids it at the end, the full text refused the push while the
    201-character copy AUTHORISED it. The wall lost the one word it exists to
    obey.

    Reads the tail first, because the transcript reached 127,337 lines in the
    session this wall was written in and this runs inside a synchronous hook.
    Falls back to the whole file whenever the tail holds no TYPED record, which
    happens after a very long single turn; a capped record alone is not enough
    to stop reading, and the comment at that line carries the measurement of
    what it cost when it was. None on any failure, and the caller treats None as
    a refusal.

    THE TIMING THIS REPORTS ON, measured rather than reasoned about. The two
    records are not written together. MEASURED 2026-09-06 over 982 turns that
    contain a tool call, across the 25 largest transcripts on this machine: in
    515 of them (52.4%) the turn's `last-prompt` record is written AFTER the
    first `tool_use` record of that turn. A commit issued as the first tool call
    of a turn therefore lands, about half the time, on a transcript holding the
    full typed record and the PREVIOUS turn's capped one, which is
    `_PROMPT_MISMATCH`, or no capped record at all, which is `_PROMPT_UNCONFIRMED`.
    Both refuse, and refusing is right: a forged typed record looks exactly the
    same. What was wrong was saying it in the words used for an unreadable file.
    `check_release_gate` now re-reads on those two before it refuses; the
    measurement of what a wait can and cannot reach is at `_await_prompt_records`.

    One mismatch is not a timing state at all, and is reported as
    `_PROMPT_COMMAND`: see that constant.
    """
    if not transcript_path:
        return None, _PROMPT_NO_TRANSCRIPT
    p = Path(transcript_path)
    if not p.is_file():
        return None, _PROMPT_NO_TRANSCRIPT

    def _decide(typed: str | None, capped: str | None,
                capped_newer: bool) -> tuple[str | None, str]:
        prompt, why = _reconcile_detail(typed, capped)
        if why == _PROMPT_MISMATCH and capped_newer and _is_slash_command(capped):
            return None, _PROMPT_COMMAND
        return prompt, why

    def _scan(blob: bytes,
              partial_first: bool) -> tuple[str | None, str | None, bool]:
        lines = blob.splitlines()
        if partial_first and lines:
            lines = lines[1:]
        typed = capped = None
        capped_newer = False
        for raw in reversed(lines):
            if capped is None and b'"last-prompt"' in raw:
                rec = _loads_or_none(raw)
                if rec is not None and rec.get("type") == "last-prompt":
                    val = rec.get("lastPrompt")
                    if isinstance(val, str):
                        capped = val
                        capped_newer = typed is None
            if typed is None and (b'"typed"' in raw or b'"queued"' in raw):
                rec = _loads_or_none(raw)
                if (rec is not None and rec.get("type") == "user"
                        and rec.get("promptSource") in _OPERATOR_PROMPT_SOURCES):
                    message = rec.get("message")
                    if (isinstance(message, dict)
                            and isinstance(message.get("content"), str)):
                        typed = message["content"]
            if typed is not None and capped is not None:
                break
        return typed, capped, capped_newer

    try:
        size = p.stat().st_size
        window = 1 << 18  # 256 KiB
        with p.open("rb") as fh:
            if size > window:
                fh.seek(size - window)
                typed, capped, capped_newer = _scan(fh.read(), partial_first=True)
                if typed is not None:
                    return _decide(typed, capped, capped_newer)
                # IT USED TO STOP HERE ON A CAPPED RECORD ALONE, and that is
                # how a prompt from an earlier turn reached the wall wearing
                # `_PROMPT_OK`. The tail is 256 KiB; one turn's tool output can
                # fill it, so the turn's typed record scrolls out of the window
                # while `last-prompt` records stay in it, being written far
                # more often: 7,144 of them against 1,647 typed records in the
                # largest transcript here. Finding one was read as "found
                # something", the
                # whole-file fallback never ran, and `_reconcile_detail` then
                # took its legacy branch and returned a 200-character copy of
                # SOME turn's prompt.
                #
                # MEASURED 2026-09-21, replaying this reader at every tool call
                # across the 25 largest transcripts on this machine: with the
                # old condition, 5,347 of 42,095 resolved tool calls were
                # answered from a capped record alone, 1,811 of them from one
                # belonging to another turn. Re-scanning the whole file finds
                # the current turn's typed record in 1,811 of those 1,811.
                #
                # Strictly narrowing: the whole-file scan sees the same capped
                # record the window saw plus whatever typed record precedes it,
                # so the outcome is either the same prompt at full length or a
                # refusal. It can never authorise something the old path
                # refused.
                fh.seek(0)
                return _decide(*_scan(fh.read(), partial_first=False))
            return _decide(*_scan(fh.read(), partial_first=False))
    except OSError:
        return None, _PROMPT_NO_TRANSCRIPT


#: How long the release gate re-reads a transcript whose two prompt records do
#: not yet agree, and how often. Chosen 2026-09-25 from what the harness does,
#: MEASURED that day on Claude Code 2.1.282: see `_await_prompt_records`. The
#: registered PreToolUse timeout for Bash is 30 s, so the whole budget is under a
#: tenth of it.
_RELEASE_WAIT_BUDGET_S = 2.0
_RELEASE_WAIT_INTERVAL_S = 0.1


def _await_prompt_records(transcript_path: str) -> tuple[str | None, str, float]:
    """Re-read until the turn's two prompt records stop being in flight.

    Returns the reader's answer on the first read that is neither
    `_PROMPT_UNCONFIRMED` nor `_PROMPT_MISMATCH`, or its last answer when the
    budget runs out, and the seconds spent. It decides nothing itself: every
    rule that follows the read is applied to whatever it returns, exactly as to
    a first read.

    WHAT A WAIT CAN REACH, MEASURED 2026-09-25 rather than assumed. The harness
    writes the capped `last-prompt` record only inside a metadata re-append,
    which runs after a flush once about 32 KiB have been appended since the last
    one; appends are flushed on a 100 ms timer. A PreToolUse hook starts BEFORE
    its own tool_use line is flushed: five sequential probes in a yard saw that
    line land 86 to 105 ms into the hook, or already on disk, and then nothing
    more for the rest of a 3 s window. Over the 25 largest transcripts on this
    machine, of 1752 turns holding a tool call, the capped record directly
    followed the first tool_use in 134, which is that flush, and a wait reaches
    it. In 589 more it landed only after the first tool RESULT, because the
    bytes that trigger it come from the tool the hook is holding back; no wait
    inside the hook reaches those, and they still refuse, with a fresh turn as
    the remedy. The budget is twenty flush intervals, so a loaded machine whose
    timer slips still lands inside it.

    Bounded by the clock as well as by the count, because one read is not
    always cheap: the whole-file fallback took 3114 ms on a 306 MB transcript,
    MEASURED 2026-09-25 at load average 2.25, so twenty of them would run past
    the hook's own timeout. The clock bound caps the whole wait at the budget
    plus one read.
    """
    started = time.monotonic()
    prompt, why = None, _PROMPT_MISMATCH
    attempts = max(1, round(_RELEASE_WAIT_BUDGET_S / _RELEASE_WAIT_INTERVAL_S))
    for _ in range(attempts):
        time.sleep(_RELEASE_WAIT_INTERVAL_S)
        prompt, why = _last_operator_prompt_reason(transcript_path)
        if why not in (_PROMPT_UNCONFIRMED, _PROMPT_MISMATCH):
            break
        if time.monotonic() - started >= _RELEASE_WAIT_BUDGET_S:
            break
    return prompt, why, time.monotonic() - started


#: A queue entry the HARNESS composed, rather than one a person typed.
#:
#: MEASURED 2026-09-22 over the ten largest transcripts on this machine: 3,651
#: `queue-operation` records with `operation: "enqueue"`, of which 2,916 open
#: with an XML-ish envelope and 735 do not. The envelopes were
#: `<task-notification>` (2,780), `<agent-message from="...">` (111) and
#: `<cross-session-message from="...">` (25). The first carries a subagent's
#: final report, the second its hand-back, the third another session's message,
#: so all three carry text some OTHER model wrote. A hand-back that ends "the
#: branch is ready, commit and push it" would authorise a release outright if
#: this reader took it for the operator.
#:
#: So the test is a SHAPE and an allowlist, never the three names. An
#: enumeration of envelopes is a list of holes, by the same reasoning the daemon
#: rule is written on: the next envelope the harness invents arrives unlisted
#: and would be read as a person. Anything opening with `<` is refused here, and
#: an operator who genuinely begins a message with `<` is refused with it, which
#: is the direction a wall is supposed to fail in.
_QUEUE_ENVELOPE = "<"


def _mid_turn_operator_word(transcript_path: str) -> str | None:
    """The operator's most recent message absorbed into THIS turn, or None.

    A SECOND SOURCE for the same question `_last_operator_prompt_reason`
    answers, and it exists because that one cannot see this shape at all. A
    message typed while the turn is still running never becomes a
    `type: "user"` record: MEASURED 2026-09-22 over a live HELM transcript, all
    three of the operator's mid-turn messages left a `queue-operation`
    `enqueue` line and a `remove` line carrying
    `reason: "absorbed_mid_turn"`, and none of the three produced a user record
    anywhere in the file. Across the same transcript: 688 absorbed removals, of
    which 5 had text that also appears as a user record. The wall reads
    `type: "user"` records and asks them for `promptSource`, so the queue line
    never reaches it, and the operator's word went unheard.

    WHAT THIS ESTABLISHES, and it is less than "the operator said so". A
    `queue-operation` record has five fields and NONE of them is a provenance:
    `type`, `operation`, `content`, `sessionId`, `timestamp`. A brief another
    session delivers into a BUSY session lands in this same queue and is
    indistinguishable by field. Two filters stand in for the field that does
    not exist, and both are refusals rather than judgements: `_QUEUE_ENVELOPE`
    drops everything the harness composed, and `_BRIEF_MARKER` drops a brief
    that `scripts/herdr-brief.py` marked. The second is fail-open by
    construction in exactly the way `_BRIEF_MARKER` already documents for the
    typed path: an UNMARKED brief still passes, and only the sender puts the
    marker there. This reader is no weaker than the one beside it and no
    stronger.

    STALENESS, which is the hole a second source opens if it is not closed. The
    typed path ties a prompt to the current turn by requiring the capped record
    to be a prefix of it; a queue line has nothing to pair with. So the scan
    runs BACKWARDS from the end of the file and stops at the first turn
    boundary it meets, which is any `type: "user"` record that is not a tool
    result. Anything between that boundary and the end of the file belongs to
    the turn running now; a word from an earlier turn is behind the boundary and
    is never reached. MEASURED 2026-09-22 over the same ten transcripts, a user
    record is a turn boundary when it carries a `promptSource` (1,314 typed, 884
    system, 77 queued, 32 typed with non-string content, 3 queued) or when its
    `message.content` is a string with no `promptSource` (1,918, the slash
    command and local-command-caveat records); the 48,778 with list content are
    tool results and are not boundaries. Both halves of that union are stop
    conditions, because a stop condition can only make this reader answer None.

    Bounded to the last 256 KiB, like the reader above, and NOT widened to the
    whole file when that window holds neither a boundary nor a candidate. The
    two readers fail in opposite directions, which is why they are bounded
    differently: widening makes that one find a prompt it would otherwise miss,
    and would make this one reach past a boundary that scrolled out of the
    window. Here the window running out means None, which is a refusal the
    operator clears by typing the word in a fresh turn.
    """
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.is_file():
        return None
    try:
        size = p.stat().st_size
        window = 1 << 18  # 256 KiB, the same window the reader above uses
        with p.open("rb") as fh:
            if size > window:
                fh.seek(size - window)
                lines = fh.read().splitlines()[1:]  # first line is a fragment
            else:
                lines = fh.read().splitlines()
    except OSError:
        return None

    for raw in reversed(lines):
        if b'"user"' not in raw and b'"queue-operation"' not in raw:
            continue
        rec = _loads_or_none(raw)
        if rec is None:
            continue
        kind = rec.get("type")
        if kind == "user":
            message = rec.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(rec.get("promptSource"), str) or isinstance(content, str):
                return None  # the turn started here; anything older is not ours
            continue
        if kind != "queue-operation" or rec.get("operation") != "enqueue":
            continue
        text = rec.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        if text.lstrip().startswith(_QUEUE_ENVELOPE):
            continue  # the harness composed it; keep looking for a person
        if _BRIEF_MARKER in text:
            # The most recent word in this turn is a machine-to-machine brief,
            # so there is no operator word to find behind it. Refused here AND
            # again in `prompt_authorises`, deliberately: this reader must be
            # safe for a caller that never reaches that function, and that
            # function must be safe for a prompt that never came through here.
            return None
        return text
    return None


# WHAT ONE ROW OF THIS JOURNAL MEANS, written here 2026-09-23 because nothing
# said it and the absence cost a false reading of the file (HOS-81).
#
# ONE ROW IS ONE PERMITTED RELEASE ACTION: one Bash command that `release_action`
# classified as a commit or a push, and that this gate then let through. It is
# not one operator decision, and the two counts diverge by a factor the file
# itself never states. A refusal writes nothing here; it goes to `_record_denial`.
# A row is written BEFORE the command runs, so a permitted command that then
# fails, or that a later wall refuses, still has its row. A chained
# `git commit && git push` is ONE row carrying `push`, because `release_action`
# returns the first action it finds and never a list.
#
# THAT IS THE DESIGN AND NOT AN OVERSIGHT, which HOS-81 asked and would not
# answer from behaviour. The record is written only on the two paths that
# RELEASE, the log is described in its own docstring as the trail by which
# "every commit and push this workspace makes can be traced back to the exact
# words that authorised it", and `HOOKS-REFERENCE.md` (now under `spec-docs` in
# the data repository) states the same contract in its own words. An audit
# trail of actions is what a commit or a push needs answered later; a trail of
# decisions is a different question asked of the same rows.
#
# WHICH IS WHY REPEATS ARE EXPECTED AND MUST NOT BE SUPPRESSED HERE. MEASURED
# 2026-09-23 over the registered journal: 1682 rows, 115 distinct authorising
# phrasings, and one phrasing covering 1004 rows across three days. Collapsing
# them at the WRITE end would destroy the action trail to answer the other
# question; `scripts/release-journal.py` collapses them at the READ end instead
# and prints the check count beside each decision. Those 1682 rows read as 260
# decisions, and the 1004 as 7. No row is ever rewritten: this path only ever
# appends.
#
# WHAT THE RECORD CANNOT ANSWER, so that the next reader does not infer it. There
# is no session id and no turn id, so two identical authorisations typed in a row
# are indistinguishable from one, and any decision count taken off this file is a
# floor. Rows written before 2026-09-08 carry no `checkout` and no `branch`.
def _record_release(action: str, command: str, prompt: str) -> None:
    """Append one authorised release to a log the operator can audit.

    Telemetry only; it can never change a decision. The point is that every
    commit and push this workspace makes can be traced back to the exact words
    that authorised it, which is the thing that was missing when the rule was
    broken. Everything below stays inside one `except Exception`, so a durable
    path that does not exist, is not writable, or sits on a disconnected volume
    prints and is dropped. It never turns into a refused commit.

    `checkout` and `branch` are what a SHARED file needs that a per-checkout one
    did not. One log now carries HELM's releases and every yard's; without them
    the merged trail answers a weaker question than the file it replaced, since
    "who authorised this commit" is only half an answer if you cannot say which
    tree the commit happened in.

    ONE `os.write` OF ONE PRE-ENCODED LINE, because several yards and HELM
    append to this file concurrently. A buffered text handle is free to split a
    record across write() calls whenever the payload outgrows its buffer, and
    two split records interleave into two corrupt lines. A single write() on a
    regular file opened O_APPEND resolves the offset and copies under the
    inode lock, so a concurrent append lands before or after, never inside.
    Worst-case line length is bounded by the four caps below and was MEASURED
    2026-09-08 at 8517 bytes. A short return is reported rather than assumed
    away. `os.write` rather than a buffered handle buys independence from a
    buffer size nobody would think to check (2026-09-08 mutation run).
    """
    try:
        import datetime as _dt

        path = _release_log_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        rec = {
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "command": (command or "")[:400],
            "authorised_by": (prompt or "")[:400],
            "checkout": str(WORKSPACE)[:400],
            "branch": _checkout_branch(WORKSPACE)[:200],
        }
        blob = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = os.write(fd, blob)
        finally:
            os.close(fd)
        if written != len(blob):
            print(f"[_dispatch] release log wrote {written} of {len(blob)} "
                  f"bytes; the last line may be truncated.", file=sys.stderr)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[_dispatch] release log unavailable ({type(exc).__name__}): {exc}",
              file=sys.stderr)


# The refusal quotes the operator's own typing back, because "why did this
# refuse" is the difference between a wall people keep and a wall people
# disable. But it is read by AGENTS as often as by humans, and MEASURED
# 2026-08-31 a subagent that hit this refusal read the quoted Russian text as a
# prompt-injection attempt and filed it as a security finding. A control that
# cries wolf gets routed around, exactly as over-friction does.
#
# So the excerpt is fenced by a stable label, flattened to one line, and capped.
# The label is the one stable thing to assert on. Pinning the surrounding prose
# would break on the next rewording and teach the next person to loosen the
# check instead of fixing the wall.
#
# The text comes from `_last_operator_prompt_reason`, which reads only
# `type: "last-prompt"` and `promptSource: "typed"` or `"queued"` records. That
# establishes the RECORD, never the AUTHOR (see `_BRIEF_MARKER`). The label
# keeps `[operator-prompt]` for continuity with the refusals already in the
# audit trail: read it as a fence around a prompt record, not an attribution.
_EVIDENCE_LABEL = "[operator-prompt]"
_EVIDENCE_LIMIT = 160


def _quoted_evidence(prompt: str) -> str:
    """The operator's words as one capped, labelled, inert line."""
    flat = " ".join((prompt or "").split())
    if len(flat) > _EVIDENCE_LIMIT:
        flat = flat[:_EVIDENCE_LIMIT] + "..."
    return f"{_EVIDENCE_LABEL} {flat!r}"


def check_release_gate(payload: dict) -> dict | None:
    """Refuse a commit or a push the operator did not ask for in this turn."""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command") or ""
    release = release_action(command)
    send = mattermost_send_action(command)
    if send and release:
        # One prompt word would otherwise authorise both: "отправь" is a push
        # word too. Two commands, two separate checks.
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": (f"RELEASE GATE: this command both sends a message "
                       f"({outbound_send_target(command)}) and does a {release}. "
                       f"Run them as separate commands."),
        }
    action = send or release
    if action is None:
        return None

    transcript = payload.get("transcript_path") or ""
    prompt, why = _last_operator_prompt_reason(transcript)
    waited = 0.0
    if prompt is None and why in (_PROMPT_UNCONFIRMED, _PROMPT_MISMATCH):
        # Refusing on the first read was the defect (HOS-128), not refusing.
        # Whatever the re-read returns goes through every rule below unchanged.
        prompt, why, waited = _await_prompt_records(transcript)
    if prompt is None and why == _PROMPT_COMMAND:
        # The typed record belongs to the turn before a slash command, so it is
        # not consulted at all. Only the mid-turn reader can speak for this
        # turn, with its envelope and brief-marker refusals intact, and through
        # the same `prompt_authorises` as every other word.
        mid = _mid_turn_operator_word(transcript)
        if mid is not None and prompt_authorises(mid, action):
            _record_release(action, command, mid)
            return None
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": (
                f"RELEASE GATE: the newest prompt record is a slash command, so "
                f"this {action} is refused.\n\n"
                "A slash command such as `/compact` writes a capped "
                "`last-prompt` record and no typed record, so the typed record "
                "on file is the prompt from BEFORE it and does not speak for "
                "this turn. Nothing typed since, including mid-turn, authorises "
                f"a {action}.\n\n"
                "What clears it: the operator asks for it in a fresh turn. Do "
                "not edit the transcript, and do not work around this."
            ),
        }
    if prompt is None and why in (_PROMPT_UNCONFIRMED, _PROMPT_MISMATCH):
        # A DIFFERENT refusal, because a different thing happened. The wall is
        # unchanged: both states still refuse, and both still refuse for the
        # same reason, which is that a typed record with nothing to confirm it
        # is exactly what a forged one looks like. What changed on 2026-09-06 is
        # that the two used to print the same sentence as an unreadable file,
        # and establishing which had happened cost a yard a trip through the
        # reader and the raw JSONL of a live transcript.
        missing = ("no `last-prompt` record for this turn has been written yet"
                   if why == _PROMPT_UNCONFIRMED else
                   "the `last-prompt` record present is not a prefix of it, so "
                   "it belongs to an earlier turn")
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": (
                f"RELEASE GATE: the transcript's two records of the operator's "
                f"prompt do not yet agree, so this {action} is refused.\n\n"
                f"The full text is there and {missing}. The wall re-read the "
                f"transcript for {round(waited * 1000)} ms and the two never "
                f"agreed.\n\n"
                f"This is a TIMING state, not a broken file, and it is common: "
                f"MEASURED 2026-09-06 over 982 turns holding a tool call, the "
                f"turn's `last-prompt` record is written after its first "
                f"tool_use record in 515 of them. The harness writes it only "
                f"after enough bytes have been appended, and the bytes it is "
                f"waiting for usually come from the result of the very tool "
                f"this wall is holding back, so no wait here can produce "
                f"them.\n\n"
                f"It fails closed on purpose. The capped record is the one "
                f"thing here a model cannot write, so accepting the full text "
                f"without it would accept a forged record just as readily.\n\n"
                f"What usually clears it: let a tool call complete, then run "
                f"the {action} again, because the capped record most often "
                f"lands just after the first tool RESULT. It does not always: "
                f"MEASURED 2026-09-06 on this wall's own first refusal, two "
                f"capped records were written after the operator's word and "
                f"both still carried the PREVIOUS prompt. When the records "
                f"never agree, the remedy is a fresh turn, not another tool "
                f"call. Do not edit the transcript, and do not work around "
                f"this."
            ),
        }
    if prompt is None and why == _PROMPT_TRUNCATED:
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": (
                f"RELEASE GATE: only a TRUNCATED copy of a prompt is readable, "
                f"so this {action} is refused.\n\n"
                "The transcript holds a `last-prompt` record sitting on the "
                "harness's 200-character cap, and no untruncated `promptSource: "
                "\"typed\"` or `\"queued\"` record to restore the rest of it. The words past the "
                "cap are not in front of this wall, and a prohibition is exactly "
                "what goes at the end of a sentence: MEASURED 2026-09-03, a "
                "303-character prompt that asks for a push and forbids it at the "
                "end AUTHORISED the push through its 201-character copy.\n\n"
                "Until 2026-09-21 this state returned the truncated text as the "
                "operator's words, and the record it returned was not always "
                "even this turn's.\n\n"
                "What clears it: a fresh turn, so that both records of one "
                "prompt are written. Report it to the operator rather than "
                "working around it."
            ),
        }
    if prompt is None:
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": (
                "RELEASE GATE: cannot read the operator's most recent prompt, so "
                "this "f"{action} is refused.\n\n"
                "This wall reads `last-prompt` from the session transcript to "
                "confirm the operator asked for this, in this turn. It could not, "
                "and it fails closed: a gate that opens when it cannot see is not "
                "a gate.\n\n"
                "Report this to the operator rather than working around it."
            ),
        }

    if prompt_authorises(prompt, action):
        _record_release(action, command, prompt)
        return None

    # A SECOND SOURCE for the same word, and it is consulted only here, after
    # the turn's own prompt has been read and has not authorised. That position
    # is the whole of what keeps this addition from being a loosening: every
    # refusal above still returns before this line, so a turn whose two prompt
    # records do not agree, or whose only readable copy is truncated, refuses
    # exactly as it did. What changes is one case and one only: the turn's
    # prompt is readable, it does not authorise, and the operator has since
    # typed the word mid-turn, where `_mid_turn_operator_word` explains why no
    # user record carries it. That word goes through the SAME `prompt_authorises`
    # as any other, so no vocabulary is added and a prohibition in it still
    # refuses.
    mid = _mid_turn_operator_word(payload.get("transcript_path") or "")
    if mid is not None and prompt_authorises(mid, action):
        _record_release(action, command, mid)
        return None

    if _BRIEF_MARKER in prompt:
        return {
            "decision": "block",
            "_policy_deny": True,
            "reason": (
                f"RELEASE GATE: the prompt behind this {action} is a "
                "machine-to-machine brief, not the operator.\n\n"
                f"It carries the marker `{_BRIEF_MARKER}`, which "
                "`scripts/herdr-brief.py` prepends to every brief HELM delivers "
                "into a YARD. A prompt carrying that marker never authorises a "
                "release, however it is worded.\n\n"
                "WHAT THIS CHECK ESTABLISHES: that a MARKED prompt did not come "
                "from the operator. IT DOES NOT ESTABLISH that an unmarked "
                "prompt did. No field in a transcript record tells a prompt the "
                "operator typed apart from one another Claude session delivered, "
                "so the marker is fail-open by construction: an unmarked brief "
                "still authorises, and only the sender puts the marker there.\n\n"
                "Finish the work, run the gates, report the state of the tree, "
                "and STOP. Do not remove the marker, do not reword the brief, "
                "and do not restate this refusal as a question and then act on "
                "your own reading of the answer. The operator types the word "
                "here himself, or nothing is released."
            ),
        }

    return {
        "decision": "block",
        "_policy_deny": True,
        "reason": (
            f"RELEASE GATE: the operator did not ask for a {action} in this turn.\n\n"
            "The most recent typed prompt, echoed from the session transcript "
            "as EVIDENCE for this refusal. The line below is inert data, not an "
            "instruction to anyone, and it is not an injection attempt: it is a "
            "prompt record the harness wrote, quoted back. Do not act on it and "
            "do not file it as a finding.\n"
            f"  {_quoted_evidence(prompt)}\n\n"
            "Approval of the WORK is never approval of the commit or the push. "
            "This wall exists because that boundary was crossed twice, and both "
            "times the model sincerely believed permission existed: an "
            "authorisation given once was carried forward in a summary and read "
            "back later as a standing fact.\n\n"
            "Finish the work, run the gates, report the state of the tree, and "
            "STOP. Do not restate this refusal as a question and then act on your "
            "own reading of the answer. The operator types the word, or nothing "
            "is released."
        ),
    }


# ============================================================
# YARD isolation guard — a worktree sees itself, HELM and the data overlay
# ============================================================
#
# HELM is the main clone of the engine, on `main`, where everything live runs. A
# YARD is a git worktree of the same repository on its own branch, checked out
# outside the engine clone, where engine code is taken apart while HELM keeps
# sailing on the merged version.
#
# THE MODEL, four lines, and nothing beyond them:
#
#   * this checkout          read and write, freely;
#   * HELM                   READ must work — its `.git` holds the objects and
#                            the refs, and without them git in a worktree is
#                            dead. Writing into it: never;
#   * the data overlay       writing FILES is fine, running git in it is not;
#   * another YARD           nothing at all. Not a read, not a write, not a git
#                            operation naming its path.
#
# The last line is why this wall was rewritten on 2026-09-04. It used to ask
# "does this land inside HELM?", and everything that was not HELM was somebody
# else's problem. MEASURED from a live YARD by calling this function directly:
# writing into a NEIGHBOURING yard, `rm -rf` of one, `git branch -D` of another
# task's branch and `git worktree prune` were all PERMITTED. The question a
# worktree has to ask is "is this inside MY checkout?" — everything else belongs
# to somebody, and two of the somebodies are not HELM.
#
# THE HONEST BOUNDARY, and it is narrower than it looks. This is a PreToolUse
# hook. It sees TOOL CALLS the model makes and nothing else. It does not see
# inside a process a tool call starts: an entire `pytest` run is ONE Bash call,
# so every `open()`, every `shutil.rmtree` and every `subprocess` inside the
# suite passes this wall once, as the string `pytest`, and is then unobserved.
# The damage that prompted this change came from exactly there — a fixture's
# `shutil.rmtree` removed the repository's worktree registrations, not a tool
# call. So this closes the AGENT. It does not close tests, and it does not close
# scripts. Do not read this wall as protection against code the agent runs.
#
# The only guard that runs INSIDE a process is
# `scripts/utils/overlay_write_guard.py`, and it was NOT extended here. It
# watches the data overlay's roots, so a neighbouring worktree is invisible to
# it and so is HELM's `.git`. Extending it was considered and deliberately left
# undone, because it is three changes rather than one: the sibling enumeration
# would have to move to `scripts/utils/clone_guard.py` to avoid a second copy;
# the new roots must be refusal-only and NOT snapshot roots, since `arm()` feeds
# one set to both and walking HELM at every interpreter start is not affordable;
# and to cover the incident that prompted this it would have to protect
# `<HELM>/.git/worktrees`, which the suite's own fixtures legitimately write
# when they clean up their entry. That last one cannot be settled without
# measuring the suite against it. It is a task with its own evidence, not a
# rider on this one.
#
# This check lives HERE, inside the dispatcher, because `WORKSPACE` is already
# this checkout, the payload schema, block protocol, imports and registration
# are already right, and a raise is caught by `crashed_wall_block` and BLOCKS
# (each of these went wrong in a separate-hook draft, 2026-09-03).
#
# Writing FILES into the data overlay from a YARD is PERMITTED and expected:
# reports, decks, PDFs. Only two things are refused, and the rule is short
# enough to hold in your head: not into HELM, and no git in the data overlay.
# The git index of the overlay is shared by every checkout, so a commit from a
# task sweeps up a neighbour's half-finished work and whatever the mail sync is
# writing at that moment into one unreviewed commit.

# Resolved once per process. The hook runs on every Write, Edit, MultiEdit,
# NotebookEdit, Bash, Read, Grep, Glob and Agent call, so this must cost a stat
# and not a subprocess. `.git` is a directory in a main clone and a file in a
# worktree; `None` means neither, which is the only case worth asking git about.
_DOT_GIT = WORKSPACE / ".git"
if _DOT_GIT.is_dir():
    _IN_A_YARD = False
elif _DOT_GIT.is_file():
    _IN_A_YARD = True
else:
    _IN_A_YARD = None  # undetermined; resolved lazily, and fail-closed

_YARD_READ_ONLY_VERBS = frozenset((
    "cat", "less", "more", "head", "tail", "diff", "rg", "grep", "egrep",
    "ls", "stat", "wc", "file", "find", "sed", "awk", "cut", "sort", "uniq",
    "cd", "pwd", "echo", "test", "["
))

# Read-only git verbs. Anything else spelled `git` and aimed at HELM or at the
# data overlay is refused.
#
# `branch` and `worktree` were on this list until 2026-09-04 and that is where
# two of the four measured holes were: `git branch -D <another task's branch>`
# and `git worktree prune` both classified as reads and sailed through. Neither
# verb is read-only or write-only — each has a listing form and a mutating one —
# so they are classified by `_yard_git_subcommand_is_listing` below instead of
# by membership here.
_YARD_READ_ONLY_GIT = frozenset((
    "log", "show", "diff", "status", "remote", "rev-parse",
    "describe", "blame", "shortlog", "ls-files", "cat-file",
))

# Every git verb this guard recognises, so `git -C <path> branch` finds `branch`
# rather than mistaking `<path>` for the subcommand.
_YARD_GIT_VERBS = _YARD_READ_ONLY_GIT | {"branch", "worktree"}

# `git branch`, listing forms only. An ALLOW-list rather than a list of mutating
# flags, and deliberately: an enumeration of the dangerous verbs is a list of
# holes, and this repository has already paid for one (the daemon prohibition
# that named install/restart/uninstall and let `start` through). A flag git adds
# next year is unrecognised here and therefore REFUSED, which is the direction
# that survives being out of date.
_YARD_BRANCH_LISTING_FLAGS = frozenset((
    "-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv", "-vvv",
    "--verbose", "--show-current", "--merged", "--no-merged", "--contains",
    "--no-contains", "--points-at", "--sort", "--format", "--color",
    "--no-color", "--column", "--no-column", "-q", "--quiet", "-i",
    "--ignore-case", "--omit-empty",
))

# The flags that rename or copy a branch. `git branch -m <new>` with a single
# operand renames the branch you are standing on, which is your own.
_YARD_BRANCH_MOVE_FLAGS = frozenset(("-m", "-M", "--move", "-c", "-C", "--copy"))

# Splits a command into the links of its chain. Checking only the START of the
# whole string, which is what the previous plan revision did, let
# `cat <HELM>/x; rm -rf <HELM>/y` through because the string began with `cat`,
# and refused `cd /tmp && cat <HELM>/x` because it did not. Both are fixed by
# asking about each link.
#
# Kept as the FALLBACK, for the two inputs the quote-aware splitter below
# refuses to guess at: a heredoc, and a command whose quoting does not close.
#
# Its bluntness, stated because it is met in practice rather than in theory: a
# heredoc body is split the same way, so writing a file whose CONTENT quotes a
# refused command is itself refused. That is the safe direction and the same
# trade the redirection check below makes, and the workaround is one tool call
# (write the file with the Write tool, not with `cat <<EOF`).
_YARD_CHAIN_RE = re.compile(r"(?:&&|\|\||;|\||\n)")

# The shells whose `-c` argument is a COMMAND rather than data, and so is read
# as one. `eval` is handled beside them for the same reason.
_YARD_SHELL_VERBS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "ash"})

# How far a payload may nest before the descent stops. `sh -c "sh -c ..."` is
# already contrived; three is a bound, not a judgement about what is legitimate.
_YARD_PAYLOAD_DEPTH = 3

# Prefixes that carry a command without being one. Stripped in exactly one
# place, because `_yard_words` and `_yard_shell_payload` both need the same
# answer and a second copy is the one that stops being fixed.
_YARD_WORD_PREFIXES = ("sudo", "env", "time", "nice", "nohup")

#: Shell control keywords that stand BEFORE the verb of a segment and say
#: nothing about what that segment does. `do find "$t" -type f` runs `find`.
#:
#: Without them (until 2026-09-18) a read-only `for t in a b; do find "$t"
#: ...; done` classified as not-read-only, and its loop words were read as
#: write destinations. Stripping them does not loosen the classifier:
#: `do rm "$t"` is still not read-only, now because `rm` is on no list.
#: Shortening this list tightens the classifier silently; lengthening it
#: loosens a wall with nothing to say so.
_YARD_SHELL_KEYWORDS = (
    "for", "do", "done", "while", "until", "if", "then", "elif", "else", "fi",
    "case", "esac", "in", "{", "}", "(", ")", "!",
)


def _yard_strip_prefix(words: list[str]) -> list[str]:
    """Drop a leading `sudo`/`env FOO=1`/`time`/`do` prefix from a link's words.

    The shell keywords are dropped for the same reason `sudo` is: neither says
    anything about what the segment does, and leaving one in place means the
    verb test reads a word that is not the verb.
    """
    while words and (words[0] in _YARD_WORD_PREFIXES
                     or words[0] in _YARD_SHELL_KEYWORDS
                     or "=" in words[0] and not words[0].startswith("-")):
        words = words[1:]
    return words


def _yard_quoted_positions(text: str) -> list[bool]:
    """True at every index the shell would NOT read as syntax.

    A character inside single or double quotes, the quote characters
    themselves, and anything a backslash escaped. Raises `ValueError` when the
    text ends inside a quote, because a mask over text this scanner could not
    parse is a guess, and every caller falls back to the blunt split rather
    than act on one.
    """
    mask = [False] * len(text)
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            mask[index] = True
            if char == "\\" and quote == '"' and index + 1 < len(text):
                mask[index + 1] = True
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char == "\\" and index + 1 < len(text):
            mask[index + 1] = True
            index += 2
            continue
        if char in "'\"":
            quote = char
            mask[index] = True
        index += 1
    if quote:
        raise ValueError(f"unterminated {quote} quote")
    return mask


def _yard_split_chain(command: str, mask: list[bool]) -> list[str]:
    """Split on the chain operators the shell would act on, and no others."""
    segments: list[str] = []
    start = index = 0
    length = len(command)
    while index < length:
        if mask[index]:
            index += 1
            continue
        char = command[index]
        if (char in "&|" and index + 1 < length
                and command[index + 1] == char and not mask[index + 1]):
            segments.append(command[start:index])
            index += 2
            start = index
            continue
        if char in ";|\n":
            segments.append(command[start:index])
            index += 1
            start = index
            continue
        index += 1
    segments.append(command[start:])
    return [segment.strip() for segment in segments if segment.strip()]


def _yard_shell_payload(link: str) -> str | None:
    """The command text this link would EXECUTE as a shell, or None.

    `sh -c '<payload>'` and `eval '<payload>'` run their argument; `grep`,
    `echo` and `herdr agent prompt` carry the same characters as DATA. Once
    quoting is respected, that difference has to be made explicitly, and
    MEASURED 2026-09-08 it was not being made at all: `sh -c 'rm -rf <yard>'`
    was PERMITTED by every wall in this file, because the single-quoted payload
    contained no chain operator to split on and the link's verb was `sh`. The
    double-quoted spelling was refused only by accident of where the blunt
    split happened to cut it.
    """
    import shlex
    try:
        words = _yard_strip_prefix(shlex.split(link))
    except ValueError:
        return None          # unbalanced quoting; the caller has the blunt path
    if not words:
        return None
    verb = words[0].rsplit("/", 1)[-1]
    if verb == "eval":
        return " ".join(words[1:]) or None
    if verb not in _YARD_SHELL_VERBS:
        return None
    # `-c`, and the SHORT bundles that carry it: `-lc`, `-euc`. The payload is
    # the first word after the flag carrying it.
    #
    # A long option is skipped, and that is not tidiness. Found by mutation
    # 2026-09-08: the first version asked only whether the word held a `c`, so
    # `bash --norc -c '<payload>'` matched on `--norc`, returned the literal
    # `-c` as the payload, and the real one was never read at all.
    for index, word in enumerate(words[1:], start=1):
        if (word.startswith("-") and not word.startswith("--")
                and "c" in word[1:]):
            rest = words[index + 1:]
            return rest[0] if rest else None
    return None


def _yard_in_a_yard() -> bool:
    """True when this checkout is a worktree. Raises when it cannot be told."""
    if _IN_A_YARD is not None:
        return _IN_A_YARD
    from scripts.utils.clone_guard import is_main_clone
    return not is_main_clone(WORKSPACE)


def _yard_links(command: str) -> list[str]:
    """The chain's links: quote-aware where the quoting can be established.

    MEASURED 2026-09-08. Splitting on every `|` and every newline regardless of
    quoting made the wall refuse commands that merely QUOTE a refused one:
    `herdr agent prompt <id> "...\\n  git worktree remove --force <path>\\n..."`
    was refused as though the middle line were a command, and so was any
    `grep`, `rg` or `echo` of the same text. The brief describing this defect
    was itself refused on its first send, by the wall it describes.

    A heredoc keeps the blunt split, deliberately. Its body is not shell
    quoting, an apostrophe in English prose inside one would open a quote this
    scanner would then close somewhere arbitrary, and reading heredoc bodies is
    a property the write guard is documented to want.
    """
    if "<<" in command:
        return [seg.strip() for seg in _YARD_CHAIN_RE.split(command) if seg.strip()]
    try:
        mask = _yard_quoted_positions(command)
    except ValueError:
        # Quoting that does not close. The shell would reject this outright, so
        # there is no correct reading to find; take the blunt one, which splits
        # MORE rather than less.
        return [seg.strip() for seg in _YARD_CHAIN_RE.split(command) if seg.strip()]
    return _yard_split_chain(command, mask)


def _yard_segments(command: str, _depth: int = _YARD_PAYLOAD_DEPTH) -> list[str]:
    """Every link that would RUN, the payload of a `sh -c` included.

    The payload is appended immediately after the link that carries it, so a
    `cd` inside one is followed for the commands that come after it. Its limit,
    stated rather than left to be found: that `cd` also reaches the OUTER links
    that follow, where a real shell would have discarded it, so
    `bash -c "cd /elsewhere" && rm -rf <relative>` resolves the relative path
    against the wrong directory. The alternative was not descending at all,
    which is the hole in `_yard_shell_payload`'s docstring.
    """
    out: list[str] = []
    for link in _yard_links(command):
        out.append(link)
        if _depth > 0:
            payload = _yard_shell_payload(link)
            if payload:
                out.extend(_yard_segments(payload, _depth - 1))
    return out


# HOS-196. Every check in the two yard walls reads command TEXT, so a path held
# in a shell variable was invisible to them. MEASURED 2026-10-04: a yard ran
# `H=<HELM>; S=<data scratch>; cd $S; git clone --mirror --no-local $H ...` and
# `H=<HELM>; D=<overlay folder>; git -C $H bundle create $D/x.bundle --all`, and
# both passed, while the same commands written literally were refused. Refusing
# every variable was measured too, over the yards' own history, and would have
# refused 237 commands that select a directory through one, the sanctioned
# `S=$(... data-path.py scratch); cd $S` among them. So the text is EXPANDED
# first, with what can be known without executing anything, and every existing
# check then runs on the expanded text.
_YARD_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")

# The workspace's own resolver, as a shell spells it: `$(python3
# scripts/data-path.py scratch)` and `$(... data-path.py path [--create] <name>
# [key=value ...])`. Anything else inside `$(...)` is unknowable here.
_YARD_DATA_PATH_SUB_RE = re.compile(
    r"\$\(\s*(?:[^\s()]*python[\d.]*\s+)?([^\s()]*scripts/data-path\.py)\s+"
    r"(scratch|path(?:\s+--create)?\s+[^\s()]+(?:\s+[\w-]+=[^\s()]+)*)\s*\)")

_YARD_ASSIGN_RE = re.compile(r"[A-Za-z_]\w*=")
_YARD_DECLARE_VERBS = frozenset(("export", "declare", "local", "readonly", "typeset"))


def _yard_resolve_data_path(match: re.Match, current: Path) -> str | None:
    """What the shell's `$(... data-path.py ...)` would print, in-process.

    The same resolver the script calls, never a shell, and never the creating
    half: `scratch` and `--create` make the directory, and a guard that writes
    is not a guard. The checkout is the one the spelled script belongs to, which
    is the one the shell would have run. None when anything cannot be told.
    """
    script = Path(match.group(1).strip("'\""))
    script = script if script.is_absolute() else current / script
    if not script.is_file():
        return None
    words = [w.strip("'\"") for w in match.group(2).split() if w != "--create"]
    try:
        from scripts.utils import data_path as resolver
        if words[0] == "scratch":
            return str(resolver.data_path(
                "checkout-scratch",
                checkout=resolver.checkout_name(script.resolve().parent.parent)))
        # The name is the shell's argv to `data-path.py path`, read back here
        # to judge the command, the one case `_cli` in data_path exempts too.
        return str(resolver.data_path(words[1],  # leak-guard: ok (the shell's argv to data-path.py path, read to judge the command)
                                      **resolver._params_from_argv(words[2:])))
    except Exception as exc:  # noqa: BLE001 - unresolved is the safe answer
        print(f"[_dispatch:yard] data-path substitution unresolved "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def _yard_expand(text: str, env: dict[str, str], current: Path) -> str:
    """`text` with every value this hook can know put in place of its name.

    Outside single quotes, as the shell does: `$NAME` and `${NAME}` from `env`,
    the data-path substitutions above, and a word-initial `~`. Anything else
    stays as written, so an unresolved name is still visibly a `$`.
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = "" if char == "'" else quote
        elif char == "\\" and index + 1 < len(text):
            out.append(text[index:index + 2])
            index += 2
            continue
        elif char == '"' or (char == "'" and not quote):
            quote = "" if quote == char else char
        elif char == "$":
            sub = _YARD_DATA_PATH_SUB_RE.match(text, index)
            value = _yard_resolve_data_path(sub, current) if sub else None
            if value is not None:
                out.append(value)
                index = sub.end()
                continue
            var = _YARD_VAR_RE.match(text, index)
            name = var and (var.group(1) or var.group(2))
            if name in env:
                out.append(env[name])
                index = var.end()
                continue
        elif (char == "~" and not quote and "HOME" in env
              and (index == 0 or text[index - 1] in " \t=:")
              and (index + 1 == len(text) or text[index + 1] in "/ \t;&|)")):
            out.append(env["HOME"])
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _yard_record_assignments(link: str, env: dict[str, str]) -> None:
    """Remember what an already-expanded link assigns, and forget what it hides.

    A link made only of `NAME=value` words, or `export`/`declare` ones, sets
    them for the links that follow. A prefix assignment (`NAME=v cmd`) applies to
    that one command and sets nothing. A value still holding `$`, a backtick or
    `(` is unknowable, and so is a `for` or `read` variable: those are DROPPED,
    so a value inherited from the environment is never mistaken for them.
    """
    import shlex
    try:
        words = shlex.split(link)
    except ValueError:
        words = link.split()
    if words[:1] == ["for"] and len(words) > 1:
        env.pop(words[1], None)
        return
    if words[:1] == ["read"]:
        for word in words[1:]:
            env.pop(word, None)
        return
    declared = bool(words) and words[0] in _YARD_DECLARE_VERBS
    if declared:
        words = [w for w in words[1:] if not w.startswith("-")]
    assigns = [w for w in words if _YARD_ASSIGN_RE.match(w)]
    if not declared:
        assigns = words[:next((i for i, w in enumerate(words)
                               if not _YARD_ASSIGN_RE.match(w)), len(words))]
    sets = declared or len(assigns) == len(words)
    for word in assigns:
        name, value = word.split("=", 1)
        if any(mark in value for mark in "$`("):
            env.pop(name, None)
        elif sets:
            env[name] = value


def _yard_expanded_segments(command: str, cwd: Path,
                            _env: dict[str, str] | None = None,
                            _depth: int = _YARD_PAYLOAD_DEPTH) -> list[str]:
    """`_yard_segments`, each link expanded against what came before it.

    One function, both walls: `check_yard_write_guard` and
    `_yard_deletion_requests` judge the text this returns. The environment is
    the hook's own, which the Bash tool's shell inherits from the same parent,
    with `PWD` following each `cd`.

    Its limits, stated rather than left to be found. A variable assigned in one
    Bash call is unknown in the next. A `sh -c` payload sees every variable the
    outer links set, exported or not. A value the shell would word-split is
    inserted whole. And an unresolved name in a PLAIN operand, a loop variable
    in `rm $f`, stays unjudged: only a directory-selecting position refuses one,
    in `_yard_unresolved_directory`.
    """
    env = dict(os.environ, PWD=str(cwd)) if _env is None else _env
    out: list[str] = []
    for link in _yard_links(command):
        expanded = _yard_expand(link, env, Path(env["PWD"]))
        out.append(expanded)
        words = _yard_words(expanded)
        if words and words[0].rsplit("/", 1)[-1] == "cd":
            env["PWD"] = str(_yard_follow_cd(words, Path(env["PWD"])))
        _yard_record_assignments(expanded, env)
        payload = _yard_shell_payload(expanded) if _depth > 0 else None
        if payload:
            out.extend(_yard_expanded_segments(payload, cwd, env, _depth - 1))
    return out


def _yard_unresolved_directory(segment: str) -> str | None:
    """The directory-selecting word of an expanded link that is still unknown.

    `cd`, `git -C`, `--git-dir`, `--work-tree`, `GIT_DIR=` and `GIT_WORK_TREE=`
    decide where everything after them runs, so a `$` or a backtick left there
    after expansion means the guard would judge the wrong directory.
    """
    def unknown(word: str) -> bool:
        return "$" in word or "`" in word

    spoken = segment.split()
    if any(w.startswith(("GIT_DIR=", "GIT_WORK_TREE=")) and unknown(w) for w in spoken):
        return next(w for w in spoken if w.startswith(("GIT_DIR=", "GIT_WORK_TREE=")))
    words = _yard_strip_prefix(spoken)
    verb = words[0].rsplit("/", 1)[-1] if words else ""
    if verb == "cd" and len(words) > 1 and unknown(words[1]):
        return words[1]
    if verb != "git":
        return None
    for index, word in enumerate(words[1:], start=1):
        if word in ("-C", "--git-dir", "--work-tree") and index + 1 < len(words):
            if unknown(words[index + 1]):
                return words[index + 1]
        elif word.startswith(("--git-dir=", "--work-tree=")) and unknown(word):
            return word
        elif not word.startswith("-") and words[index - 1] not in _YARD_GIT_GLOBAL_VALUE_OPTS:
            break                         # the subcommand: git's own options end
    return None


def _yard_words(segment: str) -> list[str]:
    """Words of one link, with a leading `sudo`/`env`/`time` prefix stripped.

    A prefix would otherwise make every command look like an unknown verb and
    the guard would refuse `env FOO=1 cat <HELM>/x`, which is a read.
    """
    return _yard_strip_prefix(segment.split())


# `2>&1`, `>&2` and `2>/dev/null` are plumbing, not writes. Everything else
# containing `>` sends bytes to a file.
_YARD_REDIRECT_NOISE_RE = re.compile(r"\d?>&\d|\d?>\s*/dev/null")

# A redirection OPERATOR standing alone as a word, its target being the word
# after it: `> out.txt`, `2>> err.log`, `&> all.log`. The operator is never an
# operand of the command; its target may be a real file, and that target is
# deliberately NOT covered here.
_YARD_REDIRECT_OPERATOR_RE = re.compile(r"\d?>>?|&>>?|>&|<")


def _yard_is_redirection(word: str) -> bool:
    """True when this word is shell plumbing rather than an operand.

    ONE predicate, called from `_yard_git_operands` and
    `_yard_deletion_operands`, because both collect "the words that are not
    flags" and a redirection token carries no dash, so both were counting it as
    an operand. MEASURED 2026-09-08 in HELM against the merged hook:
    `herdr worktree remove --help 2>&1` collected `['worktree', 'remove',
    '2>&1']`, three operands where the help exemption needs two, so the
    exemption did not apply and the wall refused a help request. The git
    spelling collected the same token and passed it to `_resolve`, which read a
    redirection target as a candidate deletion PATH: harmless today only
    because no checkout is ever spelled that way.

    `_YARD_REDIRECT_NOISE_RE` is reused rather than restated. It is this file's
    existing statement of what plumbing looks like, and it is anchored here
    with `fullmatch` because there it is substituted out of a whole segment.
    It is NOT sufficient on its own, and the gap is deliberate rather than
    overlooked: it does not match `&>/dev/null`, `2>>/dev/null`, or a redirect
    into a real file (`> out.txt`, `2> err.log`, `>> log`). Every one of those
    stays an operand, so the link is JUDGED instead of waved through. That is
    the safe direction. Under-exempting costs a refusal the operator can work
    around; over-exempting is a bypass, because any word accepted here vanishes
    from the count the help exemption rests on.
    """
    return bool(_YARD_REDIRECT_NOISE_RE.fullmatch(word)
                or _YARD_REDIRECT_OPERATOR_RE.fullmatch(word))


# Read-only verbs that WRITE when given one of these words. HOS-211, MEASURED
# 2026-10-07 on main 1575af33: `sed -i s/a/b/ <HELM>/x`, `find <HELM>/x
# -delete`, `find <HELM> -exec rm {} +` and `sort -o <HELM>/x` were all
# permitted from a yard, because the verb alone made the link a read.
_YARD_WRITING_FORMS = {
    "sed": ("--in-place",),
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir",
             "-fprint", "-fprint0", "-fprintf", "-fls"),
    "sort": ("-o", "--output"),
    "awk": ("inplace", "--include=inplace"),
}


def _yard_verb_writes(verb: str, words: list[str]) -> bool:
    """True when a read-only verb carries a form that writes a file."""
    forms = _YARD_WRITING_FORMS.get(verb, ())
    for word in words[1:]:
        if word.startswith(forms) or word.split("=", 1)[0] in forms:
            return True
        # `sed -i`, `sed -i.bak`, `sed -ni`: the in-place flag in a bundle.
        if verb == "sed" and word.startswith("-") and not word.startswith("--") \
                and "i" in word[1:]:
            return True
        if verb == "sort" and word.startswith("-") and not word.startswith("--") \
                and "o" in word[1:]:
            return True
    return False


def _yard_segment_is_read_only(segment: str) -> bool:
    # A redirection settles it before the verb is even looked at. `echo` is on
    # the read-only list and belongs there, but `echo x > <HELM>/scripts/x.py`
    # writes a file, and reading the verb alone permitted exactly that until a
    # test drove the real command and caught it. The same shape reaches HELM as
    # `cd <HELM> && echo x > y`.
    #
    # Deliberately blunt: `rg "a>b" file` reads as a write and is refused. That
    # is the safe direction, it costs one `cd` to work around from HELM, and a
    # shell-accurate parse here would be a second shell in this file.
    if ">" in _YARD_REDIRECT_NOISE_RE.sub("", segment):
        return False
    words = _yard_words(segment)
    if not words:
        return True
    verb = words[0].rsplit("/", 1)[-1]
    if verb == "git":
        subcommand = _yard_git_subcommand(words)
        if subcommand in ("branch", "worktree"):
            return _yard_git_subcommand_is_listing(subcommand, words)
        return subcommand in _YARD_READ_ONLY_GIT
    return verb in _YARD_READ_ONLY_VERBS and not _yard_verb_writes(verb, words)


def _yard_follow_cd(words: list[str], current: Path) -> Path:
    """Where a `cd` link leaves the working directory.

    An unresolvable `cd` leaves `current` where it was, which is the safe
    direction: the tracked directory stays the last one a guard could actually
    name, so a later segment is still judged against a real path rather than
    against a guess. A variable or a substitution the expansion could not
    resolve is one of those (`_yard_expanded_segments`), and the write guard
    refuses that `cd` outright.

    One function, two callers: `check_yard_write_guard` and
    `check_yard_deletion_guard` both walk a command link by link and both have
    to know where each link would run. The second copy is the one that stops
    being fixed.
    """
    if len(words) > 1 and not words[1].startswith("-"):
        candidate = Path(words[1].strip("'\""))
        candidate = candidate if candidate.is_absolute() else current / candidate
        with contextlib.suppress(OSError):
            return candidate.resolve()
    return current


def _yard_git_subcommand(words: list[str]) -> str:
    """The git verb of one link, or "".

    `git -C <path> log` puts a value between the flag and the verb, so the first
    non-flag word can be the path. The first RECOGNISED verb is taken instead.
    """
    return next((w for w in words[1:]
                 if not w.startswith("-") and w in _YARD_GIT_VERBS), "")


def _yard_git_operands(subcommand: str, words: list[str]) -> list[str]:
    """The non-flag words after `subcommand`, unquoted, redirections dropped."""
    rest = words[words.index(subcommand) + 1:]
    return [w.strip("'\"") for w in rest
            if not w.startswith("-") and not _yard_is_redirection(w)]


def _yard_git_flags(subcommand: str, words: list[str]) -> list[str]:
    """The flags of the SUBCOMMAND, with any `=value` dropped.

    After the subcommand only. Anything before it belongs to `git` itself
    (`-C <path>`, `--no-pager`) and says nothing about whether the subcommand
    lists or mutates; counting those made `git --no-pager branch` look like a
    mutation.
    """
    rest = words[words.index(subcommand) + 1:]
    return [w.split("=", 1)[0] for w in rest if w.startswith("-")]


def _yard_git_subcommand_is_listing(subcommand: str, words: list[str]) -> bool:
    """True for the read-only form of `git branch` / `git worktree`.

    `branch` lists when every flag it carries is a listing flag AND it either
    names nothing or carries a flag that takes a pattern or a commit
    (`--list 'feat/*'`, `--contains HEAD`). A bare `git branch <name>` CREATES a
    branch in the shared refs, which is not a read.

    `worktree` lists only as `git worktree list`. Everything else it can do —
    add, remove, prune, move, lock, repair — edits the one registry directory
    that every checkout of this repository shares.
    """
    if subcommand == "worktree":
        return _yard_git_operands(subcommand, words)[:1] == ["list"]
    flags = _yard_git_flags(subcommand, words)
    if any(flag not in _YARD_BRANCH_LISTING_FLAGS for flag in flags):
        return False
    operands = _yard_git_operands(subcommand, words)
    return not operands or bool(flags)


def _yard_data_roots(helm: Path) -> list[Path]:
    """Every path that names the shared data overlay, as a caller might spell it.

    Two candidates rather than one. `get_data_root()` is the authority, but in a
    YARD with no `.env` it answers `<yard>/examples` (demo mode, MEASURED
    2026-09-03), which names nothing a `git -C` would be pointed at. The sibling
    of HELM is the path an operator or an agent actually types.
    """
    roots: list[Path] = []
    sibling = helm.parent / ".heading-os-data"
    if sibling.is_dir():
        roots.append(sibling.resolve())
    try:
        from scripts.utils.paths import data_root_is_demo, get_data_root
        if not data_root_is_demo():
            resolved = get_data_root().resolve()
            if resolved not in roots:
                roots.append(resolved)
    except Exception as exc:  # noqa: BLE001
        # Logged, never swallowed. The sibling candidate still stands, and the
        # dispatcher's crash handling covers a genuinely broken import.
        print(f"[_dispatch:yard] data root unresolvable ({type(exc).__name__}: "
              f"{exc}); falling back to the sibling candidate only",
              file=sys.stderr)
    return roots


def _yard_data_git_dirs(roots: list[Path]) -> list[Path]:
    """Every path that is, or holds, the git directory of a data root.

    HOS-199. MEASURED 2026-10-07 by HELM: from a yard the write guard allowed a
    Write to `<overlay>/.git/hooks/pre-commit`, an Edit of `<overlay>/.git/config`,
    and `cp`, `tee` and `echo >` into the hooks directory. A hook planted there
    runs in HELM on the next overlay commit, so a yard reaches HELM through it.

    `<root>/.git` itself is always one, file or directory, because rewriting a
    `gitdir:` pointer is the same reach. When it IS such a pointer, the
    directory it names is the other. An unreadable pointer is logged and the
    `.git` path alone still stands.
    """
    dirs: list[Path] = []
    for root in roots:
        dot = root / ".git"
        candidates = [dot]
        try:
            if dot.is_file():
                pointer = dot.read_text(encoding="utf-8").strip()
                if pointer.startswith("gitdir:"):
                    named = Path(pointer.split(":", 1)[1].strip())
                    candidates.append(named if named.is_absolute() else root / named)
        except OSError as exc:
            print(f"[_dispatch:yard] gitdir pointer of {root} unreadable "
                  f"({type(exc).__name__}: {exc}); guarding {dot} alone",
                  file=sys.stderr)
        for candidate in candidates:
            with contextlib.suppress(OSError):
                candidate = candidate.resolve()
            if candidate not in dirs:
                dirs.append(candidate)
    return dirs


# Options whose value is a PATTERN a tool matches paths against, never a path it
# opens: `find -path`, `find -name`, `grep --exclude-dir`. Their value is how a
# command stays OUT of a directory, so it is not read as naming one.
_YARD_PATTERN_OPTS = frozenset((
    "-path", "-ipath", "-wholename", "-iwholename", "-name", "-iname",
    "--exclude-dir", "--exclude",
))


def _yard_without_patterns(segment: str) -> str:
    """`segment` with the values of `_YARD_PATTERN_OPTS` taken out.

    MEASURED 2026-10-07 over the HOS-196 corpus: without this, 76 historical
    commands were refused, nearly all `grep --exclude-dir=.git`,
    `find . -path ./.git -prune` or `-not -path "./.git/*"` run in the data
    repository, each one keeping itself out of the directory this guards.
    """
    words = segment.split()
    kept: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word in _YARD_PATTERN_OPTS:
            index += 2
            continue
        if word.split("=", 1)[0] in _YARD_PATTERN_OPTS and "=" in word:
            index += 1
            continue
        kept.append(word)
        index += 1
    return " ".join(kept)


def _yard_segment_reaches(segment: str, current: Path, dirs: list[Path]) -> bool:
    """True when a link names, or runs inside, one of `dirs`.

    Absolute spellings are found in the text; every word is also read as a
    path, relative ones against where the link runs, so `cd <root> && echo x
    >.git/hooks/x` and `<root>/./.git` are caught. Reads are not told apart
    from writes: nothing in a yard has business in those directories. Only a
    pattern that keeps a command out of them is set aside first.
    """
    segment = _yard_without_patterns(segment)
    if any(_yard_segment_names(segment, d) or _yard_is_under(current, d) for d in dirs):
        return True
    for word in re.split(r"[\s<>|&;=]+", segment):
        word = word.strip("'\"")
        if not word or word.startswith("-"):
            continue
        path = Path(os.path.normpath(word if word.startswith("/") else current / word))
        if any(_yard_is_under(path, d) for d in dirs):
            return True
    return False


def _yard_git_dir_reason(where: str) -> str:
    return (
        f"YARD write guard — intentional policy block, not an error. This "
        f"reaches the data repository's GIT directory from a YARD:\n\n  {where}\n\n"
        f"A hook or a config line planted there runs in HELM on the next commit "
        f"of the data repository, so it is a write into HELM by another road. "
        f"Files in the data repository are yours to write; its git directory "
        f"is not, not even to read. Ask HELM."
    )


# HOS-211. The places outside every checkout whose CONTENT decides what runs on
# this machine, HELM's sessions and daemons included. MEASURED 2026-10-07 by
# HELM on main 1575af33: from a yard the write guard permitted a Write or an
# Edit of `~/.claude/settings.json`, a Write of `~/.claude/hooks/<x>.py` and of
# `~/.config/systemd/user/<x>.service`, an Edit of `~/.bashrc`, and `echo >>`,
# `cp` and `echo >` into the same three. A yard writes its own tree, the data
# repository's files and the caches its tools keep; none of these.
#
# Relative to the home directory, each with the reason it is here:
_YARD_MACHINE_HOME_PLACES = (
    # (a) Claude Code's own: settings.json (the hooks every session runs, HELM's
    # included), hooks/, skills/, plugins/, agents/, CLAUDE.md, output styles,
    # the status line script; and `.claude.json`, which registers the MCP
    # servers every session starts. The harness writes transcripts and session
    # state there itself, which no tool call carries, so no PreToolUse wall
    # sees it and none is needed.
    ".claude", ".claude.json",
    # (b) Every directory `systemd --user` reads a unit or a drop-in from that
    # this user can write, and `environment.d`, which sets the environment of
    # every user unit. `/run/user/<uid>/systemd` is added beside these.
    ".config/systemd", ".local/share/systemd", ".config/environment.d",
    # (c) Shell and login startup files: each runs in every new shell, HELM's
    # panes and every daemon started from a login shell. `.bash_aliases` is
    # sourced by `.bashrc` on this machine; `.tmux.conf` can `run-shell`.
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".bash_aliases",
    ".profile", ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".zlogout",
    ".config/fish", ".pam_environment", ".tmux.conf",
    # (d) The same kind, found on 2026-10-07:
    # git's global configuration: `core.hooksPath`, aliases and filters run in
    # every repository, HELM's included.
    ".gitconfig", ".config/git",
    # herdr, the multiplexer HELM and every yard run inside: its config decides
    # what a pane launches.
    ".config/herdr",
    # Hindsight's coding-agent plugin and its client config, loaded by every
    # session's hooks.
    ".hindsight", ".config/hindsight",
    # Configuration a running service starts from, read from its unit's
    # ExecStart: lific (`.config/lific/lific.toml`) and CLIProxyAPI
    # (`cliproxyapi/config.yaml` and its binary). And the offsite timer's key.
    ".config/lific", "cliproxyapi", ".cli-proxy-api", ".config/heading-os",
    # The head of PATH: a file here runs in place of `claude`, `herdr`, `lific`
    # or `git-lfs` for every session; and the user site-packages, where a
    # `.pth` file runs at every Python start.
    ".local/bin", "bin", ".cargo/bin", ".nvm", ".local/lib",
    # ssh's config runs a `ProxyCommand` on every connection, git's included,
    # and `authorized_keys` admits a login.
    ".ssh",
    # gh aliases can run a shell; uv.toml can repoint the package index HELM's
    # `uv sync` installs from.
    ".config/gh", ".config/uv",
)

# Inside the places above, the one a yard is TOLD to write: in plan mode Claude
# Code asks the model to write its plan file under `<config dir>/plans/` (read
# from the 2.1.292 binary on 2026-10-07: the default plans directory is the
# config directory joined with "plans"). A plan is markdown the harness reads
# back to the operator; it configures nothing.
_YARD_MACHINE_EXEMPT = (".claude/plans",)

# And the scratch directory the harness hands a session as `$CLAUDE_JOB_DIR`,
# `<config dir>/jobs/<id>/tmp`. MEASURED 2026-10-07 over the HOS-196 corpus, in
# a run stopped before its end: 139 historical yard commands wrote there (test
# output), and nothing reads it back to decide what runs.
_YARD_MACHINE_JOB_DIRS = ".claude/jobs"


def _yard_is_job_scratch(path: Path, home: Path) -> bool:
    try:
        parts = path.relative_to(home / _YARD_MACHINE_JOB_DIRS).parts
    except ValueError:
        return False
    return len(parts) >= 2 and parts[1] == "tmp"


# Verbs whose operands are files they create, change or remove. The home rule
# judges a link written with one of these, a read-only verb in a writing form
# (`_YARD_WRITING_FORMS`) and a mutating `git`; any other verb is not judged by
# it. MEASURED 2026-10-07 over the HOS-196 corpus with "anything not on the
# read-only list" instead, in a run stopped before the end of the corpus: 226
# historical commands newly refused, 139 of them writes to `$CLAUDE_JOB_DIR`
# (exempted above) and 87 others, among them reads such as `du`, `rg` inside
# `time (...)`, `f=~/.claude/...; python3 - "$f"` and `grep | python3 -c` run
# in `~/.claude/projects`. What this leaves unjudged is a program that writes by
# itself (`python3 -c "open(...,'w')"`, a script, an installer), which no text
# check sees, as this hook's own deletion-guard notes say of every wall here.
_YARD_FILE_WRITING_VERBS = frozenset((
    "cp", "mv", "ln", "install", "tee", "touch", "mkdir", "rm", "rmdir",
    "unlink", "shred", "rsync", "dd", "truncate", "chmod", "chown", "chgrp",
    "patch", "curl", "wget", "tar", "unzip", "cpio", "ed", "ex", "vi", "vim",
    "nano", "perl", "sponge", "git",
))

# Of those, the ones that READ every operand but the last.
_YARD_COPY_VERBS = frozenset(("cp", "ln", "rsync", "install"))


def _yard_homes() -> list[Path]:
    """`$HOME` and the password database's home, which differ only when one lies."""
    homes: list[Path] = []
    candidates = [os.environ.get("HOME", "")]
    try:
        import pwd
        candidates.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError) as exc:
        print(f"[_dispatch:yard] home from the password database unreadable "
              f"({type(exc).__name__}: {exc}); using $HOME alone", file=sys.stderr)
    for raw in candidates:
        if raw and os.path.isabs(raw):
            home = Path(os.path.normpath(raw))
            if home not in homes:
                homes.append(home)
    return homes


def _yard_machine_places() -> tuple[list[Path], list[Path]]:
    """(guarded, exempt): every spelling of the places above, raw and resolved."""
    guarded: list[Path] = [Path("/etc")]      # root-owned; reached through `sudo`
    exempt: list[Path] = []
    for env_name in ("CLAUDE_CONFIG_DIR", "XDG_RUNTIME_DIR"):
        raw = os.environ.get(env_name, "")
        if raw and os.path.isabs(raw):
            guarded.append(Path(raw) / "systemd" if env_name == "XDG_RUNTIME_DIR"
                           else Path(raw))
    guarded.append(Path(f"/run/user/{os.getuid()}/systemd"))
    for home in _yard_homes():
        guarded.extend(home / rel for rel in _YARD_MACHINE_HOME_PLACES)
        exempt.extend(home / rel for rel in _YARD_MACHINE_EXEMPT)
    for listed in (guarded, exempt):
        for path in list(listed):
            with contextlib.suppress(OSError):
                real = path.resolve()
                if real not in listed:
                    listed.append(real)
    return guarded, exempt


def _yard_machine_place(path: Path, places: tuple[list[Path], list[Path]]) -> Path | None:
    """The guarded place `path` lies in, or None (outside all, or exempt)."""
    guarded, exempt = places
    if any(_yard_is_under(path, e) for e in exempt) \
            or any(_yard_is_job_scratch(path, home) for home in _yard_homes()):
        return None
    return next((g for g in guarded if _yard_is_under(path, g)), None)


def _yard_word_path(word: str, current: Path) -> Path | None:
    """A shell word read as a path: `~` expanded, relative to `current`."""
    word = word.strip("'\"")
    if not word or word.startswith("-") or "$" in word:
        return None
    if word.startswith("~"):
        word = os.path.expanduser(word)
        if word.startswith("~"):
            return None
    return Path(os.path.normpath(word if word.startswith("/") else current / word))


def _yard_redirection_targets(text: str) -> list[str]:
    """The target word of every unquoted file redirection in `text`.

    The same reading `_yard_without_redirections` takes out, kept instead.
    Quoting that does not close yields nothing here, and the caller's
    not-read-only branch judges every word of such a link anyway.
    """
    try:
        mask = _yard_quoted_positions(text)
    except ValueError:
        return []
    targets: list[str] = []
    index = 0
    while index < len(text):
        match = None if mask[index] else _YARD_FILE_REDIRECT_RE.match(text, index)
        if match is None:
            index += 1
            continue
        start = index = match.end()
        while index < len(text) and (mask[index] or not text[index].isspace()):
            index += 1
        # An input redirection reads its file. MEASURED 2026-10-07 in the
        # HOS-196 replay: `if ts < '...'` in a heredoc body was refused as one.
        if match.group(0).strip().lstrip("0123456789") != "<":
            targets.append(text[start:index])
    return targets


def _yard_segment_machine_place(segment: str, current: Path,
                                places: tuple[list[Path], list[Path]]) -> Path | None:
    """The guarded place a link WRITES, or None.

    A redirection into one is a write whatever the verb. Otherwise a link that
    writes files (`_YARD_FILE_WRITING_VERBS`, a read-only verb in a writing
    form, a `git` that is not a read; judged without its redirections, so
    `cat ~/.bashrc > <scratch>/x` stays a read) is refused when it runs inside
    a guarded place or names one, absolutely, through `~`, or relative to
    where it runs. Reads stay open, and so does every other verb: see
    `_YARD_FILE_WRITING_VERBS` for what that leaves unjudged.
    """
    for target in _yard_redirection_targets(segment):
        # `2>&1` and `>&-` duplicate or close a descriptor; no file is named.
        if target.startswith("&") or target.isdigit() or target == "-" \
                or target in ("/dev/null", "/dev/stderr", "/dev/stdout"):
            continue
        path = _yard_word_path(target, current)
        where = None if path is None else _yard_machine_place(path, places)
        if where is not None:
            return where
    bare = _yard_without_redirections(segment)
    words = _yard_words(bare)
    verb = words[0].rsplit("/", 1)[-1] if words else ""
    writes = (_yard_verb_writes(verb, words) if verb in _YARD_READ_ONLY_VERBS
              else not _yard_segment_is_read_only(bare) if verb == "git"
              else verb in _YARD_FILE_WRITING_VERBS)
    if not writes:
        return None
    where = _yard_machine_place(current, places)
    if where is not None:
        return where
    judged = " ".join(words[1:])          # the verb is not a path it writes
    if verb in _YARD_COPY_VERBS:
        # Only the destination is written. MEASURED 2026-10-07 in the HOS-196
        # replay: `cp ~/.hindsight/<log> <overlay>/...` was refused for its
        # SOURCE. `-t <dir>` / `--target-directory=<dir>` names it explicitly.
        operands = [w for w in words[1:] if not w.startswith("-")]
        target = next((words[i + 1] for i, w in enumerate(words[:-1])
                       if w in ("-t", "--target-directory")), None) or next(
            (w.split("=", 1)[1] for w in words if w.startswith("--target-directory=")),
            None)
        judged = target or (operands[-1] if operands else "")
    for word in re.split(r"[\s<>|&;=]+", judged):
        path = _yard_word_path(word, current)
        if path is not None:
            where = _yard_machine_place(path, places)
            if where is not None:
                return where
    return None


def _yard_machine_reason(where: str, place: Path) -> str:
    return (
        f"YARD write guard — intentional policy block, not an error. This "
        f"writes a place outside every checkout whose content decides what "
        f"runs on this machine, HELM included:\n\n  {where}\n\n(under {place})\n\n"
        f"Shell startup files, systemd units, Claude Code's settings and hooks, "
        f"git's global config and the head of PATH are HELM's to change, with "
        f"the operator's approval. Reading them from here is fine. Write what "
        f"HELM should change into your closing note."
    )


# Commands that change what runs without naming any file. Each is a read only
# in the forms listed; anything else, a verb added next year included, is
# refused. MEASURED 2026-10-07 on main 1575af33: `systemctl --user stop
# lific.service`, `crontab -r`, `git config core.hooksPath /dev/null` and
# `git config --global alias.st status` were all permitted from a yard.
_YARD_SYSTEMCTL_READS = frozenset((
    "status", "show", "cat", "list-units", "list-unit-files", "list-timers",
    "list-sockets", "list-dependencies", "list-jobs", "list-paths",
    "is-active", "is-enabled", "is-failed", "is-system-running",
    "show-environment", "get-default", "help",
))
_YARD_LOGINCTL_READS = frozenset((
    "list-sessions", "list-users", "list-seats", "session-status", "show-session",
    "user-status", "show-user", "seat-status", "show-seat", "help",
))
# Options of the two whose value is the NEXT word, never the subcommand.
_YARD_CTL_VALUE_OPTS = frozenset((
    "-n", "--lines", "-t", "--type", "-p", "--property", "--state", "-H",
    "--host", "-M", "--machine", "-o", "--output", "-s", "--signal",
    "--kill-whom", "--root", "--job-mode", "--what", "--when", "--timestamp",
))
_YARD_GIT_CONFIG_READ_FLAGS = frozenset((
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color",
    "--get-colorbool", "-l", "--list",
))
_YARD_GIT_CONFIG_MODIFIERS = frozenset((
    "--global", "--system", "--local", "--worktree", "--file", "-f", "--blob",
    "--show-origin", "--show-scope", "--name-only", "-z", "--null", "--type",
    "--default", "--includes", "--no-includes", "--bool", "--int", "--path",
    "--bool-or-int", "--expiry-date",
))


def _yard_ctl_subcommand(words: list[str]) -> str:
    """The first word after the verb that is neither an option nor its value."""
    index = 1
    while index < len(words):
        word = words[index]
        if word in _YARD_CTL_VALUE_OPTS:
            index += 2
            continue
        if not word.startswith("-"):
            return word
        index += 1
    return ""


def _yard_git_config_writes(words: list[str]) -> bool | None:
    """For `git ... config ...`: True if it writes, False if it reads, None if
    the link is not `git config` at all. A read is `get`/`list`, a read flag, or
    a lone key, with nothing outside the known modifiers."""
    verb_at = None
    for index in range(1, len(words)):
        if words[index].startswith("-") or words[index - 1] in _YARD_GIT_GLOBAL_VALUE_OPTS:
            continue
        verb_at = index
        break
    if verb_at is None or words[verb_at] != "config":
        return None
    rest = [w.strip("'\"") for w in words[verb_at + 1:] if not _yard_is_redirection(w)]
    flags = [w.split("=", 1)[0] for w in rest if w.startswith("-")]
    operands = [w for w in rest if not w.startswith("-")]
    if operands[:1] in (["get"], ["list"]):
        return False
    if any(f not in _YARD_GIT_CONFIG_READ_FLAGS | _YARD_GIT_CONFIG_MODIFIERS for f in flags):
        return True
    return not (any(f in _YARD_GIT_CONFIG_READ_FLAGS for f in flags) or len(operands) <= 1)


def _yard_runs_something_refusal(verb: str, words: list[str], current: Path,
                                 shared: list[Path]) -> str | None:
    """The reason a command-shaped change of what runs is refused, or None.

    `git config` is refused when it writes the global or system file, or the
    configuration of a repository whose config is SHARED with HELM (this
    checkout, HELM, a neighbour: every worktree reads `<HELM>/.git/config`). A
    throwaway repository elsewhere keeps its own config and stays writable.
    """
    lead = ("YARD write guard — intentional policy block, not an error. "
            "From a YARD, nothing may change what runs on this machine.\n\n  ")
    if verb == "systemctl" and _yard_ctl_subcommand(words) not in ("", *_YARD_SYSTEMCTL_READS):
        return (f"{lead}{' '.join(words)[:300]}\n\nEvery daemon lifecycle "
                f"action happens in HELM. `systemctl --user status|show|cat|"
                f"list-*|is-*` read and are permitted.")
    if verb == "loginctl" and _yard_ctl_subcommand(words) not in ("", *_YARD_LOGINCTL_READS):
        return (f"{lead}{' '.join(words)[:300]}\n\nLingering and sessions "
                f"decide whether user units run; HELM's.")
    if verb == "crontab":
        operands = [w for i, w in enumerate(words[1:], start=1)
                    if not w.startswith("-") and words[i - 1] != "-u"]
        flags = {w for w in words[1:] if w.startswith("-")}
        if operands or "-l" not in flags or not flags <= {"-l", "-u"}:
            return (f"{lead}{' '.join(words)[:300]}\n\n`crontab -l` reads and is "
                    f"permitted; nothing in this workspace is scheduled by cron.")
    if verb == "git" and _yard_git_config_writes(words):
        run_in = current
        for index, word in enumerate(words[1:-1], start=1):
            if word == "-C":
                path = _yard_word_path(words[index + 1], run_in)
                run_in = path if path is not None else run_in
        global_scope = any(w in ("--global", "--system") for w in words)
        if global_scope or any(_yard_is_under(run_in, root) for root in shared):
            return (f"{lead}{' '.join(words)[:300]}\n\nThis writes git "
                    f"configuration that HELM reads: the global or system file, "
                    f"or `<HELM>/.git/config`, which every worktree of this "
                    f"repository shares (one `core.hooksPath` there disarms "
                    f"HELM's commit gates). Reading it is permitted.")
    return None


def _yard_is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _yard_mine() -> Path:
    """This checkout's root. `WORKSPACE` is this file's own tree, so in a YARD
    it is the YARD, with no environment variable in the chain."""
    try:
        return WORKSPACE.resolve()
    except OSError:
        return WORKSPACE


def _yard_registry(helm: Path) -> Path:
    return helm / ".git" / "worktrees"


def _yard_foreign_roots(helm: Path) -> list[Path]:
    """Every checkout of this repository that is neither mine nor HELM.

    Read from the registry git keeps for itself:
    `<HELM>/.git/worktrees/<name>/gitdir` holds the path of that worktree's
    `.git` FILE, so its parent is that worktree's root. A directory listing and
    a handful of small reads, no subprocess, because this runs on every tool
    call.

    Not `git worktree list`: that is a subprocess on the hot path, and it
    answers from the same file this reads.
    """
    mine = _yard_mine()
    roots: list[Path] = []
    try:
        entries = sorted(_yard_registry(helm).iterdir())
    except OSError:
        return roots                       # no registry: nothing to enumerate
    for entry in entries:
        try:
            pointer = (entry / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not pointer:
            continue
        root = Path(pointer).parent
        with contextlib.suppress(OSError):
            root = root.resolve()
        if root != mine and root != helm and root not in roots:
            roots.append(root)
    return roots


def _yard_foreign_checkout_containing(path: Path, helm: Path) -> Path | None:
    """The root of ANOTHER checkout of this repository that contains `path`.

    A second, independent signal, and it is here because the registry above can
    itself be the casualty: on 2026-09-03 a test fixture's `shutil.rmtree`
    emptied `<HELM>/.git/worktrees` and every live YARD lost its entry. A
    worktree whose registration is gone still has its `.git` FILE, and that file
    still points into this repository's registry directory, so it is still
    recognisable as a checkout of this repository.

    Walks up from `path`, which need not exist — a Write names a file that is
    about to be created. Stops at `mine` and at HELM, which the callers answer
    for themselves.
    """
    mine = _yard_mine()
    prefix = str(_yard_registry(helm))
    for candidate in (path, *path.parents):
        if candidate in (mine, helm):
            return None
        dot_git = candidate / ".git"
        try:
            if not dot_git.is_file():
                continue
            pointer = dot_git.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if pointer.startswith("gitdir:"):
            pointer = pointer.split(":", 1)[1].strip()
        if pointer.startswith(prefix):
            return candidate
    return None


# Verbs that destroy or move what they are pointed at. Consulted for ONE
# question, below: is a bare `/` in this command the argument of something that
# would destroy every worktree on the machine? A verb missing from this set only
# returns that command to the state it was in before `_yard_word_is_root`
# existed, so a gap here is not a new hole.
_YARD_DESTRUCTIVE_VERBS = frozenset((
    "rm", "rmdir", "unlink", "shred", "mv", "dd", "truncate", "mkfs",
    "chmod", "chown", "chgrp",
))


def _yard_word_is_root(word: str) -> bool:
    """True when a shell word is the filesystem root itself.

    `/`, `//`, `'/'`. Asked of the SHAPE and never by resolving: this runs on
    every tool call, and the answer must not depend on the filesystem.
    """
    stripped = word.strip("'\"")
    return bool(stripped) and set(stripped) == {"/"}


def _yard_root_destruction(verb: str, words: list[str]) -> str | None:
    """A destructive verb aimed at `/`, which is a different question.

    Separate from the containing-directory check because the two answers differ
    in kind. `rm -rf /` does not REACH another task's worktree; it destroys the
    machine, and a refusal saying "you touched a neighbour" misnames it. This is
    where that command keeps its refusal when `/` stops counting as evidence of
    reaching a neighbour.
    """
    if verb not in _YARD_DESTRUCTIVE_VERBS:
        return None
    if not any(_yard_word_is_root(word) for word in words[1:]):
        return None
    # The wording opens with the same words as every other refusal from this
    # wall, so a reader can tell WHICH wall said no.
    return (
        f"YARD isolation guard — intentional policy block, not an error. This "
        f"command points `{verb}` at the filesystem root, which holds HELM, the "
        f"data overlay and every task's worktree.\n\n"
        f"Nothing in a YARD needs that. Name the directory you mean."
    )


def _yard_foreign_reason(where: Path) -> str:
    return (
        f"YARD isolation guard — intentional policy block, not an error. "
        f"{where} is ANOTHER YARD: a different task's worktree of this same "
        f"repository.\n\n"
        f"A YARD sees three things and no more: its own checkout, HELM (read "
        f"only), and the data overlay (files yes, git no). A neighbour is none "
        f"of them, so nothing here is permitted against it — not a read, not a "
        f"write, not a git operation naming its path. Its branch is unmerged, "
        f"its working tree is somebody's half-finished change, and a session is "
        f"very likely standing in it right now.\n\n"
        f"If you need what is in it, ask for it in HELM, which sees every "
        f"branch of this repository already."
    )


def _yard_shared_git_refusal(words: list[str], own_branch: str | None) -> str | None:
    """Refuse the git operations that edit the ONE directory every checkout shares.

    These need no path to do their damage, so no path check can catch them.
    `git branch -D` and `git worktree prune|remove|add` run from inside a YARD
    reach across the whole repository: the refs and the worktree registry live
    in HELM's `.git`, and every worktree is a client of it. MEASURED 2026-09-04
    from a live YARD, before this existed: all four were permitted.

    Your own branch and your own worktree are the exception, because those are
    yours to edit.
    """
    subcommand = _yard_git_subcommand(words)
    if subcommand not in ("branch", "worktree"):
        return None
    if _yard_git_subcommand_is_listing(subcommand, words):
        return None
    operands = _yard_git_operands(subcommand, words)

    if subcommand == "branch":
        flags = _yard_git_flags(subcommand, words)
        # A move or copy with ONE operand names the new name and renames the
        # branch you are standing on. Its bound, stated rather than left to be
        # found: `git branch -m <old> <new>` carries two operands and is refused
        # even when `<old>` is your own, because the guard would then have to
        # decide which operand is the source.
        if len(operands) == 1 and any(f in _YARD_BRANCH_MOVE_FLAGS for f in flags):
            return None
        if operands and own_branch and all(op == own_branch for op in operands):
            return None
        return (
            "YARD isolation guard — intentional policy block, not an error. "
            "This edits a branch of the SHARED repository from a YARD.\n\n"
            "Every worktree of this repository reads and writes ONE set of "
            "refs, kept in HELM's `.git`. A branch you delete here is deleted "
            "for the session standing in it, and a task branch that has not "
            "been merged is not recoverable from a worktree that no longer has "
            "it checked out.\n\n"
            "Your own branch is yours. Everybody else's is HELM's, and HELM is "
            "where branches are reviewed and removed."
        )

    return (
        "YARD isolation guard — intentional policy block, not an error. "
        "This edits the worktree REGISTRY, which every checkout of this "
        "repository shares.\n\n"
        "`<HELM>/.git/worktrees` is one directory holding one entry per live "
        "worktree. `prune` reaches every entry in it, including those of "
        "sessions running right now; `remove` and `add` change which checkouts "
        "exist at all. On 2026-09-03 that directory was emptied and every live "
        "YARD lost the ability to run a single git command until the operator "
        "rebuilt the entries by hand.\n\n"
        "`git worktree list` reads it and is permitted. Creating and removing "
        "worktrees is HELM's, which is the one checkout that can see them all."
    )


def _yard_own_branch() -> str | None:
    """The branch THIS worktree has checked out, or None (detached, or unknown).

    Read from this worktree's own `HEAD`, reached through the `gitdir:` pointer
    in its `.git` file. No subprocess. None is the safe answer: it makes the
    own-branch exception above match nothing, so a branch operation is refused
    rather than permitted on a guess.
    """
    try:
        pointer = (WORKSPACE / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir:"):
        return None
    try:
        head = (Path(pointer.split(":", 1)[1].strip()) / "HEAD").read_text(
            encoding="utf-8").strip()
    except OSError:
        return None
    marker = "ref: refs/heads/"
    return head[len(marker):] if head.startswith(marker) else None


def _yard_segment_names(segment: str, root: Path) -> bool:
    """True when the command text names `root` or something inside it.

    A plain `str(root) in segment` is WRONG here and the test caught it:
    HELM is `.../.heading-os` and the data overlay is `.../.heading-os-data`,
    so every `git -C <data-root> commit` matched the HELM branch as a string
    prefix and was refused with the wrong reason. Any sibling named
    `.heading-os-data-<suffix>` has the same shape, and a path that merely
    starts with the same characters is not a path inside it.

    A boundary is therefore required after the root: a separator, whitespace,
    a quote, or the end of the segment. `Path.relative_to` cannot be used
    because this is command TEXT, not a resolved path.
    """
    return re.search(
        re.escape(str(root)) + r"""(?=/|\s|['"]|$)""", segment) is not None


# git's own options that take their value as the NEXT word, before the
# subcommand. Their value is never the subcommand.
_YARD_GIT_GLOBAL_VALUE_OPTS = frozenset((
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env",
    "--super-prefix", "--exec-path",
))

# `git commit` options whose value is the NEXT word. That word is kept whatever
# it looks like, so `git commit -m -F <path>` does not read `-F` as the flag
# that owns `<path>`, and `<path>` stays a pathspec the guard can see.
_YARD_COMMIT_VALUE_OPTS = frozenset((
    "-m", "-c", "-C", "-t", "--message", "--reuse-message", "--reedit-message",
    "--template", "--author", "--date", "--fixup", "--squash", "--cleanup",
    "--trailer", "--pathspec-from-file",
))


def _yard_without_commit_message_file(words: list[str]) -> str:
    """The link's text with the message file of `git commit -F` taken out.

    HOS-129. MEASURED 2026-09-25 from a yard: `git commit -F <file>` was refused
    whenever `<file>` sat under the data overlay, because the overlay test asked
    whether the link's TEXT named the overlay, and a message file is text too.
    git only READS that file; the commit still lands in this checkout's own
    repository. What the rule protects is the overlay's index, which git reaches
    through the working directory, `-C`, `--git-dir`, `--work-tree` or a
    pathspec, and every one of those is still in the text this returns.

    Four spellings are removed, and only after the `commit` verb: `-F <path>`,
    `-F<path>`, `--file <path>`, `--file=<path>`. Anything else keeps its words,
    which is the refusing direction: `-aF <path>` (a bundle), an abbreviated
    `--fil`, and every word after `--`.
    """
    verb_at = None
    for index in range(1, len(words)):
        word = words[index]
        if word.startswith("-") or words[index - 1] in _YARD_GIT_GLOBAL_VALUE_OPTS:
            continue
        verb_at = index
        break
    if verb_at is None or words[verb_at] != "commit":
        return " ".join(words)
    kept = words[:verb_at + 1]
    rest = words[verb_at + 1:]
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "--":
            kept.extend(rest[index:])
            break
        if word in _YARD_COMMIT_VALUE_OPTS:
            kept.extend(rest[index:index + 2])
            index += 2
            continue
        if word in ("-F", "--file"):
            index += 2
            continue
        if word.startswith("--file=") or (word.startswith("-F") and len(word) > 2):
            index += 1
            continue
        kept.append(word)
        index += 1
    return " ".join(kept)


# An unquoted file redirection: an optional fd or `&` standing at the start of
# a word, the operator, optional blanks, then the target word. The operator may
# be glued to the word before it (`HEAD:x>f`), as the shell reads it. `<<` and
# `<<<` are left alone (a heredoc or a here-string is not a file), and so is
# `<(` / `>(`, a process substitution.
_YARD_FILE_REDIRECT_RE = re.compile(
    r"(?:(?:(?<=\s)|^)(?:\d+|&))?(?:>>|>\||>&|<>|>|(?<!<)<(?![<(]))(?!\()[ \t]*")


def _yard_without_redirections(text: str) -> str:
    """`text` with every unquoted file redirection and its target taken out.

    HOS-199. The overlay test asked whether a git link's TEXT named the
    overlay, and a redirection target is text too: `git show HEAD:x >
    <overlay>/f` was refused, although it is the SHELL that writes git's
    stdout there and git never opens the overlay's index. MEASURED 2026-10-04
    over 49,215 historical yard commands: 99 of the 237 that HOS-196 newly
    refused were this shape. What reaches the index is the working directory,
    `-C`, `--git-dir`, `--work-tree`, `GIT_DIR=` and a pathspec, and none of
    those is a redirection, so every one stays in what this returns.

    Quoting is honoured: a `>` inside quotes is an argument and is kept, and
    text whose quoting does not close is returned unchanged, which is the
    refusing direction. A quoted target is removed up to the closing quote.
    """
    try:
        mask = _yard_quoted_positions(text)
    except ValueError:
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        match = None if mask[index] else _YARD_FILE_REDIRECT_RE.match(text, index)
        if match is None:
            out.append(text[index])
            index += 1
            continue
        index = match.end()
        while index < len(text) and (mask[index] or not text[index].isspace()):
            index += 1
        out.append(" ")
    return "".join(out)


def _yard_deny(reason: str) -> dict:
    return {"decision": "block", "reason": reason, "_policy_deny": True}


_YARD_WRITING_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# Reading tools reach this dispatcher too, and until 2026-09-04 the wall waved
# them through on the grounds that a read cannot damage anything. That is true
# of HELM and of the data overlay, and it is the whole point of allowing both.
# It is not true of a neighbouring YARD, which is a different task's unmerged
# work: the model is that a YARD does not see one at all. So these arrive, and
# exactly one question is asked of them.
_YARD_READING_TOOLS = ("Read", "Grep", "Glob")


def check_yard_write_guard(payload: dict) -> dict | None:
    tool_name = payload.get("tool_name", "")
    # Named rather than inferred, because "the matcher will not send it" is an
    # assumption about a settings file and this file has been burned by one
    # before.
    if tool_name not in (*_YARD_WRITING_TOOLS, *_YARD_READING_TOOLS, "Bash"):
        return None

    try:
        if not _yard_in_a_yard():
            return None                       # HELM. This wall stays silent.
    except Exception as exc:  # noqa: BLE001
        return _yard_deny(
            f"YARD write guard: could not establish whether this checkout is "
            f"the main clone or a worktree ({type(exc).__name__}: {exc}). "
            f"Refusing in the safe direction."
        )

    from scripts.utils.clone_guard import CloneGuardError, main_clone_path
    try:
        helm = main_clone_path(WORKSPACE)
    except CloneGuardError as exc:
        return _yard_deny(
            f"YARD write guard: this is a worktree, but the main clone could "
            f"not be resolved ({exc}). Refusing in the safe direction."
        )

    cwd = Path(payload.get("cwd") or WORKSPACE)
    foreign_roots = _yard_foreign_roots(helm)

    def _foreign(target: Path) -> Path | None:
        """The neighbouring YARD `target` belongs to, by either signal."""
        for root in foreign_roots:
            if _yard_is_under(target, root):
                return root
        return _yard_foreign_checkout_containing(target, helm)

    if tool_name in _YARD_READING_TOOLS:
        raw = (payload.get("tool_input", {}) or {}).get("file_path") \
            or (payload.get("tool_input", {}) or {}).get("path") \
            or (payload.get("tool_input", {}) or {}).get("notebook_path") or ""
        if not raw:
            return None      # no target named, so it reads this checkout
        target = Path(raw)
        target = target.resolve() if target.is_absolute() else (cwd / target).resolve()
        where = _foreign(target)
        return _yard_deny(_yard_foreign_reason(where)) if where else None

    if tool_name in _YARD_WRITING_TOOLS:
        raw = (payload.get("tool_input", {}) or {}).get("file_path") \
            or (payload.get("tool_input", {}) or {}).get("notebook_path") or ""
        if not raw:
            return _yard_deny(
                "YARD write guard: this edit carries no destination path, so "
                "where it would land cannot be established. Refusing.")
        target = Path(raw)
        target = target.resolve() if target.is_absolute() else (cwd / target).resolve()
        if _yard_is_under(target, helm):
            return _yard_deny(
                f"YARD write guard — intentional policy block, not an error. "
                f"This is a YARD worktree, and the write lands inside HELM at "
                f"{target}. A task changes only its own checkout; HELM is the "
                f"live clone and reviews the branch afterwards.\n\n"
                f"Writing files into the data overlay from here is allowed and "
                f"expected. Only HELM is closed."
            )
        if any(_yard_is_under(target, d)
               for d in _yard_data_git_dirs(_yard_data_roots(helm))):
            return _yard_deny(_yard_git_dir_reason(str(target)))
        # HOS-211. The path as spelled is judged too, not only where it
        # resolves: a write through a link is still a write to what it names.
        expanded = os.path.expanduser(raw)
        spelled = Path(os.path.normpath(
            expanded if os.path.isabs(expanded) else cwd / expanded))
        places = _yard_machine_places()
        place = _yard_machine_place(target, places) or _yard_machine_place(spelled, places)
        if place is not None:
            return _yard_deny(_yard_machine_reason(str(target), place))
        where = _foreign(target)
        if where:
            return _yard_deny(_yard_foreign_reason(where))
        return None

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command:
        return None
    own_branch = _yard_own_branch()
    data_roots = _yard_data_roots(helm)
    data_git_dirs = _yard_data_git_dirs(data_roots)
    machine_places = _yard_machine_places()

    # `cd` is followed across the chain, and that is not a refinement. Splitting
    # on `&&` and asking each link about the paths IT spells lets
    # `cd <HELM> && rm -rf x` through: the first link is a read-only verb and
    # the second names no path at all. The same shape reaches the data overlay
    # as `cd <data-root> && git commit`. So a link that changes directory is
    # remembered, and every later link is judged against where it would run.
    #
    # Each link is judged EXPANDED (HOS-196): a variable assigned literally
    # earlier in the same command, one in this hook's environment, and the
    # workspace's own `$(... data-path.py ...)` are put in place first, so
    # `S=<overlay>; cd $S && git commit` is the literal command it stands for.
    # Its limit, stated rather than left to be found: what cannot be known
    # without executing (`$(mktemp -d)`, a loop variable, a value from an
    # earlier Bash call) stays unexpanded. In a directory-selecting position
    # that is refused, below; as a plain operand (`rm $f`) it stays unjudged.
    current = cwd
    for segment in _yard_expanded_segments(command, cwd):
        words = _yard_words(segment)
        if not words:
            continue
        verb = words[0].rsplit("/", 1)[-1]

        unknown = _yard_unresolved_directory(segment)
        if unknown is not None:
            return _yard_deny(
                f"YARD write guard — intentional policy block, not an error. "
                f"This command selects a directory through `{unknown}`, which "
                f"this guard cannot resolve without running something:\n\n"
                f"  {segment[:300]}\n\n"
                f"Where everything after it runs is then unknown, so whether it "
                f"reaches HELM or the data overlay's git cannot be judged. Write "
                f"the path literally, or assign it literally earlier in the same "
                f"command (`D=/the/path; cd $D`), and run it again."
            )

        # Before anything else reads the link, and on the raw text: the
        # redirection targets HOS-199 strips for the overlay-git rule below
        # are exactly how `echo x > <overlay>/.git/hooks/pre-commit` writes.
        if _yard_segment_reaches(segment, current, data_git_dirs):
            return _yard_deny(_yard_git_dir_reason(segment[:300]))

        place = _yard_segment_machine_place(segment, current, machine_places)
        if place is not None:
            return _yard_deny(_yard_machine_reason(segment[:300], place))
        # Judged without redirections: `crontab -l 2>/dev/null` is a read, and
        # its `2>/dev/null` is not an operand (MEASURED in the HOS-196 replay).
        runs = _yard_runs_something_refusal(
            verb, _yard_words(_yard_without_redirections(segment)), current,
            [_yard_mine(), helm, *foreign_roots])
        if runs is not None:
            return _yard_deny(runs)

        if verb == "cd":
            current = _yard_follow_cd(words, current)
            continue

        read_only = _yard_segment_is_read_only(segment)
        if (_yard_segment_names(segment, helm)
                or _yard_is_under(current, helm)) and not read_only:
            return _yard_deny(
                f"YARD write guard — intentional policy block, not an error. "
                f"This shell command reaches into HELM from a YARD worktree, "
                f"and is not one of the read-only forms:\n\n  {segment[:300]}\n\n"
                f"Read it from here if you need to (git log, cat, diff, rg, ls, "
                f"head, tail). Change it in HELM."
            )

        # A neighbour, and `read_only` is not consulted. HELM and the data
        # overlay are readable on purpose; another task's worktree is not, so
        # `cat <neighbour>/x` is refused for the same reason `rm -rf` of it is.
        neighbour = next(
            (root for root in foreign_roots
             if _yard_segment_names(segment, root) or _yard_is_under(current, root)),
            None) or _yard_foreign_checkout_containing(current, helm)
        if neighbour is None and not read_only:
            # A command that names a DIRECTORY CONTAINING a neighbour, which is
            # how `rm -rf` of the worktree container reaches one without ever
            # spelling its name. Only the absolute words, and only when the link
            # is not a read: otherwise `ls <the directory they all live in>`
            # would be refused, and that is a read of nothing but names.
            # THE FILESYSTEM ROOT IS NOT EVIDENCE. Every neighbour is under `/`,
            # and so are HELM and the data overlay, which this YARD is allowed to
            # read, so a lone `/` never distinguished a neighbour from the two
            # places it may reach: it matched every command that happened to
            # contain the character. MEASURED 2026-09-05 in `yard-day-mode-routes`:
            # two `python - <<EOF` heredocs were refused over the pathlib
            # operator in `SCRATCH / rel` and `Path(root) / rel`, each refusal
            # naming a worktree the command had never mentioned, because
            # `_yard_words` splits on whitespace and a binary `/` is a word.
            #
            # Reading the heredoc BODY stays, deliberately: a heredoc that opens
            # a neighbour's file must still be refused, and it is `/` that had to
            # stop counting rather than the body. A word naming a real containing
            # directory is unchanged, which is the case this branch was written
            # for. The check that drove it was parked in the 2026-09-21
            # test-suite split, so that half of the branch is now unexercised:
            # an edit that narrows the neighbour test would look identical from
            # the outside.
            #
            # `rm -rf /` keeps its refusal, in `_yard_root_destruction`, where
            # the reason can say what that command actually does.
            neighbour = next(
                (root for word in words for root in foreign_roots
                 if word.startswith("/") and not _yard_word_is_root(word)
                 and _yard_is_under(root, Path(word.strip("'\"")))),
                None)
            if neighbour is None:
                rooted = _yard_root_destruction(verb, words)
                if rooted is not None:
                    return _yard_deny(rooted)
        if neighbour is not None:
            return _yard_deny(_yard_foreign_reason(neighbour))

        if verb == "git":
            shared = _yard_shared_git_refusal(words, own_branch)
            if shared is not None:
                return _yard_deny(shared)
            if "push" in words[1:]:
                return _yard_deny(
                    "YARD write guard — intentional policy block, not an error. "
                    "The engine repository is PUBLIC, and any branch pushed to "
                    "it is visible to everyone immediately, not only `main`. A "
                    "task never publishes: commit on the task branch and stop. "
                    "Every worktree shares one repository, so HELM can already "
                    "see the branch with nothing transferred, and it is HELM "
                    "that reviews and publishes."
                )
            # The message file of `git commit -F` is read, not written, so a
            # data path there is not git reaching the overlay (HOS-129), and a
            # redirection target is written by the shell, not by git (HOS-199).
            # The stripped prefix goes back in front: `GIT_DIR=<overlay>/.git
            # git commit` reaches the overlay through exactly those words.
            spoken = _yard_without_redirections(segment).split()
            bare = _yard_strip_prefix(spoken)
            judged = " ".join([*spoken[:len(spoken) - len(bare)],
                               _yard_without_commit_message_file(bare)])
            for root in data_roots:
                reaches = (_yard_segment_names(judged, root)
                           or _yard_is_under(current, root))
                if reaches and not read_only:
                    return _yard_deny(
                        f"YARD write guard — the one rule. Writing FILES into "
                        f"the data overlay from a YARD is allowed; running git "
                        f"in it is not.\n\n  {segment[:300]}\n\n"
                        f"Every checkout shares that repository's index, so a "
                        f"commit from a task sweeps up other tasks' unfinished "
                        f"work and whatever the daemons are writing at that "
                        f"moment into one commit nobody reviewed. HELM records "
                        f"the data's history."
                    )
    return None


# ============================================================
# Deleting a YARD that is not finished
# ============================================================
#
# The sibling above asks whether a YARD is reaching OUT of itself. This one asks
# the opposite question, and it is HELM's: is this command about to erase a
# worktree whose work is not yet safe anywhere else?
#
# WHY IT EXISTS. On 2026-09-06 an agent removed a finished yard with
# `git worktree remove <path> --force`, which switches off git's one refusal to
# remove a worktree carrying changes. The operator's instruction was to make
# erasing unfinished work impossible for the agent. That is the boundary: a
# PreToolUse hook sees one tool call, never what runs inside the process it
# starts (a script, a test run, the operator's own terminal).
#
# A YARD is UNFINISHED when any one of three is true, asked of the TARGET's tree:
#
#   1. its working tree carries changes (`git status --porcelain` in THAT tree);
#   2. its HEAD holds commits `main` cannot reach (`rev-list --count main..HEAD`);
#   3. a LIVE process stands in it that THIS COMMAND WOULD NOT CLOSE.
#
# Every unmet one is named, because "commit it" and "it is still running" are
# different instructions. HELM is refused outright, at the end of
# `check_yard_deletion_guard`: it holds the object store and the reflog every
# worktree points into, so its own clean tree proves nothing.
#
# OWNERSHIP (condition 3). A finished yard is never empty: MEASURED 2026-09-06,
# seven processes stood in one (the agent, its MCP servers, its shells).
# `herdr worktree remove --workspace <ID>` closes that workspace's session, so a
# process carrying `HERDR_WORKSPACE_ID=<ID>` is part of the operation; `rm -rf`
# and `git worktree remove` close nothing, so every process still counts. A
# process whose environment cannot be read, or names no or another workspace,
# is FOREIGN and refuses.
#
# ORPHANS. A process naming a workspace herdr no longer records (warmed
# background workers whose cwd wandered in; MEASURED 2026-09-06, nine of
# fourteen in one yard) is dropped for every form: it is nobody's unfinished
# work. When herdr's record cannot be read, nothing is dropped and the refusal
# says why. Not argv, which would be a list of process shapes. The workspace id
# is a claim the process makes about itself, and is written as one: both
# exemptions reach condition 3 only, and conditions 1 and 2 hold the work.
#
# It never removes, commits, merges or kills anything, and a finished yard
# passes in silence.

# A worktree under one of these is a throwaway: `tmp_path`, `tmp_path_factory`
# and every `mkdtemp` in the suite land here, and the suite creates and destroys
# real worktrees of this repository. LITERAL, and read from no environment
# variable on purpose: `tempfile.gettempdir()` honours `TMPDIR`, so a guard
# resting on it carries its own disarm switch (`TMPDIR=/ rm -rf <yard>`).
# S108 / B108: the literal is deliberate (read only as comparison prefixes).
# Both suffixes are needed: ruff and bandit each read only their own (2026-09-06).
_YARD_DELETION_TEMP_ROOTS = (Path("/tmp"), Path("/var/tmp"))  # noqa: S108  # nosec B108

# Cheap first question, asked before anything resolves a path or reads a
# registry. Every deletion form below spells one of these two words, and almost
# no other command does.
_YARD_DELETION_HINT_RE = re.compile(r"(?:^|[^\w-])rm(?:$|[^\w-])|worktree")

# `rm -r`, `rm -rf`, `rm -fr`, `rm --recursive`. A bare `rm` cannot remove a
# directory, so it cannot remove a checkout.
_YARD_RM_RECURSIVE_RE = re.compile(r"^-[a-zA-Z]*[rR]")


# A usage request is not a deletion. MEASURED 2026-09-08 in HELM against the
# version this exemption was added to: `herdr worktree remove --help`,
# `herdr worktree remove -h`, `git worktree remove --help` and
# `git worktree remove -h` all produced the byte-identical refusal telling the
# operator to name the worktree by its path, for four commands that delete
# nothing. The herdr README (now under `spec-docs` in the data repository)
# records the herdr help form as the source of a measurement, so the wall
# refused a step of its own runbook.
#
# The exemption is NARROW on purpose. A help flag exempts nothing by itself; it
# is read only when the link carries NOTHING ELSE: no path, no `--workspace`,
# no `--force`, no second flag. Whether a CLI honours a help flag when it is
# also handed work is that CLI's business and not knowable from a hook, so the
# only case waved through is the one where there is no work to honour.
_YARD_HELP_FLAGS = frozenset({"--help", "-h"})


def _yard_deletion_operands(words: list[str]) -> list[str]:
    """The non-flag words of a link, unquoted, `--` and what follows included.

    A redirection is dropped on BOTH sides of `--`. The shell removes plumbing
    before the program is handed its argv, so `--` ends the flags and has no
    bearing on whether `2>&1` is one.
    """
    out = []
    seen_separator = False
    for word in words[1:]:
        if word == "--" and not seen_separator:
            seen_separator = True
            continue
        if _yard_is_redirection(word):
            continue
        if word.startswith("-") and not seen_separator:
            continue
        out.append(word.strip("'\""))
    return out


def _yard_herdr_session_files() -> list[Path]:
    """Every file that could hold herdr's workspace-id → checkout-path map.

    BOTH candidates, never the first that exists. `HERDR_SOCKET_PATH` is an
    environment variable, so a resolver trusting it alone would take its answer
    from a file the running process can choose; the fixed `~/.config/herdr`
    path is read as well and the union of what they say is used, so pointing
    the variable at a doctored file can only ADD a checkout to guard.
    """
    candidates: list[Path] = []
    socket = os.environ.get("HERDR_SOCKET_PATH")
    if socket:
        with contextlib.suppress(OSError, RuntimeError):
            candidates.append(Path(socket).expanduser().parent / "session.json")
    with contextlib.suppress(RuntimeError):
        candidates.append(Path.home() / ".config" / "herdr" / "session.json")
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _yard_herdr_checkouts(workspace_id: str) -> list[Path]:
    """Every checkout path herdr records for `workspace_id`.

    `herdr worktree remove --workspace <ID>` names no path at all, so the path
    has to be resolved before the command can be judged. It IS resolvable, and
    cheaply: herdr keeps its session state as JSON, and each workspace record
    carries `worktree_space.checkout_path`. MEASURED 2026-09-06 against the live
    file, 3901 bytes and three workspaces. That is one small read and a JSON
    parse, no subprocess and no socket, so this form is covered rather than
    waved through.

    An empty result means "could not establish", never "nothing to protect";
    the caller refuses on it.
    """
    found: list[Path] = []
    for path in _yard_herdr_session_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Logged rather than swallowed. An absent file is the ordinary state
            # on a machine with no herdr, and the caller refuses anyway when
            # nothing resolves, so this never fails open.
            print(f"[_dispatch:yard-deletion] {path} unreadable "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        for workspace in data.get("workspaces") or []:
            if not isinstance(workspace, dict):
                continue
            if workspace.get("id") != workspace_id:
                continue
            space = workspace.get("worktree_space")
            checkout = space.get("checkout_path") if isinstance(space, dict) else None
            if not checkout:
                continue
            candidate = Path(checkout)
            with contextlib.suppress(OSError):
                candidate = candidate.resolve()
            if candidate not in found:
                found.append(candidate)
    return found


def _yard_herdr_workspace_for(checkout: Path) -> str | None:
    """The herdr workspace id whose checkout is `checkout`, or None.

    The reverse of `_yard_herdr_checkouts`, and it exists only to make a refusal
    actionable: when the one thing standing in a yard is its own session,
    the operator needs the spelling that CLOSES it rather than a wall. Read on
    the refusal path only, so it costs nothing on a command that passes.
    """
    for path in _yard_herdr_session_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[_dispatch:yard-deletion] {path} unreadable "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        for workspace in data.get("workspaces") or []:
            if not isinstance(workspace, dict):
                continue
            space = workspace.get("worktree_space")
            recorded = space.get("checkout_path") if isinstance(space, dict) else None
            if not recorded:
                continue
            candidate = Path(recorded)
            with contextlib.suppress(OSError):
                candidate = candidate.resolve()
            if candidate == checkout and workspace.get("id"):
                return str(workspace["id"])
    return None


def _yard_deletion_requests(
        command: str, cwd: Path) -> list[tuple[str, list[Path], str | None]]:
    """Every link that would destroy a checkout: (form, targets, closes).

    An EMPTY target list means the form was recognised but what it points at
    could not be established, which the caller refuses on. Forms, not one
    string: the incident was a `git worktree remove --force`, and matching that
    literal would leave `rm -rf` and the herdr spelling open.

    `closes` is the herdr workspace id this command SHUTS DOWN as part of the
    removal, and it is None for every form that shuts nothing down. It is the
    third element rather than something the caller re-derives from `form`,
    because "does this close the session standing there" is a property of the
    command and belongs where the command is read.
    """
    requests: list[tuple[str, list[Path], str | None]] = []
    current = cwd

    def _resolve(spelled: str) -> Path:
        candidate = Path(spelled)
        candidate = candidate if candidate.is_absolute() else current / candidate
        with contextlib.suppress(OSError):
            candidate = candidate.resolve()
        return candidate

    for segment in _yard_expanded_segments(command, cwd):
        words = _yard_words(segment)
        if not words:
            continue
        verb = words[0].rsplit("/", 1)[-1]

        if verb == "cd":
            current = _yard_follow_cd(words, current)
            continue

        if verb == "rm":
            recursive = any(word == "--recursive" or _YARD_RM_RECURSIVE_RE.match(word)
                            for word in words[1:])
            if not recursive:
                continue          # a bare `rm` cannot remove a directory
            targets = [_resolve(operand)
                       for operand in _yard_deletion_operands(words)]
            if targets:
                requests.append(("rm", targets, None))
            continue

        if verb == "git":
            if _yard_git_subcommand(words) != "worktree":
                continue
            operands = _yard_git_operands("worktree", words)
            if operands[:1] != ["remove"]:
                continue
            # `_yard_git_flags` reads the flags AFTER the subcommand only, so
            # `git -C /x worktree remove -h` is covered and `git --no-pager`
            # cannot masquerade as a help flag.
            flags = _yard_git_flags("worktree", words)
            if len(operands) == 1 and flags and all(
                    flag in _YARD_HELP_FLAGS for flag in flags):
                continue          # usage only: no operand and no other flag
            requests.append(("git worktree remove",
                             [_resolve(operand) for operand in operands[1:]],
                             None))
            continue

        if verb == "herdr":
            operands = _yard_deletion_operands(words)
            if operands[:2] != ["worktree", "remove"]:
                continue
            flags = [word.split("=", 1)[0] for word in words if word.startswith("-")]
            if len(operands) == 2 and flags and all(
                    flag in _YARD_HELP_FLAGS for flag in flags):
                continue          # usage only: no operand and no other flag
            workspace_id = ""
            for index, word in enumerate(words):
                if word.startswith("--workspace="):
                    workspace_id = word.split("=", 1)[1].strip("'\"")
                elif word == "--workspace" and index + 1 < len(words):
                    workspace_id = words[index + 1].strip("'\"")
            targets = _yard_herdr_checkouts(workspace_id) if workspace_id else []
            requests.append(("herdr worktree remove", targets,
                             workspace_id or None))

    return requests


def _yard_guarded_checkouts() -> list[Path]:
    """Every worktree of THIS repository that a deletion has to be judged against.

    Derived from the current checkout, through `worktree_roots`, which reads the
    registry git keeps in the main clone and drops both HELM and registrations
    whose directory is gone. Never a constant, never `${CLAUDE_PROJECT_DIR}`:
    this file's own location is the only input.

    Throwaways are dropped here rather than in the caller, because the suite
    creates and destroys real worktrees of this repository under `/tmp` and a
    guard that fights its own test suite is a guard somebody switches off.
    """
    from scripts.utils.clone_guard import worktree_roots
    return [root for root in worktree_roots(WORKSPACE)
            if not any(_yard_is_under(root, temp)
                       for temp in _YARD_DELETION_TEMP_ROOTS)]


def _yard_main_clone() -> Path | None:
    """HELM's own path, when a deletion has to be judged against it too.

    `worktree_roots` drops the main clone by construction: it is the clone the
    registry belongs to, not a worktree of it. Correct for every other caller
    and a hole here, because `rm -rf <HELM>` is a command the deletion guard
    watched pass in silence.

    Derived from `main_clone_path`, which asks git for `--git-common-dir` and so
    answers the same from HELM, from a YARD and from any subdirectory of either.
    The temp-tree exemption is the same one the yards get, and for the same
    reason: the suite builds real main clones under `tmp_path`, and a guard that
    fights its own suite is a guard somebody switches off.
    """
    from scripts.utils.clone_guard import main_clone_path
    root = main_clone_path(WORKSPACE)
    if any(_yard_is_under(root, temp) for temp in _YARD_DELETION_TEMP_ROOTS):
        return None
    return root


def _yard_git_answer(root: Path, args: list[str]) -> tuple[bool, str]:
    """Run one read-only git command in `root`. (ok, output-or-error)."""
    import subprocess
    try:
        # `errors="replace"`: a filename in that worktree can hold bytes that are
        # not UTF-8, and `text=True` alone raises UnicodeDecodeError, which is a
        # ValueError and so is caught by neither handler below. That would end
        # the hook rather than the command, which is a wall going down over one
        # byte of a child's output.
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout).strip()
    return True, completed.stdout


#: The variable herdr exports into a pane, inherited by the agent, its MCP
#: servers, its shells and their children. See the ownership clause above.
_HERDR_WORKSPACE_ENV = "HERDR_WORKSPACE_ID"


def _yard_live_workspaces() -> set[str] | None:
    """Every workspace id herdr currently records, or None if nothing was read.

    None is the "could not look" answer again, and the caller must not read it
    as an empty set: with no record, a process naming a workspace is
    indistinguishable from an orphan, and dropping it would be inventing the
    exemption rather than establishing it.
    """
    ids: set[str] = set()
    read_any = False
    for path in _yard_herdr_session_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[_dispatch:yard-deletion] {path} unreadable "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        read_any = True
        if not isinstance(data, dict):
            continue
        for workspace in data.get("workspaces") or []:
            if isinstance(workspace, dict) and workspace.get("id"):
                ids.add(str(workspace["id"]))
    return ids if read_any else None


def _yard_live_processes(
        root: Path,
        owned_by: str | None = None) -> tuple[list[str] | None, str | None]:
    """(names, caveat) for the processes standing inside `root` that count.

    `names` is None when `/proc` could not be read at all, which is NOT an empty
    list: "could not look" and "nobody is there" send the operator opposite
    ways, and the caller refuses on the first.

    Two kinds are dropped, and neither is a weakening of the condition:

      * the workspace the COMMAND closes (`owned_by`, herdr form only) -- the
        removal closes those, so they are part of the operation;
      * an ORPHAN, whose workspace herdr no longer records at all. Its yard is
        already gone, so it is not unfinished work standing here.

    Everything else is reported: a live neighbouring workspace, a process naming
    no workspace, and one whose environment could not be read.

    `caveat` is a sentence for the refusal when the drop could not be made
    honestly, so the operator is never shown a process count that rests on a
    lookup nobody managed.

    The process question is asked of `scripts/utils/proc_cwd.processes_in`,
    which owns it for the callers that want it; the import is deferred because
    this hook runs on every tool call and only ever reaches here on a deletion.
    """
    from scripts.utils.proc_cwd import processes_in
    found = processes_in(root, nested=True, env_names=(_HERDR_WORKSPACE_ENV,))
    if found is None:
        return None, None

    live = _yard_live_workspaces()
    kept: list[str] = []
    for process in found:
        workspace = (process.env or {}).get(_HERDR_WORKSPACE_ENV)
        if owned_by and workspace == owned_by:
            continue
        if workspace and live is not None and workspace not in live:
            continue                   # an orphan: its own yard is already gone
        kept.append(f"{process.pid} ({process.comm})" if process.comm
                    else str(process.pid))

    caveat = None
    if kept and live is None:
        caveat = (" herdr's session record could not be read, so a process "
                  "naming a workspace that no longer exists could not be told "
                  "from one naming a live neighbour, and none were dropped")
    return kept, caveat


def _yard_unfinished(root: Path,
                     owned_by: str | None = None) -> list[tuple[str, str]]:
    """Every reason `root` is not finished, as (kind, sentence).

    Empty means it is safe to delete. The KIND is carried rather than inferred
    from the sentence, because the refusal has to tell the operator what to DO,
    and "commit it", "have it merged" and "something is standing in it" are
    three different instructions. Until 2026-09-06 one closing paragraph gave
    all three unconditionally, so a refusal over a live process ended "commit
    the work and have HELM merge the branch, then this passes without a word"
    against a yard whose work was committed and whose branch was merged. A
    remedy that does not apply is a wall lying about itself.

    `owned_by` is passed through to condition 3 only. Conditions 1 and 2 do not
    know which command is asking and must not: the incident this wall was
    written for is a `--force` over uncommitted work, and no spelling of the
    removal makes that survivable.
    """
    unmet: list[tuple[str, str]] = []

    ok, answer = _yard_git_answer(root, ["status", "--porcelain"])
    if not ok:
        unmet.append(("unknown",
                      f"its working tree could not be read ({answer[:200]}), so "
                      f"whether it holds uncommitted work is UNKNOWN, not no"))
    elif answer.strip():
        changed = len([line for line in answer.splitlines() if line.strip()])
        unmet.append(("tree",
                      f"{changed} uncommitted change(s) in its working tree "
                      f"(modified tracked files, or untracked files git does "
                      f"not ignore)"))

    ok, answer = _yard_git_answer(root, ["rev-list", "--count", "main..HEAD"])
    if not ok:
        unmet.append(("unknown",
                      f"its commits could not be compared against `main` "
                      f"({answer[:200]}), so whether the branch is merged is "
                      f"UNKNOWN, not yes"))
    else:
        try:
            ahead = int(answer.strip() or "0")
        except ValueError:
            ahead = -1
        if ahead < 0:
            unmet.append(("unknown",
                          f"`rev-list --count main..HEAD` answered "
                          f"{answer.strip()!r}, which is not a count, so whether "
                          f"the branch is merged is UNKNOWN"))
        elif ahead:
            unmet.append(("branch",
                          f"{ahead} commit(s) on its HEAD that `main` cannot "
                          f"reach"))

    live, caveat = _yard_live_processes(root, owned_by)
    if live is None:
        unmet.append(("unknown",
                      "`/proc` could not be enumerated, so whether a session is "
                      "standing in it is UNKNOWN, not no"))
    elif live:
        shown = ", ".join(live[:6])
        if len(live) > 6:
            shown += f", and {len(live) - 6} more"
        closes = (" that this command would not close" if owned_by else "")
        unmet.append(("processes",
                      f"{len(live)} live process(es) standing in it{closes}: "
                      f"{shown}{'.' + caveat if caveat else ''}"))

    return unmet


def _yard_deletion_remedies(unmet: list[tuple[str, str]], form: str, root: Path,
                            closes: str | None) -> str:
    """One instruction per unmet condition, and none for a condition that holds.

    Composed from the KINDS rather than printed unconditionally. The sentence
    this replaced ended "then this command passes without a word" under a
    refusal caused by a running process, against a yard whose work was
    committed and whose branch was merged, so it named two things the operator
    had already done and not the one that was blocking.
    """
    kinds = {kind for kind, _ in unmet}
    lines: list[str] = []
    if "tree" in kinds:
        lines.append("  * Commit the work in that yard. `--force` does not lift "
                     "this: it switches off git's own version of that check, "
                     "which is what made this wall necessary.")
    if "branch" in kinds:
        lines.append("  * Have HELM merge the branch. A worktree is the only "
                     "place its unmerged commits exist.")
    if "processes" in kinds:
        if closes is not None:
            lines.append("  * Those processes belong to a workspace this "
                         "command does not close, so removing this one leaves "
                         "them holding a deleted directory. Wait for them, or "
                         "close the workspace that owns them.")
        else:
            # Only on this path: the lookup is a file read, and a command that
            # passes must not pay for it.
            workspace = _yard_herdr_workspace_for(root)
            spelling = (f"`herdr worktree remove --workspace {workspace}`"
                        if workspace else
                        "`herdr worktree remove --workspace <this yard's id>`")
            lines.append(f"  * `{form}` closes nothing, so every process above "
                         f"would be left holding a deleted directory. "
                         f"{spelling} closes this yard's own session as part of "
                         f"the removal, and is the last step of the cycle.")
    if "unknown" in kinds:
        lines.append("  * Establish what could not be read above and run it "
                     "again. Nothing here guesses in the permissive direction.")
    return "\n".join(lines)


def check_yard_deletion_guard(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command or not _YARD_DELETION_HINT_RE.search(command):
        return None

    cwd = Path(payload.get("cwd") or WORKSPACE)
    requests = _yard_deletion_requests(command, cwd)
    if not requests:
        return None

    try:
        guarded = _yard_guarded_checkouts()
        helm = _yard_main_clone()
    except Exception as exc:  # noqa: BLE001 - resolving is what this rests on
        return _yard_deny(
            f"YARD deletion guard: this command removes a checkout, and the "
            f"worktrees of this repository could not be enumerated "
            f"({type(exc).__name__}: {exc}), so what it would erase cannot be "
            f"established. Refusing in the safe direction."
        )

    for form, targets, closes in requests:
        if not targets:
            # The advice is per FORM, because the two forms take different
            # things and the single sentence this replaces offered both to
            # both. MEASURED 2026-09-08: `herdr worktree remove <path>` cannot
            # work. `_yard_deletion_operands` collects the positional, the
            # herdr branch never reads it (it resolves the checkout from
            # `--workspace` alone), and herdr itself exits 2 on an extra
            # operand. So half of that sentence sent the operator to a spelling
            # that fails twice over, on the refusal path, which is exactly
            # where a wall has to be right or it gets switched off.
            advice = (
                "Name the workspace id herdr knows, as `--workspace <id>`, and "
                "run it again. A path does not work for this form: herdr "
                "resolves the checkout from `--workspace` alone."
                if form == "herdr worktree remove"
                else "Name the worktree by its path and run it again.")
            return _yard_deny(
                f"YARD deletion guard — intentional policy block, not an error. "
                f"`{form}` names no checkout this hook could resolve, so whether "
                f"it would erase unfinished work cannot be established.\n\n"
                f"{advice}"
            )
        for target in targets:
            # `root == target` is the yard itself; `root under target` is a
            # command aimed at a directory that CONTAINS yards, which is how
            # `rm -rf` of the worktree container erases several without ever
            # spelling one of their names.
            for root in guarded:
                if root != target and not _yard_is_under(root, target):
                    continue
                unmet = _yard_unfinished(root, closes)
                if not unmet:
                    continue
                reasons = "\n".join(f"  * {text}" for _, text in unmet)
                aimed = ("" if root == target
                         else f"\nAimed at {target}, which contains it.")
                return _yard_deny(
                    f"YARD deletion guard — intentional policy block, not an "
                    f"error. This `{form}` would erase a worktree that is NOT "
                    f"finished:\n\n  {root}{aimed}\n\n"
                    f"What is unfinished:\n{reasons}\n\n"
                    f"What to do:\n{_yard_deletion_remedies(unmet, form, root, closes)}"
                )

    # HELM, and it is asked LAST so that a command sweeping the directory the
    # yards live in is still answered by the yard it would erase, naming the
    # work that is actually at risk.
    #
    # A WIDENING BEYOND THE ORIGINAL INSTRUCTION, which was about yards. It is
    # here because the set the loop above walks comes from `worktree_roots`,
    # which drops the main clone, so a command spelling HELM's own path passed
    # in silence while the same command against any yard was refused.
    #
    # UNCONDITIONAL, and deliberately not the three yard conditions. Those ask
    # "is what is in here safe somewhere else", and for HELM the answer is
    # structurally no whatever its tree says: it holds the only object store,
    # so every yard's `.git` file points into it and all of them die with it,
    # and it holds the reflog, which is the only record of what was thrown away.
    # A clean working tree says nothing about any of that. Condition 3 would
    # have been the third instance of one shape as well, the other way up: the
    # operator's own session stands in HELM, so it would fire always, and a
    # condition that is always true teaches nothing.
    #
    # There is no legitimate step that deletes HELM. It is not part of the
    # cycle, nothing produces it as an ending, and the operator's own terminal
    # is not covered by this hook at all -- so the honest cost of refusing here
    # is that the agent cannot do it, which is the whole point.
    if helm is not None:
        for form, targets, _closes in requests:
            for target in targets:
                if helm != target and not _yard_is_under(helm, target):
                    continue
                aimed = ("" if helm == target
                         else f"\nAimed at {target}, which contains it.")
                return _yard_deny(
                    f"YARD deletion guard — intentional policy block, not an "
                    f"error. This `{form}` would erase the MAIN CLONE:\n\n"
                    f"  {helm}{aimed}\n\n"
                    f"Unlike a yard, this is refused whatever state it is in. "
                    f"It holds the one object store every worktree of this "
                    f"repository points into, so removing it takes every yard "
                    f"with it, and it holds the reflog, which is the only "
                    f"record of what a mistake threw away. A clean working "
                    f"tree does not make any of that recoverable.\n\n"
                    f"Nothing in the documented cycle deletes it. If it really "
                    f"has to go, that is the operator's own keyboard, not an "
                    f"agent's tool call."
                )
    return None


# ============================================================
# An agent started into a yard's pane without the yard marker
# ============================================================
#
# MEASURED 2026-09-27. A yard was created with `herdr worktree create`, and the
# session that created it then ran `herdr agent start --kind claude --pane
# <ws>:p1` while the bootstrap was still at step 9. `agent start` launches the
# canonical executable into the pane's shell, so the agent came up WITHOUT
# `HEADING_OS_YARD`, and every yard-side guard keyed on that marker was inert
# in it. The bootstrap's own step-11 launch line then arrived in the live agent
# and was read as a prompt.
#
# `agent start` has no way to carry the marker: it starts the executable herdr
# knows for `--kind`, in the pane's existing environment. So in a yard's pane
# it is refused outright, and the refusal names the spelling that does carry
# it. A pane in the main clone passes, because HELM runs without the marker by
# design.
#
# The pane id is `<workspace>:p<N>` (MEASURED 2026-09-27 from `herdr pane
# list`), so the workspace is read off the id and resolved to its checkout
# through the same session file the deletion guard reads. Anything that cannot
# be resolved is refused, as there: an empty answer means "could not
# establish", never "nothing to protect".

_YARD_AGENT_START_HINT_RE = re.compile(r"herdr")

_YARD_AGENT_LAUNCH = "HEADING_OS_YARD=1 claude --dangerously-skip-permissions"


def _yard_agent_start_panes(command: str) -> list[str | None]:
    """The `--pane` value of every `herdr agent start` link, None when absent."""
    panes: list[str | None] = []
    for segment in _yard_segments(command):
        words = _yard_words(segment)
        if not words or words[0].rsplit("/", 1)[-1] != "herdr":
            continue
        operands = _yard_deletion_operands(words)
        if operands[:2] != ["agent", "start"]:
            continue
        flags = [word.split("=", 1)[0] for word in words if word.startswith("-")]
        if flags and all(flag in _YARD_HELP_FLAGS for flag in flags):
            continue          # usage only
        pane = None
        for index, word in enumerate(words):
            if word.startswith("--pane="):
                pane = word.split("=", 1)[1].strip("'\"")
            elif word == "--pane" and index + 1 < len(words):
                pane = words[index + 1].strip("'\"")
        panes.append(pane or None)
    return panes


def check_yard_agent_start_guard(payload: dict) -> dict | None:
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command or not _YARD_AGENT_START_HINT_RE.search(command):
        return None
    for pane in _yard_agent_start_panes(command):
        spelled = pane or "<pane>"
        remedy = (
            f"Start the agent through the pane's shell instead, so the marker "
            f"reaches it:\n\n"
            f"  herdr pane run {spelled} \"{_YARD_AGENT_LAUNCH}\"\n\n"
            f"Normally nothing needs starting at all: the yard bootstrap starts "
            f"the agent itself at its last step, once the guards are proved. "
            f"Wait for the pane title to read \"YARD ready\".")
        workspace = pane.split(":", 1)[0] if pane and ":" in pane else ""
        checkouts = _yard_herdr_checkouts(workspace) if workspace else []
        # A checkout whose `.git` is neither a directory (main clone) nor a
        # file (linked worktree) is gone or not a checkout: unresolved.
        if not checkouts or any(not (root / ".git").exists() for root in checkouts):
            return _yard_deny(
                f"YARD agent-start guard: intentional policy block, not an "
                f"error. `herdr agent start` names a pane ({spelled}) whose "
                f"checkout this hook could not resolve, so whether it is a "
                f"yard, where the agent would run without HEADING_OS_YARD, "
                f"cannot be established.\n\n{remedy}"
            )
        linked = [root for root in checkouts if (root / ".git").is_file()]
        if linked:
            return _yard_deny(
                f"YARD agent-start guard: intentional policy block, not an "
                f"error. Pane {spelled} stands in a yard:\n\n  {linked[0]}\n\n"
                f"`herdr agent start` would launch the agent there WITHOUT "
                f"HEADING_OS_YARD, so every yard-side guard would be inert in "
                f"it, and a launch line the bootstrap sends afterwards would "
                f"reach the live agent as a prompt.\n\n{remedy}"
            )
    return None


# ============================================================
# A crashed wall
# ============================================================
#
# Until 2026-09-02 a check that raised was logged, turned into an advisory, and
# skipped. The tool call then ran with that wall down, and the only thing
# standing between the operator and an unguarded operation was the model reading
# an advisory and choosing to stop. That is not a wall; it is a suggestion, and
# it is the same shape as every other defect this workspace spent ten days
# removing: a control that reports something other than what it established.
#
# So a crash now BLOCKS. Three properties make that safe enough to live with:
#
# * Every remaining check still runs. The crash is collected, not raised, so a
#   later wall's real refusal is never lost behind an earlier wall's bug, and a
#   genuine block still wins because the block path is terminal.
# * The block is not total. Reading, searching and listing stay open, so the
#   operator can diagnose the crash, and an edit to the hooks directory stays
#   open, so the crash can be REPAIRED. A wall that cannot be fixed without
#   itself being disarmed would get disarmed.
# * The refusal names the wall and the exception, so the fix is one file away
#   rather than a hunt.
#
# The exemption is deliberately narrow and deliberately not configurable. There
# is no environment variable here, and there must not be: a switch that turns
# this off is the wall's own disarm button.

_CRASH_READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})
_CRASH_REPAIR_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_CRASH_REPAIR_DIR = ".claude/hooks/"


def crash_block_exemption(payload):
    """Why a crashed wall must not block THIS call, or None to block it.

    Pure, and returns the reason rather than a bool so the caller can say which
    exemption applied instead of asserting one existed.
    """
    tool = payload.get("tool_name", "") or ""
    if tool in _CRASH_READ_ONLY_TOOLS:
        return "read-only tool: diagnosing the crash must stay possible"
    if tool in _CRASH_REPAIR_TOOLS:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        target = (tool_input.get("file_path")
                  or tool_input.get("notebook_path") or "")
        if _CRASH_REPAIR_DIR in str(target).replace("\\", "/"):
            return "edit inside .claude/hooks/: repairing the crash must stay possible"
    return None


def crashed_wall_block(crashed, exemption):
    """The refusal for one or more walls that raised, or None when exempt.

    `crashed` is a list of `(check_name, exception_text)`.
    """
    if not crashed or exemption is not None:
        return None
    listed = "\n".join(f"  {name}: {error}" for name, error in crashed)
    return {
        "decision": "block",
        "_policy_deny": True,
        "reason": (
            f"HOOK WALL CRASHED: {len(crashed)} PreToolUse check(s) raised, so "
            f"this operation would have run with that wall down.\n\n"
            f"{listed}\n\n"
            "Until 2026-09-02 this was an advisory and the call proceeded. A "
            "wall that reports its own failure and then steps aside is not a "
            "wall. Reading, searching and editing under .claude/hooks/ are "
            "still allowed, so the crash can be diagnosed and repaired; "
            "everything else stops until it is.\n\n"
            "Fix the check named above. There is no flag, variable or setting "
            "that turns this off, by design."
        ),
    }


def _terminate(source, decision, payload):
    """Record one refusal and render it. Never returns; always exits.

    The ONLY call site of `_record_denial` in this file, and that is the
    invariant to keep: a counter called from individual checks would leave a
    new check uncounted unless its author remembered, and a step that depends
    on remembering is already dead. A check held the single call site until the
    2026-09-21 test-suite split parked it, which is an irony worth naming here:
    the invariant that must not depend on remembering now does. The
    crashed-wall refusal came through here for the same reason -- a second call
    site would have been a second thing to keep in step.

    `source` is what refused: a check's `__name__`, or `crashed_wall`.
    """
    _record_denial(source, payload, decision.get("reason", ""))
    # protect-personal-threads blocks are rendered as a PreToolUse permission
    # deny (hookSpecificOutput / exit 0) so the CLI shows an intentional policy
    # block with its reason, NOT a "hook error" -- the exit-2 + stderr path the
    # harness labels as an error. The deny is just as binding as exit 2
    # (claude-code-guide confirmed both paths block with 100% reliability); only
    # the presentation changes.
    if decision.get("_policy_deny"):
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": decision.get("reason", ""),
                }
            },
            sys.stdout,
        )
        sys.exit(0)
    # Strip internal flags (defensive - shouldn't be present here).
    public = {k: v for k, v in decision.items() if not k.startswith("_")}
    json.dump(public, sys.stdout)
    sys.exit(0)


# ============================================================
# Dispatcher main
# ============================================================
CHECKS = [
    check_prevent_secrets,
    check_release_gate,
    check_protect_personal_threads,
    check_protect_docs,
    check_yard_write_guard,
    check_yard_deletion_guard,
    check_yard_agent_start_guard,
    check_cwd_anchor,
    check_slow_shell,
    check_rate_limit,
    check_graph_first,
    check_hindsight_first,
    check_fanout_first,
    check_tool_budget,
]

def main():
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        # Deliberate fail-open ONLY for unparseable stdin: Claude Code always sends
        # well-formed JSON, so a parse failure means the harness contract is broken,
        # not an exfil attempt. Blocking here would wedge every tool call. Logged.
        print(f"[_dispatch] failed to parse input: {e}", file=sys.stderr)
        sys.exit(0)
    # Coerce a malformed payload into a safe shape so every check sees a dict and
    # none crash into the advisory-and-continue path (which would let the tool run
    # unchecked). Added 2026-06-09 audit (hooks finding — non-dict tool_input).
    if not isinstance(payload, dict):
        print("[_dispatch] payload is not a dict; treating as empty", file=sys.stderr)
        payload = {}
    if not isinstance(payload.get("tool_input"), dict):
        payload["tool_input"] = {}
    advisory = []
    crashed = []
    for check in CHECKS:
        try:
            decision = check(payload)
        except Exception as e:
            # Logged to stderr and carried as an advisory; the crash itself
            # BLOCKS after the loop (see `crashed_wall_block`).
            print(f"[_dispatch:{check.__name__}] {e}", file=sys.stderr)
            advisory.append(
                f"HOOK INTERNAL ERROR in {check.__name__}: {e}. "
                f"Verify this operation is safe before proceeding."
            )
            crashed.append((check.__name__, str(e)))
            # Still `continue`: the remaining walls must run, so an early crash
            # can never hide a later wall's real refusal. The block for this
            # crash is raised after the loop, where a genuine refusal has
            # already had its chance to win.
            continue
        if decision is None:
            continue
        if decision.get("decision") == "block":
            _terminate(check.__name__, decision, payload)
        if decision.get("additionalContext"):
            advisory.append(decision["additionalContext"])

    # After every wall has run, so a real refusal above already won and a crash
    # here cannot mask one. Rendered through the same `_policy_deny` path the
    # other refusals use, and recorded in the same denial log, so a crashed wall
    # is counted rather than only printed.
    crash_decision = crashed_wall_block(crashed, crash_block_exemption(payload))
    if crash_decision is not None:
        _terminate("crashed_wall", crash_decision, payload)

    if advisory:
        # Nested in `hookSpecificOutput`: a top-level `additionalContext` on
        # PreToolUse is silently ignored (Claude Code hooks documentation), and
        # every advisory was lost until 2026-09-01 (7779d233).
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "\n".join(advisory),
                }
            },
            sys.stdout,
        )
    sys.exit(0)

if __name__ == "__main__":
    main()
