#!/usr/bin/env python3
"""
grok-consult.py - Council second-opinion API wrapper.

Calls Grok through the local CLIProxyAPI proxy seam (scripts/utils/proxy_transport)
and returns the response on stdout. Pure API wrapper - no disk writes, no
orchestration logic. The /council skill handles inputs, output formatting,
and transcript persistence.

Transport, auth, retry, and error classification all live in proxy_transport.call_model;
this module is a thin delegate that owns only the Grok-specific prompt/CLI surface.

Update DEFAULT_MODEL when xAI ships a new flagship model (via
config/council-models.json, see scripts/council-models.py --set).

Usage:
  python scripts/grok-consult.py --mode independent --question "..." [--context "..."]
  python scripts/grok-consult.py --mode critique    --draft "..."    [--context "..."]
  python scripts/grok-consult.py --mode independent --question "..." --model grok-4.5

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
# pin with: python scripts/council-models.py --set grok=<new>. Falls back to the
# baseline in scripts/utils/council_models.py if the config is missing.
DEFAULT_MODEL = get_model("grok")


# ============================================================
# Grok API call
# ============================================================

def consult_grok(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: Optional[float] = None,
) -> str:
    """Send the council prompt to Grok through the proxy; return the answer text.

    `timeout` overrides the proxy socket timeout (seconds). Leave None to inherit
    proxy_transport.DEFAULT_TIMEOUT. grok-4.5 is a thinking model, so a large
    critique can exceed the default in the reasoning phase, and the truncation
    error the transport raises tells the operator to raise this exact flag.
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
        argv, prog="grok-consult.py", voice="Grok",
        default_model=DEFAULT_MODEL, timeout_subject="grok-4.5 critique")


def main(argv: Optional[list[str]] = None) -> int:
    """Build the prompt for the requested mode, call Grok, print to stdout."""
    return council_cli.main(argv, parse_args, consult_grok, with_reasoning=False)


if __name__ == "__main__":
    sys.exit(main())
