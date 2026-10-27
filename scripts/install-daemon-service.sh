#!/usr/bin/env bash
# Install a HEADING OS daemon as a systemd user unit on Linux/WSL2.
#
# Usage:
#   scripts/install-daemon-service.sh <name>
#
#   <name> is one of:
#     sentinel
#     sync-exchange | sync-exchange-daemon
#
# Substitutes {{WORKSPACE}}, {{PYTHON}} and {{TZ}} placeholders in the matching
# template at scripts/templates/systemd/<unit>.service, writes the unit
# to ~/.config/systemd/user/, then enables and starts it.
#
# For unattended boot:
#   loginctl enable-linger "$USER"
#
# Counterparts:
#   macOS:   scripts/utils/schedule.py (sentinel via launchd)
#
# `bridge` was a fourth target here, with its own Windows and macOS installers
# beside this one. HOS-7 removed the bridge daemon on 2026-09-21 and deleted
# scripts/templates/systemd/bridge-daemon.service with it, so the target could
# only ever have failed on a missing template: after require_main_clone, and
# after the usage text above had already offered it as a choice.

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
    sentinel)
        UNIT="sentinel"
        ;;
    sync-exchange|sync-exchange-daemon)
        UNIT="sync-exchange-daemon"
        ;;
    *)
        echo "Unknown daemon: $1" >&2
        echo "Expected one of: sentinel sync-exchange" >&2
        exit 2
        ;;
esac

# Workspace root = directory containing this script's parent (i.e. scripts/../).
WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"

# Interpreter for the unit's ExecStart=, resolved by the one owner:
# explicit PYTHON wins, else the pinned .venv, else PATH. MEASURED 2026-09-06:
# seven installed units ran on /usr/bin/python3, whose numpy and pyyaml are
# luck rather than a contract, and everything they respawn through
# sys.executable inherited it.
source "$(dirname "$0")/lib/resolve-python.sh"
source "$(dirname "$0")/lib/render-units.sh"
PYTHON="$(resolve_python "$WORKSPACE")"

# Unit timezone, resolved the same way as the timer installers: an explicit
# HEADING_OS_TZ wins, else the workspace resolver (run as a module), else UTC.
TZ_VALUE="${HEADING_OS_TZ:-$(cd "$WORKSPACE" && "$PYTHON" -m scripts.utils.paths tz || echo UTC)}"

TEMPLATE="$WORKSPACE/scripts/templates/systemd/$UNIT.service"
DEST_DIR="$HOME/.config/systemd/user"
DEST="$DEST_DIR/$UNIT.service"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "Template not found: $TEMPLATE" >&2
    exit 3
fi
if [[ -z "$PYTHON" ]]; then
    echo "No python3 (or python) on PATH" >&2
    exit 4
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found - systemd user units require systemd >= 226." >&2
    echo "On WSL2 enable systemd via /etc/wsl.conf:" >&2
    echo "  [boot]" >&2
    echo "  systemd=true" >&2
    exit 5
fi

mkdir -p "$DEST_DIR"

render_unit "$TEMPLATE" "$DEST" "$WORKSPACE" "$PYTHON" "$TZ_VALUE"

# No runtime directory is made here any more. Until 2026-09-25 this created
# a runtime directory per daemon (`.sentinel/`, `.sync-exchange/`, ...) inside the engine checkout;
# since HOS-5 section 7 each daemon keeps its pid, state and log in the data
# repository and creates that directory itself through the registry.

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT.service"

echo "  [ok] systemd user unit installed and started: $UNIT.service"
echo ""
echo "  Status: systemctl --user status $UNIT.service"
echo "  Logs:   journalctl --user -u $UNIT.service -f"
echo ""
# Linger is one of the three things a user unit needs to start after an
# unattended reboot; enable it here and fail loudly when that does not hold.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" || true
    if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
        echo "  [error] linger is off, so $UNIT.service will not start after a reboot" >&2
        echo "          until someone logs in. Run: loginctl enable-linger $USER" >&2
        exit 6
    fi
fi
