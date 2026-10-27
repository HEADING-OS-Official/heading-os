#!/usr/bin/env python3
"""
kimi-consult.py - Council second-opinion API wrapper.

Calls Kimi through the local CLIProxyAPI proxy seam (scripts/utils/proxy_transport)
and returns the response on stdout. Pure API wrapper - no disk writes, no
orchestration logic. The /council skill handles inputs, output formatting, and
transcript persistence.

Transport, auth, retry, and error classification all live in proxy_transport.call_model;
this module is a thin delegate that owns only the Kimi-specific prompt/CLI surface.

Update DEFAULT_MODEL when Moonshot ships a new flagship Kimi variant (via
config/council-models.json, see scripts/council-models.py --set).

The prompt is council-shaped: it carries the company context block and, by default,
asks for 200-400 words. For an ENUMERATING task ("list every defect in this
file") pass `--length-hint ""` to drop that cap, and raise `--max-tokens` and
`--timeout` together so the answer is not cut off by the budget instead.

Usage:
  python scripts/kimi-consult.py --mode independent --question "..." [--context "..."]
  python scripts/kimi-consult.py --mode critique    --draft "..."    [--context "..."]
  python scripts/kimi-consult.py --mode independent --question "..." --model kimi-for-coding
  python scripts/kimi-consult.py --mode independent --question "list every defect" \
      --context "$(cat target.py)" --length-hint "" --max-tokens 32768 --timeout 900

The parser and the exit-code contract (0 ok, 2 argument error or missing key,
3 API call failed) are shared with the other wrappers in
`scripts/lib/council_cli.py`; this file owns only the voice-specific call.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# Workspace imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.lib import council_cli  # noqa: E402
from scripts.lib.council_cli import (  # noqa: E402
    DEFAULT_CRITIQUE_TEMPERATURE,  # noqa: F401 - part of this module's surface
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
)
from scripts.utils.council_models import get_model  # noqa: E402
from scripts.utils.proxy_transport import call_model  # noqa: E402

# ============================================================
# Configuration
# ============================================================

# Resolved from config/council-models.json (single source of truth). Bump the
# pin with: python scripts/council-models.py --set kimi=<new>. Falls back to the
# baseline in scripts/utils/council_models.py if the config is missing.
DEFAULT_MODEL = get_model("kimi")


# ============================================================
# Kimi API call
# ============================================================

def consult_kimi(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    reasoning_effort: Optional[str] = None,
    timeout: Optional[float] = None,
) -> str:
    """Send the council prompt to Kimi through the proxy; return the answer text.

    `timeout` overrides the proxy socket timeout (seconds). Leave None to inherit
    proxy_transport.DEFAULT_TIMEOUT. The k3 reasoning voice on a large draft can
    exceed the default, so long critiques should pass a higher value.
    """
    kwargs = {"temperature": temperature, "max_tokens": max_tokens,
              "reasoning_effort": reasoning_effort}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return call_model(model, prompt, **kwargs)


# ============================================================
# CLI entry point
# ============================================================

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments and validate mode-specific requirements."""
    return council_cli.parse_args(
        argv, prog="kimi-consult.py", voice="Kimi",
        default_model=DEFAULT_MODEL, timeout_subject="k3 critique",
        reasoning_efforts=["low", "high", "max"],
        reasoning_help="k3 thinking effort (proxy request param); omit for kimi-for-coding")


def main(argv: Optional[list[str]] = None) -> int:
    """Build the prompt for the requested mode, call Kimi, print to stdout."""
    return council_cli.main(argv, parse_args, consult_kimi, with_reasoning=True)


if __name__ == "__main__":
    sys.exit(main())
