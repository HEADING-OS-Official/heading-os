#!/usr/bin/env python3
"""Read-only console critic for a queued outbound draft (R5b, console-first rule).

Prints an advisory critique of an email draft - either an Action Queue card,
read in-process from the queue file exactly as ``action-queue.py`` reads it, or
an ad-hoc draft from a file. This is the terminal path for the R5b pre-approval
critique. It is strictly ADVISORY: it never stamps the queue, never approves,
never dismisses, never sends. The one write reading the queue can cause is the
queue module's own quarantine of a file it cannot parse, the same as
``action-queue.py list``.

The card path reads the queue file in-process, as `action-queue.py` does
(HOS-168).

Usage:
    python scripts/draft-critique.py <id-or-prefix>          # critique a queued card
    python scripts/draft-critique.py --body-file draft.txt [--subject S] [--to ADDR]
    python scripts/draft-critique.py <id> --json             # machine-readable output

Exit codes:
    0  critique produced.
    1  usage error (including an argparse parse error, which `_Parser` below
       remaps from argparse's stock 2); no critique produced (model
       unavailable, missing API key, empty body); no matching card, an
       ambiguous prefix, an unreadable ``--body-file``, or a card that carries
       no draft body.
    2  the queue store is ambiguous (``store_address`` refused to pick one).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import draft_critique
from scripts.utils.colors import BOLD, GRAY, GREEN, RED, RESET, YELLOW
from scripts.utils.workspace import get_data_root
from scripts.record_readers.sources.action_queue import list_action_queue, store_address
from scripts.utils.runtime_junk_moves import AmbiguousStore

_RISK_COLOR = {"high": RED, "medium": YELLOW, "low": GREEN}


class _Parser(argparse.ArgumentParser):
    """An argparse parse error exits 1, not argparse's stock 2.

    This module reserves 2 for an ambiguous queue store, and `main` already
    answers its OWN usage errors with 1 ("pass a card id-or-prefix, or
    --body-file"). Stock `ArgumentParser.error()` calls `sys.exit(2)`, so one
    kind of usage error would exit 1 and another 2, colliding with the store
    code. Overriding `error` is the whole fix; `-h/--help` goes through
    `parser.exit(0)` and is untouched.
    """

    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"{RED}usage error{RESET}: {message}", file=sys.stderr)
        sys.exit(1)


def _fetch_card(prefix: str) -> dict:
    """The active card whose id is ``prefix`` or uniquely starts with it."""
    data_root = get_data_root()
    try:
        store_address(data_root)
    except AmbiguousStore as exc:
        print(f"{RED}REFUSED{RESET}: {exc}", file=sys.stderr)
        sys.exit(2)
    data = list_action_queue(data_root)
    items = [c for c in data.get("items", []) if isinstance(c, dict)]
    exact = [c for c in items if c.get("id") == prefix]
    matches = exact or [c for c in items if str(c.get("id", "")).startswith(prefix)]
    if not matches:
        print(f"{RED}no active card matches '{prefix}'{RESET}", file=sys.stderr)
        sys.exit(1)
    if len(matches) > 1:
        print(f"{RED}ambiguous prefix '{prefix}' ({len(matches)} matches){RESET}", file=sys.stderr)
        sys.exit(1)
    return matches[0]


def _print_human(card_id: str | None, result: dict) -> None:
    risk = result.get("risk", "medium")
    rc = _RISK_COLOR.get(risk, YELLOW)
    head = f"{BOLD}critique{RESET}"
    if card_id:
        head += f" {GRAY}{card_id[:8]}{RESET}"
    print(f"{head}  risk={rc}{risk}{RESET}  {GRAY}model={result.get('model', '?')}{RESET}")
    summary = result.get("summary", "")
    if summary:
        print(f"  {summary}")
    flags = result.get("flags", [])
    if flags:
        for f in flags:
            print(f"  {rc}-{RESET} {f}")
    else:
        print(f"  {GRAY}(no flags raised){RESET}")


def main() -> int:
    ap = _Parser(description="Read-only advisory critique of an outbound draft.")
    ap.add_argument("id", nargs="?", help="Action Queue card id or unique prefix")
    ap.add_argument("--body-file", help="critique an ad-hoc draft from this file")
    ap.add_argument("--subject", default=None, help="subject for --body-file mode")
    ap.add_argument("--to", default=None, help="recipient address for --body-file mode")
    ap.add_argument("--model", default=None, help="model alias/id override (default haiku-class)")
    ap.add_argument("--json", action="store_true", help="emit the critique as JSON")
    args = ap.parse_args()

    if not args.id and not args.body_file:
        print(f"{RED}pass a card id-or-prefix, or --body-file{RESET}", file=sys.stderr)
        return 1
    if args.id and args.body_file:
        print(f"{RED}pass either a card id OR --body-file, not both{RESET}", file=sys.stderr)
        return 1

    card_id = None
    if args.body_file:
        try:
            body = Path(args.body_file).read_text(encoding="utf-8")
        # MEASURED 2026-09-01 with `printf 'hello \xff world\n'`:
        # `UnicodeDecodeError: invalid start byte` out of `read_text`, past a
        # handler that names only `OSError`, and out of `main` as a traceback.
        # The file is NAMED, because a draft that cannot be decoded is a file
        # the operator has to go and look at.
        except (OSError, UnicodeDecodeError) as e:
            print(f"{RED}cannot read --body-file{RESET} {args.body_file}: {e}",
                  file=sys.stderr)
            return 1
        subject, recipient = args.subject, args.to
    else:
        card = _fetch_card(args.id)
        card_id = card.get("id")
        # Named here, not left to the catch-all below. A card that carries no
        # draft (a `note`, an `alert`, a `pipeline_update`) resolves by id just
        # like an email one, and then fell out at "model unavailable, missing
        # API key, or empty draft body" - three causes, none of them the real
        # one, sending the operator to check an API key over a card that was
        # never a draft.
        if not card.get("draft_body"):
            print(f"{YELLOW}card {str(card_id)[:8]} carries no draft body{RESET} "
                  f"(action_type={card.get('action_type', '?')}). There is nothing "
                  f"to critique.", file=sys.stderr)
            return 1
        subject = card.get("subject")
        body = card.get("draft_body")
        recipient = card.get("to")  # recipient lives in the card's `to` field

    result = draft_critique.critique_draft(subject, body, recipient, model=args.model)
    if result is None:
        print(f"{YELLOW}no critique produced{RESET} - model unavailable, missing API key, "
              f"or empty draft body.", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_human(card_id, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
