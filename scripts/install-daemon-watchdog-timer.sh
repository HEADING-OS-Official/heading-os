#!/usr/bin/env bash
# Install the daemon liveness watchdog as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-daemon-watchdog-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-daemon-watchdog-timer.sh   # pin a TZ
#
# Renders scripts/templates/systemd/daemon-watchdog.{service,timer} into
# ~/.config/systemd/user/ and enables a timer that runs one
# `scripts/daemon-watchdog.py --once` pass every two minutes. Each pass reads the
# heartbeat of every daemon this host expects and routes a deduped alert through
# scripts/utils/alert.py on a silent or missing one. Without this timer nothing
# runs the watchdog, so a dead daemon raises no alert at all.
#
# For unattended boot:  loginctl enable-linger "$USER"  (done automatically below)

set -euo pipefail

# HELM only. The systemd unit templates substitute the workspace path into
# WorkingDirectory= and ExecStart=, so running this from a YARD worktree
# points a LIVE daemon at a checkout that is deleted two days later.
source "$(dirname "$0")/lib/require-main-clone.sh"
require_main_clone

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"

# Interpreter (resolve-python.sh) and unit timezone (render-units.sh), each
# resolved by its one shared owner.
source "$(dirname "$0")/lib/resolve-python.sh"
source "$(dirname "$0")/lib/render-units.sh"
PYTHON="$(resolve_python "$WORKSPACE")"
# This timer has no calendar expression, so the zone changes nothing about WHEN
# it fires; it keeps the journal lines in the operator's local time.
TZ_VALUE="$(resolve_unit_tz "$WORKSPACE" "$PYTHON")"

TEMPLATE_DIR="$WORKSPACE/scripts/templates/systemd"
DEST_DIR="$HOME/.config/systemd/user"
UNITS=(daemon-watchdog.service daemon-watchdog.timer)

if [[ -z "$PYTHON" ]]; then
    echo "No python interpreter found (set PYTHON=...)." >&2
    exit 4
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found - systemd user units require systemd >= 226." >&2
    echo "On WSL2 enable systemd via /etc/wsl.conf:" >&2
    echo "  [boot]" >&2
    echo "  systemd=true" >&2
    exit 5
fi

for unit in "${UNITS[@]}"; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in "${UNITS[@]}"; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

systemctl --user daemon-reload
systemctl --user enable --now daemon-watchdog.timer

# Linger keeps user timers firing after an unattended reboot with no login.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: daemon-watchdog.timer"
echo "       interpreter: ${PYTHON}"
echo ""
systemctl --user list-timers daemon-watchdog.timer --no-pager || true
echo ""
echo "  Check now: $PYTHON $WORKSPACE/scripts/daemon-watchdog.py --once"
echo "  Logs:      journalctl --user -u daemon-watchdog.service -f"
echo "  Disable:   systemctl --user disable --now daemon-watchdog.timer"
