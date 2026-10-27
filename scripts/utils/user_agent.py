#!/usr/bin/env python3
"""The one User-Agent every outbound HTTP call in this workspace sends.

Usage:
    from scripts.utils.user_agent import user_agent
    headers = {"User-Agent": user_agent()}

    # shell, skill instructions, anything that is not Python:
    UA="$(.venv/bin/python scripts/utils/user_agent.py)"
    curl -A "$UA" https://example.org/

WHERE THE VALUE LIVES, and why there. `config/user-agent.txt`, one line and
nothing else. Operator instruction 2026-09-27, translated: "make the User Agent
value a constant for the whole workspace, and everywhere (a script, a plugin or
anything else) that talks to the outside world, supply that constant. When it
needs to change, we change it in one place, and the whole workspace uses the new
value at once." A plain text file is the one form Python, a shell
(`cat`, or the CLI below) and a node script can all read without a parser and
without a second copy. There is deliberately no environment override: a
variable is a second place the value can live, and the instruction was one.
`scripts/check-user-agent.py` refuses a literal User-Agent anywhere else in the
engine and an outbound call site that does not read this one.

THE VALUE. Chrome on Windows, chosen by the operator on 2026-09-27 from the
options HELM put to him ("For now, make the User Agent value as
written in No. 3, Chrome on Windows.", translated). It replaces a design, proposed by HELM the
same day and never committed, that sent `heading-os/<version> (<tool>)`.

WHY A VALUE IS SENT AT ALL. urllib's default is "Python-urllib/x.y", and some
services ban that signature outright: Replicate's Cloudflare front answers it
with HTTP 403 "error code: 1010". MEASURED 2026-09-27 against GET /v1/account
with one valid token: no header 403, "Python-urllib/3.11" 403; an empty value,
"curl/8.5.0", "python-requests/2.32.3" and "heading-os/0.9.0 (flux-image)" all
200. gamma-api.polymarket.com answered urllib's default the same way the same
day. The block is on urllib's signature, not on a missing header.

A missing, empty or multi-line file raises rather than falling back: a fallback
is a second value, and urllib's own default is exactly the one that is refused.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[2] / "config" / "user-agent.txt"


class UserAgentConfigError(RuntimeError):
    """`config/user-agent.txt` is missing or does not hold exactly one value."""


@lru_cache(maxsize=1)
def user_agent() -> str:
    """The workspace User-Agent, read once per process from `config/user-agent.txt`."""
    try:
        text = CONFIG.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserAgentConfigError(f"cannot read {CONFIG}: {exc}") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise UserAgentConfigError(
            f"{CONFIG} must hold exactly one non-empty line, found {len(lines)}")
    value = lines[0]
    if not value.isascii() or not value.isprintable():
        raise UserAgentConfigError(f"{CONFIG} holds a value that is not printable ASCII")
    return value


def __getattr__(name: str) -> str:
    # `USER_AGENT` as a module constant, resolved on first access rather than at
    # import, so importing this module never raises.
    if name == "USER_AGENT":
        return user_agent()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    try:
        print(user_agent())
    except UserAgentConfigError as exc:
        print(f"user_agent: {exc}", file=sys.stderr)
        sys.exit(1)
