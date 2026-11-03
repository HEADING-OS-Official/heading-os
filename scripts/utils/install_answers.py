#!/usr/bin/env python3
"""The install's answers, read from the engine's `.env`: the one accessor (HOS-312).

The intake of `scripts/heading-install.py` asks three questions before it
installs anything (a fourth, the Claude sign-in for the model proxy, is never
asked and comes from a flag or the answers file) and keeps each answer as one `HEADING_OS_*` line of the
engine's `.env`, the one store. Every component that depends on a choice reads
it here and keeps no default of its own (the install and setup design approved
2026-11-03 on HOS-312, section 3.2):

  HEADING_OS_MAIL            exchange, google or none (connect later)
  HEADING_OS_GITHUB_BACKUP   yes or later
  HEADING_OS_TZ              an IANA zone this Python can load
  HEADING_OS_PROXY_SIGNIN    now or later (the Claude sign-in for the model proxy)
  HEADING_OS_INSTALL_CONSENT all, or the consent groups agreed to, comma separated
                             (HOS-340; never asked here, and a "no" is never stored)

Each function returns the value, or None when it is unset or not a valid value.
With `environ=True` (the doctor, the watchdog) the process environment is read
first, then the file, the order `_setting` in scripts/doctor.py uses. With
`environ=False` (the installer, for filling and for every check) only the file,
so an exported variable never stands in for what the install wrote.

`read_answers_file` reads the file `heading-install.py --answers` takes (and
`test-vm.py install` checks before it copies one): a JSON object of strings
under the keys of ANSWERS_FILE_KEYS, readable by its owner only. Every refusal
is an AnswersFileError whose message names the key or the reason, never a value.

Usage:
    from scripts.utils import install_answers
    install_answers.mail(engine_root)                    # "exchange" | "google" | "none" | None
    install_answers.timezone(engine_root, environ=False)

Standard library only and importing no engine module: heading-install.py
imports it before the engine's environment exists, and scripts/utils/install_mail.py
imports scripts.doctor, which imports this module, so any engine import here
could close a cycle.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAIL_ENV = "HEADING_OS_MAIL"
GITHUB_BACKUP_ENV = "HEADING_OS_GITHUB_BACKUP"
TZ_ENV = "HEADING_OS_TZ"
PROXY_SIGNIN_ENV = "HEADING_OS_PROXY_SIGNIN"
MAIL_CHOICES = ("exchange", "google", "none")
GITHUB_BACKUP_CHOICES = ("yes", "later")
PROXY_SIGNIN_CHOICES = ("now", "later")
CONSENT_ENV = "HEADING_OS_INSTALL_CONSENT"
#: The four kinds of change the person agrees to before any is made, in the order
#: they are asked: name to the words every question uses (HOS-340). install.sh
#: holds the same four strings, because it asks before this file exists on disk.
CONSENT_GROUPS = {
    "system": "installing system packages with your computer password",
    "home": "putting files in your home folder",
    "downloads": "downloading from the internet",
    "services": "running programs and timers in the background",
}

_log = logging.getLogger(__name__)
_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def valid_zone(name: str | None) -> bool:
    """True when `name`, exactly as given, is an IANA zone this Python can load.

    Never stripped first: a reader passes the stored value to ZoneInfo as it
    is, so a value valid only after stripping is not valid (HOS-309 review round 1).
    """
    if not name or name != name.strip():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _file_value(engine: Path, name: str) -> str:
    """The FIRST value `name` is assigned in `<engine>/.env`, or "".

    The grammar of `parse_env_line` in scripts/utils/paths.py, restated because
    this module imports no engine module: a stripped line, an optional `export `,
    one matching pair of quotes removed. Fail-soft like `read_env_value` there:
    a missing or unreadable file reads as unset.
    """
    try:
        text = (Path(engine) / ".env").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug(".env unreadable under %s: %s", engine, exc)
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key[:6] == "export" and key[6:7] in (" ", "\t"):
            key = key[6:].strip()
        if not _ENV_NAME_RE.match(key) or key != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value
    return ""


def _raw(engine: Path, name: str, environ: bool) -> str:
    if environ and os.environ.get(name):
        return os.environ[name]
    return _file_value(engine, name)


def mail(engine: Path, *, environ: bool = True) -> str | None:
    """`HEADING_OS_MAIL`: one of MAIL_CHOICES, else None."""
    value = _raw(engine, MAIL_ENV, environ)
    return value if value in MAIL_CHOICES else None


def github_backup(engine: Path, *, environ: bool = True) -> str | None:
    """`HEADING_OS_GITHUB_BACKUP`: "yes" or "later", else None."""
    value = _raw(engine, GITHUB_BACKUP_ENV, environ)
    return value if value in GITHUB_BACKUP_CHOICES else None


def proxy_signin(engine: Path, *, environ: bool = True) -> str | None:
    """`HEADING_OS_PROXY_SIGNIN`: "now" or "later", else None."""
    value = _raw(engine, PROXY_SIGNIN_ENV, environ)
    return value if value in PROXY_SIGNIN_CHOICES else None


def timezone(engine: Path, *, environ: bool = True) -> str | None:
    """`HEADING_OS_TZ`: a zone `valid_zone` accepts, else None."""
    value = _raw(engine, TZ_ENV, environ)
    return value if valid_zone(value) else None


def valid_consent(value: str | None) -> bool:
    """True for `all`, `no`, or a comma list of distinct names from CONSENT_GROUPS."""
    if value in ("all", "no"):
        return True
    names = value.split(",") if value else []
    return bool(names) and len(set(names)) == len(names) and all(n in CONSENT_GROUPS for n in names)


def consent(engine: Path, *, environ: bool = True) -> str | None:
    """`HEADING_OS_INSTALL_CONSENT`: a value `valid_consent` accepts, else None."""
    value = _raw(engine, CONSENT_ENV, environ)
    return value if valid_consent(value) else None


#: The keys an answers file may hold, each with its accepted values (None: a zone `valid_zone` accepts;
#: a function: the values it accepts).
ANSWERS_FILE_KEYS = {"mail": MAIL_CHOICES, "github_backup": GITHUB_BACKUP_CHOICES, "tz": None,
                     "proxy_signin": PROXY_SIGNIN_CHOICES, "consent": valid_consent}


class AnswersFileError(ValueError):
    """The answers file is refused; the message is the sentence the person reads, holding no value."""


def read_answers_file(path: Path) -> dict[str, str]:
    """The answers in `path`, checked; raise AnswersFileError on any refusal.

    Opened with O_NOFOLLOW, so a symbolic link is refused rather than followed
    (and O_NONBLOCK, so a FIFO is refused rather than waited on),
    and the type and mode are read by fstat on that same descriptor, so the file
    checked is the file read. Anything but a regular file is refused, and so is
    group or other access of any kind.
    """
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO would block the open
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AnswersFileError(f"The answers file {path} is not a regular file.")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise AnswersFileError(f"The answers file {path} can be read by others; run chmod 600 on it.")
        handle, fd = os.fdopen(fd, "rb"), None  # the handle owns the descriptor from here
        with handle:
            raw = handle.read()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AnswersFileError(f"The answers file {path} is a symbolic link; give the file itself.") from exc
        raise AnswersFileError(f"The answers file {path} cannot be read: {exc.strerror}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AnswersFileError(f"The answers file {path} is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise AnswersFileError(f"The answers file {path} does not hold a JSON object.")
    for key, value in data.items():
        if key not in ANSWERS_FILE_KEYS:
            raise AnswersFileError(f"The answers file names an unknown answer {key}.")
        if not isinstance(value, str):
            raise AnswersFileError(f"The answers file's {key} is not text.")
        choices = ANSWERS_FILE_KEYS[key]
        if not (valid_zone(value) if choices is None else
                choices(value) if callable(choices) else value in choices):
            raise AnswersFileError(f"The answers file's {key} is not one of the accepted answers.")
    return data
