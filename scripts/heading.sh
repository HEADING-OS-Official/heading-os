#!/usr/bin/env bash
# heading: open Claude Code in the HEADING OS engine folder, so its rules and
# hooks load (deployment design § 2 step 10, registry name `plans`).
#
# The engine folder is this script's own checkout, never a constant. The
# command on PATH is a two-line wrapper that scripts/utils/heading_launcher.py
# writes into ~/.local/bin and that execs this file by absolute path; no
# symlink anywhere.
#
# Usage: heading [claude arguments...]
set -euo pipefail

ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolved once and run by absolute path (HOS-312, F5): a terminal opened
# before the install has no ~/.local/bin on PATH.
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
if [ -z "$CLAUDE_BIN" ] && [ -x "$HOME/.local/bin/claude" ]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
fi
if [ -z "$CLAUDE_BIN" ]; then
    echo "heading: Claude Code (claude) is not on PATH. Install it with its" >&2
    echo "  official installer, then open a new terminal." >&2
    exit 127
fi

cd "$ENGINE"
exec "$CLAUDE_BIN" "$@"
