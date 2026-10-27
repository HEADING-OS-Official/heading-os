"""The shared command line of the council consult wrappers.

`scripts/kimi-consult.py`, `scripts/grok-consult.py` and `scripts/gemini-consult.py`
were the same file three times over, differing only in the voice's name, its
model pin and whether it takes a reasoning effort, and a fix kept reaching two
of the three copies. Each wrapper now keeps its own `consult_<voice>()` (which
`scripts/utils/llm_fallback.py` loads by name) and hands this module the rest:
the parser, the per-mode temperature default, the prompt build and the 0/2/3
exit-code contract the /council skill is written against.

Exit codes of every wrapper:
  0  success, response printed to stdout
  2  argument error or missing API key (argparse + custom validation share this code)
  3  API call failed (network, rate limit, invalid model, etc.)
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional

from scripts.utils.colors import RED, RESET
from scripts.utils.council_prompts import (
    DEFAULT_LENGTH_HINT,
    build_critique_prompt,
    build_independent_prompt,
)

DEFAULT_TEMPERATURE = 0.7              # Independent mode: room to reason creatively
DEFAULT_CRITIQUE_TEMPERATURE = 0.4     # Critique mode: more deterministic, less paraphrasing
DEFAULT_MAX_TOKENS = 8192


def parse_args(argv: Optional[list[str]], *, prog: str, voice: str,
               default_model: str, timeout_subject: str,
               reasoning_efforts: Optional[list[str]] = None,
               reasoning_help: str = "") -> argparse.Namespace:
    """Parse one wrapper's CLI and validate mode-specific requirements.

    `voice` is the display name ("Kimi"); `timeout_subject` names what a large
    call is ("k3 critique"); `reasoning_efforts`, when given, adds
    `--reasoning-effort` with those choices.
    """
    p = argparse.ArgumentParser(
        prog=prog,
        description=f"Council: consult {voice} for an independent second opinion.",
    )
    p.add_argument(
        "--mode",
        choices=["independent", "critique"],
        required=True,
        help=f"independent: {voice} reasons fresh from the question. "
             f"critique: {voice} stress-tests a provided draft.",
    )
    p.add_argument("--question", default="",
                   help="The question to consult on (independent mode).")
    p.add_argument("--draft", default="",
                   help="The draft to critique (critique mode).")
    p.add_argument("--context", default="",
                   help="Additional context for either mode.")
    p.add_argument(
        "--length-hint",
        default=DEFAULT_LENGTH_HINT,
        help="Closing length instruction appended to the Output section. "
             f"Default: {DEFAULT_LENGTH_HINT!r}. Pass an empty string to omit it "
             "for an enumerating task (\"list every defect\") that must not be "
             "capped at a word count.",
    )
    p.add_argument("--model", default=default_model,
                   help=f"{voice} model. Default: {default_model}")
    p.add_argument(
        "--temperature",
        type=float,
        default=None,
        help=(
            f"Sampling temperature. Default: {DEFAULT_TEMPERATURE} for independent mode, "
            f"{DEFAULT_CRITIQUE_TEMPERATURE} for critique mode (more deterministic)."
        ),
    )
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                   help=f"Max output tokens. Default: {DEFAULT_MAX_TOKENS}")
    if reasoning_efforts:
        p.add_argument("--reasoning-effort", choices=reasoning_efforts,
                       default=None, help=reasoning_help)
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Socket timeout in seconds for ONE call. Omit to inherit the transport "
             f"default. Raise it (e.g. 480) for a large {timeout_subject} that would "
             "otherwise time out. A truncation retry makes a second call at up to "
             "twice this value, so the worst-case wall time is about 3x what you pass.",
    )
    args = p.parse_args(argv)

    if args.mode == "independent" and not args.question.strip():
        p.error("--question is required in independent mode")
    if args.mode == "critique" and not args.draft.strip():
        p.error("--draft is required in critique mode")

    # Per-mode temperature default: critique wants focused fault-finding.
    if args.temperature is None:
        args.temperature = (
            DEFAULT_CRITIQUE_TEMPERATURE if args.mode == "critique" else DEFAULT_TEMPERATURE
        )
    return args


def run(args: argparse.Namespace, consult: Callable[..., str],
        **consult_kwargs) -> int:
    """Build the prompt, call `consult`, print the answer; return the exit code.

    `consult_kwargs` carries what only some voices take (`reasoning_effort`).
    """
    if args.mode == "independent":
        prompt = build_independent_prompt(args.question, args.context,
                                          length_hint=args.length_hint)
    else:
        prompt = build_critique_prompt(args.draft, args.context,
                                       length_hint=args.length_hint)
    try:
        response = consult(prompt, model=args.model, temperature=args.temperature,
                           max_tokens=args.max_tokens, timeout=args.timeout,
                           **consult_kwargs)
    except RuntimeError as e:
        msg = str(e)
        print(f"{RED}Error:{RESET} {msg}", file=sys.stderr)
        # Narrow sentinel: the missing-key path only, NOT auth failures.
        return 2 if "is missing from .env" in msg else 3
    except Exception as e:  # noqa: BLE001 - the exit code IS the contract
        # call_model wraps the API errors it knows into RuntimeError, but not
        # every one; anything else would escape as exit 1, a code the contract
        # does not define. The type is named so the failure stays diagnosable.
        print(f"{RED}Error:{RESET} unexpected {type(e).__name__} from the proxy "
              f"call: {e}", file=sys.stderr)
        return 3
    print(response)
    return 0


def main(argv: Optional[list[str]], parse: Callable[[Optional[list[str]]], argparse.Namespace],
         consult: Callable[..., str], *, with_reasoning: bool = False) -> int:
    """A wrapper's whole `main`: parse, then `run`, mapping argparse's exit."""
    try:
        args = parse(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1
    extra = {"reasoning_effort": args.reasoning_effort} if with_reasoning else {}
    return run(args, consult, **extra)
