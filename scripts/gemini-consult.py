#!/usr/bin/env python3
"""
gemini-consult.py - Council second-opinion API wrapper.

Calls Gemini through the local CLIProxyAPI proxy seam (scripts/utils/proxy_transport)
and returns the response on stdout. Pure API wrapper - no disk writes, no
orchestration logic. The /council skill handles inputs, output formatting,
and transcript persistence.

Transport, auth, retry, and error classification all live in proxy_transport.call_model;
this module is a thin delegate that owns only the Gemini-specific prompt/CLI surface.
The direct vendor SDK is no longer used - the proxy fronts the subscription instead.

Update DEFAULT_MODEL when Google ships a new flagship tier (via
config/council-models.json, see scripts/council-models.py --set).

Usage:
  python scripts/gemini-consult.py --mode independent --question "..." [--context "..."]
  python scripts/gemini-consult.py --mode critique    --draft "..."    [--context "..."]
  python scripts/gemini-consult.py --mode independent --question "..." --model gemini-3-flash

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
# pin with: python scripts/council-models.py --set gemini=<new>. Falls back to
# the baseline in scripts/utils/council_models.py if the config is missing.
DEFAULT_MODEL = get_model("gemini")


# ============================================================
# Gemini API call
# ============================================================

def consult_gemini(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: Optional[float] = None,
) -> str:
    """Send the council prompt to Gemini through the proxy; return the answer text.

    `timeout` overrides the proxy socket timeout (seconds). Leave None to inherit
    proxy_transport.DEFAULT_TIMEOUT. The truncation error the transport raises
    tells the operator to raise this exact flag, so every wrapper that can print
    that message has to accept it.
    """
    kwargs = {"temperature": temperature, "max_tokens": max_tokens}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return call_model(model, prompt, **kwargs)


# ============================================================
# CLI entry point
# ============================================================

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments and validate mode-specific requirements."""
    return council_cli.parse_args(
        argv, prog="gemini-consult.py", voice="Gemini",
        default_model=DEFAULT_MODEL, timeout_subject="critique")


def main(argv: Optional[list[str]] = None) -> int:
    """Build the prompt for the requested mode, call Gemini, print to stdout."""
    return council_cli.main(argv, parse_args, consult_gemini, with_reasoning=False)


if __name__ == "__main__":
    sys.exit(main())
