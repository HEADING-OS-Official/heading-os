#!/usr/bin/env bash
# Install the inference guard as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-inference-guard-timer.sh
#   PYTHON=/path/to/python scripts/install-inference-guard-timer.sh
#
# Renders scripts/templates/systemd/inference-guard.{service,timer} into
# ~/.config/systemd/user/ and enables a timer that fires every five minutes.
# Each fire probes the addresses config/inference-hosts.yaml names and, when none
# of them has answered for the ops radar's escalation threshold of consecutive
# fires, sends ONE line to the operator's own Telegram sink (and one more when
# the host answers again). It starts nothing: until 2026-09-25 it launched the
# Windows-side application through WSL interop, and the operator kept the
# WSL-only rule without exception that day (HOS-60).
#
# Why this exists. Since 2026-08-23 every model this workspace uses lives on one
# machine and nothing falls back: embedding refuses when its host is down, and
# the nightly chronicle stops. Autostart covers a reboot; nothing covered a
# crash, and on 2026-08-20 the Windows daemon crash-looped for sixteen hours
# with no one the wiser.
#
# For unattended boot:  loginctl enable-linger "$USER"  (done automatically below)

set -euo pipefail

# HELM only. The systemd unit templates substitute the workspace path into
# WorkingDirectory= and ExecStart=, so running this from a YARD worktree
# points a LIVE daemon at a checkout that is deleted two days later.
source "$(dirname "$0")/lib/require-main-clone.sh"
require_main_clone

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"

# Interpreter for the unit's ExecStart=, resolved by the one owner:
# explicit PYTHON wins, else the pinned .venv, else PATH. MEASURED 2026-09-06:
# seven installed units ran on /usr/bin/python3, whose numpy and pyyaml are
# luck rather than a contract, and everything they respawn through
# sys.executable inherited it.
source "$(dirname "$0")/lib/resolve-python.sh"
source "$(dirname "$0")/lib/render-units.sh"
PYTHON="$(resolve_python "$WORKSPACE")"

# Unit timezone: HEADING_OS_TZ, else the workspace resolver, else UTC. This timer
# has no calendar expression, so the zone only sets the journal's local time.
TZ_VALUE="$(resolve_unit_tz "$WORKSPACE" "$PYTHON")"

TEMPLATE_DIR="$WORKSPACE/scripts/templates/systemd"
DEST_DIR="$HOME/.config/systemd/user"

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

for unit in inference-guard.service inference-guard.timer; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in inference-guard.service inference-guard.timer; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
        "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

systemctl --user daemon-reload
systemctl --user enable --now inference-guard.timer

if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: inference-guard.timer"
echo "       interpreter: ${PYTHON}"
echo ""
systemctl --user list-timers inference-guard.timer --no-pager || true
echo ""
echo "  Check now: $PYTHON $WORKSPACE/scripts/inference-guard.py check"
echo "  Watch now: systemctl --user start inference-guard.service"
echo "  Logs:      journalctl --user -u inference-guard.service -f"
echo "  Disable:   systemctl --user disable --now inference-guard.timer"
