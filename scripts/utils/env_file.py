#!/usr/bin/env python3
"""The one writer of a `.env` line, shared by every tool that sets a secret.

Usage:
    from scripts.utils.env_file import upsert_env_value, is_valid_env_var_name
    upsert_env_value(workspace_root / ".env", "SOME_API_KEY", value)

Moved here from `scripts/apply-wizard-answers.py` on 2026-11-01 (HOS-302), when
`scripts/heading-secrets.py` took over writing secrets and the wizard stopped:
the comments below record what each earlier defect of this writer cost. The grammar it reads with is `parse_env_line` in
`scripts/utils/paths.py`, shared with every reader of the file.

Raises ValueError for a value it refuses and for a `.env` that is not UTF-8
(`UnicodeDecodeError` is a ValueError) and for a value that would not read
back unchanged; OSError for a write that failed. POSIX only (`fcntl`). The
caller decides what each means to its own user.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import tempfile
from pathlib import Path

from scripts.utils.paths import parse_env_line


def is_valid_env_var_name(name: str) -> bool:
    """An upper-case POSIX variable name, the only shape a secret is written under."""
    return bool(re.fullmatch(r"[A-Z_][A-Z0-9_]*", name))


# Every character `str.splitlines()` breaks a line on, plus NUL. `upsert_env_value`
# reads the existing file with `splitlines()`, so this is the exact set that can
# turn one written `KEY=...` into two on the next read.
#
# The guard listed `\r`, `\n` and `\x00` alone until 2026-09-02 while its
# docstring promised that a control character was refused. The other seven split
# a line just as effectively: a value holding `\x0b` was written verbatim as one
# line, and the next upsert read it back as `KEY=abc` plus an orphan
# `def` that then sat in the operator's `.env` forever. Verified against CPython:
# `"a\x0bb".splitlines()` is `["a", "b"]`.
#
# A tab is deliberately NOT here. It is a control character and the docstring no
# longer claims otherwise, but `splitlines()` keeps it inside the line, so
# refusing it would reject a legitimate paste for no corruption it can cause.
#
# Written as escapes, never as literals: `.claude/rules/hidden-chars.md` bans
# invisible Unicode in generated text, and the PostToolUse scanner refuses
# this file outright when U+2028 and U+2029 appear in it as themselves.
ENV_LINE_BREAKERS = "\r\n\x00\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


def upsert_env_value(env_path: Path, key: str, value: str) -> None:
    """Set `key=value` in `.env`, atomically and with no readable window.

    A LINE-SPLITTING character in `value`, or a NUL, is REFUSED; the full set is
    `ENV_LINE_BREAKERS` above. The wizard's value arrived as JSON on stdin, where
    `"\\n"` is an ordinary character, and this function wrote `f"{key}={value}"`
    verbatim until 2026-08-23: one paste with a trailing newline, or a
    multi-line PEM, split into extra `KEY=...` lines that silently defined
    variables nobody asked for and corrupted the file for every later reader.
    Refusing is right here rather than escaping, because a secret with an
    embedded newline is far more likely a paste accident than an intent.
    """
    if any(ch in value for ch in ENV_LINE_BREAKERS):
        raise ValueError(
            f"the value for {key} contains a line break or NUL. A .env line "
            "cannot carry one: it would split into extra KEY=... lines. Strip "
            "it and pass the value again."
        )
    with _locked(env_path):
        lines = []
        if env_path.exists():
            try:
                lines = env_path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError as e:
                # The path goes in the message: the bare error names the byte and
                # its offset and never the file it came from.
                raise ValueError(
                    f"{env_path} is not valid UTF-8 ({e.reason} at byte {e.start})."
                ) from e
        new_line = _env_line(key, value)
        updated = False
        for i, line in enumerate(lines):
            # `parse_env_line`, not `startswith`: the reader side strips the line
            # first, so `  KEY=old` assigns KEY to every reader in the workspace and
            # was invisible to this writer. The secret was appended as a second
            # line, and `load_env` (setdefault, FIRST line wins) then handed every
            # caller the OLD value while the wizard reported the new one written.
            pair = parse_env_line(line)
            if pair is not None and pair[0] == key:
                lines[i] = new_line
                updated = True
                break
        if not updated:
            lines.append(new_line)
        _replace_atomically(env_path, "\n".join(lines) + "\n")


def _env_line(key: str, value: str) -> str:
    """The line that reads back as exactly `value` through `parse_env_line`.

    The reader strips the value and removes one matching pair of quotes, so a
    secret with a leading or trailing space, or one that starts and ends with
    the same quote character, came back changed when written bare (HOS-302
    review, 2026-11-01). Such a value is wrapped in one pair of double quotes,
    which the reader removes and nothing else. The read-back check refuses any
    value this still cannot carry, rather than writing it changed.
    """
    bare = f"{key}={value}"
    line = bare if parse_env_line(bare) == (key, value) else f'{key}="{value}"'
    if parse_env_line(line) != (key, value):
        raise ValueError(f"the value for {key} cannot be written to .env so that it reads back unchanged.")
    return line


@contextlib.contextmanager
def _locked(env_path: Path):
    """An exclusive lock beside `.env` for the whole read-modify-write.

    Two writers used to read the same `.env`, each add its own line, and the
    second rename dropped the first one's update (HOS-302 review, 2026-11-01).
    The lock file holds no data; `.gitignore` covers it with `.env.*`.
    `O_NOFOLLOW`: a symlink planted at the lock name is refused, never opened.
    """
    lock_path = env_path.with_name(env_path.name + ".lock")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _replace_atomically(env_path: Path, body: str) -> None:
    """Write `body` to a new private file beside `.env`, then rename it over `.env`.

    A unique name from `mkstemp`, never a fixed `.env.tmp` (HOS-302 review,
    2026-11-01). The fixed name was opened with `O_CREAT | O_TRUNC`, which
    follows a symlink: one planted at `.env.tmp` had its target truncated and
    overwritten with the secrets, and the rename then installed the symlink
    itself as `.env`. `mkstemp` creates with `O_EXCL`, so an existing name, a
    symlink included, is never reused, and it creates at mode 600, so the file
    is private before its first byte.

    The history this replaces: until 2026-09-01 the temp file was created at
    umask defaults and chmodded after the write, and until 2026-09-02 a write
    that died partway left the temp file, holding the credential, on disk. The
    `finally` still removes it on any failure.

    POSIX only. HEADING OS runs on Linux, macOS and WSL; the native Windows
    `icacls` branch that stood here was unreachable on all three and was
    removed with this change.
    """
    fd, tmp_name = tempfile.mkstemp(prefix=env_path.name + ".", suffix=".tmp", dir=env_path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(body)
        os.replace(tmp, env_path)
    finally:
        tmp.unlink(missing_ok=True)
