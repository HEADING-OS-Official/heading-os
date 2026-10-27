#!/usr/bin/env python3
"""Pin an item verbatim for this session: "закрепи: ..." or `/pin ...`.

A pin is a sentence the operator wants carried word for word across compaction
and /clear. It is appended to this session's pins file in the handoff runtime
state, beside the session's pointer pair, and from there it reaches two
places: every handoff the session writes, and what the SessionStart hook
injects after /clear, compact and resume.
Operator decision 2026-09-27, the handover plan's "Yard 2", item P2.

The text is kept as given. Whitespace collapses to single spaces, so one pin is
one line; nothing else is changed, reworded or translated. A pin that the
secret redactor would alter is refused rather than stored altered, because the
pins file is tracked in the data repository and a stored pin must be the
operator's words or nothing.

Usage:
    python scripts/pin.py "Brave stays untouched"
    python scripts/pin.py <<'PIN'
    text with `backticks` passes through a quoted heredoc untouched
    PIN
    python scripts/pin.py --list
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import checkpoint_paths as CP  # noqa: E402


def pins_file() -> Path:
    root = CP.engine_root()
    return CP.pins_path(CP.handoff_dir(CP.project_root(), root), CP.session_slug(), root)


def main(argv=None) -> int:
    CP.force_utf8()
    ap = argparse.ArgumentParser(description="Pin an item verbatim for this session.")
    ap.add_argument("text", nargs="*", help="the item; read from stdin when absent")
    ap.add_argument("--list", action="store_true", help="print this session's pins")
    args = ap.parse_args(argv)

    if not CP.session_id_is_known():
        print("pin: refused. No session id reached this command "
              "(CLAUDE_CODE_SESSION_ID is unset), so there is no session to pin to.",
              file=sys.stderr)
        return 1
    path = pins_file()

    if args.list:
        pins = CP.read_pins(path)
        print(f"{len(pins)} pin(s) in {path}")
        for pin in pins:
            print(f"- {pin}")
        return 0

    raw = " ".join(args.text) if args.text else sys.stdin.read()
    pin = CP.normalise_pin(raw)
    if not pin:
        print("pin: refused. The item is empty.", file=sys.stderr)
        return 1
    if len(pin) > CP.MAX_PIN_CHARS:
        print(f"pin: refused. The item is {len(pin)} characters and a pin holds "
              f"{CP.MAX_PIN_CHARS} at most; it is not cut, because a cut pin is "
              f"not verbatim. Pin it in shorter parts.", file=sys.stderr)
        return 1
    try:
        from scripts.utils.secret_patterns import redact
        clean = redact(pin)
    except Exception as exc:  # noqa: BLE001 - fail closed: the file is tracked
        print(f"pin: refused. The secret redactor is unavailable "
              f"({type(exc).__name__}: {exc}), and the pins file is tracked.",
              file=sys.stderr)
        return 1
    if clean != pin:
        print("pin: refused. The item looks like it holds a credential, and the "
              "pins file is tracked in the data repository. Keep the secret in "
              ".env and pin a sentence that names it instead.", file=sys.stderr)
        return 1

    # The writer initialises the runtime store (0700 root, README), as the
    # post-compact writer does; readers resolve the same path without it.
    CP.runtime_state_root(CP.engine_root(), create=True)
    added = CP.append_pins(path, [pin])
    total = len(CP.read_pins(path))
    if added:
        print(f"pinned ({total} in this session): {pin}")
    else:
        print(f"already pinned ({total} in this session): {pin}")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
