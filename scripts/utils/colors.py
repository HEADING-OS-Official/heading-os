"""ANSI terminal color constants.

The names below are NOT unconditional escape strings. They resolve once, at
import, to the escape when `supports_ansi()` says colour renders and to the
empty string when it does not. That is what makes the switch reach a caller who
never asks the question, and MEASURED 2026-09-21 on this branch that is all of
them: 165 files under `scripts/` import this module and 0 call `supports_ansi()`,
so before this change `TERM=dumb` was inert for every one of them and
`scripts/check-data-path-references.py --ratchet` wrote 1363 escape sequences
into a redirected file.

`tests/test_colors.py` used to pin the opposite, on the argument that a guard
inside the constants would silently disarm every caller. The argument is
answered rather than ignored: disarming now happens only when the operator's own
environment asks for it, by `NO_COLOR` or `TERM=dumb`, and under an ordinary
`TERM` every caller still colours exactly as it did.

**The cost, and it is the whole risk of this shape.** A module-level constant
now depends on the environment AT IMPORT. Changing `NO_COLOR` or `TERM` inside a
running process does not move `GREEN`; only `supports_ansi()` re-reads. Nothing
in this tree toggles colour at runtime (MEASURED 2026-09-21: the only in-process
writer of either name is `tests/`, and `scripts/audit-deps.py` sets `NO_COLOR`
in a subprocess env dict rather than in `os.environ`), so the trade is paid by
tests alone, which reload the module through the `reload_colors` fixture.
"""

import os


def supports_ansi() -> bool:
    """Whether the surrounding terminal renders escape codes.

    Deliberately NOT an `isatty()` check. The callers that need this write to a
    pipe rather than to a terminal - a Claude Code hook emits JSON on stdout and
    the TUI renders it - so `isatty()` answers False on exactly the surface where
    colour does work. The environment is the honest signal instead.

    `NO_COLOR` is read first, added 2026-09-21. It is the one variable that
    exists for exactly this, and the informal standard is about PRESENCE: any
    non-empty value means "do not add colour", whatever the value says. An empty
    `NO_COLOR=` is the documented no-op and is ignored here, which is why the
    test carries that case too. This workspace already demands the courtesy from
    other people's programs - `data_subprocess_env()` in `scripts/audit-deps.py`
    sets `NO_COLOR=1` on any child whose stdout is DATA, after `FORCE_COLOR`
    leaked an escape byte into a requirements file and pip-audit refused to read
    line 1 - and until this line it did not extend it to its own callers.

    `TERM=dumb` is honoured on EVERY platform. It used to be read only inside
    the `os.name == "nt"` branch, i.e. only where `TERM` is almost never set,
    and ignored on POSIX, where `TERM=dumb` is the one place it actually occurs:
    cron, an Emacs shell buffer, a minimal CI container, `env -i`. MEASURED
    2026-08-30 on Linux, `TERM=dumb` returned True, so a caller gating on this
    still emitted raw escape sequences into a log. A docstring arguing that the
    environment is the honest signal, over a branch that never reads the
    environment, is the contradiction; reading the one variable that means "do
    not" is what makes the sentence true.

    Everything else on POSIX stays unconditional, and that IS deliberate - see
    the paragraph above. The Claude Code TUI sets `TERM` to `xterm-256color` or
    similar, so it is unaffected.

    `.claude/hooks/checkpoint-statusline.py` calls this too since 2026-10-16;
    its private copy, which ignored `NO_COLOR`, is gone.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    for var in ("WT_SESSION", "TERM_PROGRAM", "ANSICON", "ConEmuANSI"):
        if os.environ.get(var):
            return True
    return bool(os.environ.get("TERM", ""))


_ON = supports_ansi()

GREEN = "\033[92m" if _ON else ""
YELLOW = "\033[93m" if _ON else ""
RED = "\033[91m" if _ON else ""
CYAN = "\033[96m" if _ON else ""
GRAY = "\033[90m" if _ON else ""
BOLD = "\033[1m" if _ON else ""
RESET = "\033[0m" if _ON else ""
