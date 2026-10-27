#!/usr/bin/env bash
# Restart a HEADING OS daemon's systemd user unit (Linux/WSL2).
#
# Usage:
#   scripts/restart-daemon-service.sh <name>
#
# `bridge` was a target here until HOS-161: HOS-7 removed the bridge daemon
# on 2026-09-21, so the name now falls to "Unknown daemon" like any other.

set -euo pipefail

# HELM only. The systemd unit templates substitute the workspace path into
# WorkingDirectory= and ExecStart=, so running this from a YARD worktree
# points a LIVE daemon at a checkout that is deleted two days later.
source "$(dirname "$0")/lib/require-main-clone.sh"
require_main_clone

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <name>" >&2
    echo "  sentinel | sync-exchange" >&2
    exit 1
fi

case "$1" in
    sentinel)                          UNIT="sentinel" ;;
    sync-exchange|sync-exchange-daemon) UNIT="sync-exchange-daemon" ;;
    *)
        echo "Unknown daemon: $1" >&2
        exit 2
        ;;
esac

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found - cannot restart." >&2
    exit 3
fi

systemctl --user restart "$UNIT.service"
echo "  [ok] restarted $UNIT.service"
echo ""
systemctl --user status "$UNIT.service" --no-pager 2>&1 | head -10 || true
