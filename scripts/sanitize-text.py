#!/usr/bin/env python3
"""
sanitize-text.py - CLI wrapper around scripts.utils.sanitize_text.

Library logic lives in scripts/utils/sanitize_text.py. This CLI is run on
demand from rules and skills, and as a subprocess by render-doctype.py,
artifact-evaluator.py, inbox-pulse-report.py and convert-to-md.py; no hook and
no pre-commit entry calls it.

Usage:
  python sanitize-text.py <file>              # Sanitize a file in place
  python sanitize-text.py <file> -o <output>  # Sanitize to a new file
  python sanitize-text.py --scan <file>       # Scan and report hidden chars (no changes)
  python sanitize-text.py --text "string"     # Sanitize inline text (prints to stdout)
  python sanitize-text.py --scan --text "str" # Scan inline text for hidden chars
  echo "text" | python sanitize-text.py -     # Sanitize from stdin
"""

import argparse
import sys
from pathlib import Path

# Workspace import boilerplate
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.atomic import atomic_write_text
from scripts.utils.sanitize_text import sanitize_report, scan, word_count


def main():
    parser = argparse.ArgumentParser(
        description="Strip invisible Unicode characters from AI-generated text."
    )
    parser.add_argument("file", nargs="?", help="File to sanitize (use '-' for stdin)")
    parser.add_argument("-o", "--output", help="Output file (default: overwrite input)")
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan and report hidden characters without modifying"
    )
    parser.add_argument(
        "--text", help="Inline text to sanitize or scan (instead of a file)"
    )
    args = parser.parse_args()

    if not args.text and not args.file:
        parser.error("either a file or --text is required")

    # `-o` is read only on the write-a-file path; with --text, stdin or --scan
    # it would be silently ignored and no file written (measured 2026-08-26).
    if args.output and (args.text or args.file == "-" or args.scan):
        parser.error("-o/--output writes the sanitized FILE back; it does nothing "
                     "for --scan, --text or stdin, which only print")

    if args.text:
        text = args.text
        source = "inline text"
    elif args.file == "-":
        text = sys.stdin.read()
        source = "stdin"
    else:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as exc:
            # Exit 2 means "never scanned"; exit 1 is reserved for "hidden
            # characters found", which a non-UTF-8 file used to fake (2026-09-01).
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
        source = args.file

    if args.scan:
        count = scan(text, source)
        # The word count belongs here because `.claude/rules/hidden-chars.md`
        # requires every deliverable to carry "Word count: X. Hidden characters:
        # clean." and nothing computed the X -- so the number was estimated by
        # whoever wrote the line. A validation line with a guessed figure in it
        # is the exact over-claim `.claude/rules/scope-claims.md` forbids.
        print(f"  Word count: {word_count(text)}", file=sys.stderr)
        sys.exit(1 if count > 0 else 0)

    clean, removed, replaced = sanitize_report(text)

    if args.text or args.file == "-":
        sys.stdout.write(clean)
    else:
        output_path = args.output or args.file
        # Atomic: an interrupted in-place write must not destroy the original.
        atomic_write_text(Path(output_path), clean)

    # Both numbers, because the file is rewritten for either one. The old line
    # reported deletions only, so a replaced non-breaking space was a silent
    # rewrite under the word "clean".
    if removed or replaced:
        parts = []
        if removed:
            parts.append(f"removed {removed}")
        if replaced:
            parts.append(f"replaced {replaced}")
        print(f"  {source}: {', '.join(parts)} hidden character(s)", file=sys.stderr)
    else:
        print(f"  {source}: already clean", file=sys.stderr)


if __name__ == "__main__":
    # sys.exit(main()): callers read the exit code as the scan verdict, so the
    # `return 2` for an unreadable file must reach the process exit (2026-08-25).
    sys.exit(main())
