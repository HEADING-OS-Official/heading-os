#!/bin/sh
# Start Lific's MCP server as the bot, never as you.
#
# Rendered to ~/.local/bin/lific-mcp by scripts/heading-install.py (step
# lific-mcp, scripts/utils/install_lific.py) and registered with Claude Code
# over stdio. The bot's key stays in its own 0600 file and reaches Lific only
# through LIFIC_TOKEN, never through the Claude Code config. Without a key
# Lific would run as you, with only a warning, so this refuses to start.
set -eu

TOKEN_FILE={{TOKEN_FILE}}

refuse() {
    echo "lific-mcp: $1 at $TOKEN_FILE; refusing to start as you." \
         "Run scripts/heading-install.py again to issue one." >&2
    exit 1
}

[ -r "$TOKEN_FILE" ] || refuse "no readable token"
# An empty token is no token: Lific would fall back to the local user.
LIFIC_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
[ -n "$LIFIC_TOKEN" ] || refuse "an empty token"
export LIFIC_TOKEN

exec {{LIFIC_BIN}} mcp --config {{LIFIC_CONFIG}} "$@"
