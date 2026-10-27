#!/usr/bin/env bash
# Install the ops-radar detector as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-ops-radar-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-ops-radar-timer.sh   # pin a TZ
#
# Renders scripts/templates/systemd/ops-radar.{service,timer} (substituting
# {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/, then enables the
# daily timer. The timer fires 08:00 in the configured timezone (HEADING_OS_TZ,
# default UTC) independent of any Claude Code session and runs
# scripts/ops-radar-notify.py, which auto-heals Tier-A (restarts the model server
# only when it is a declared Ollama, otherwise names the program and host to
# start; rebuilds a stale index) and pushes a counts-only nudge to the operator's Telegram alert channel ONLY on a genuine due
# signal -- never autonomously executing a Tier-B manual action.
#
# This is a STANDALONE installer mirroring install-odin-cadence-timer.sh's
# template+sed render convention. The timezone is read from the environment (no
# hardcoded locale), so the templates carry no geographic signal and ship in the
# public engine.
#
# For unattended boot:  loginctl enable-linger "$USER"  (done automatically below)

set -euo pipefail

# HELM only. The systemd unit templates substitute the workspace path into
# WorkingDirectory= and ExecStart=, so running this from a YARD worktree
# points a LIVE daemon at a checkout that is deleted two days later.
source "$(dirname "$0")/lib/require-main-clone.sh"
require_main_clone

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

# Unit timezone: HEADING_OS_TZ, else the workspace resolver, else UTC.
TZ_VALUE="$(resolve_unit_tz "$WORKSPACE" "$PYTHON")"

TEMPLATE_DIR="$WORKSPACE/scripts/templates/systemd"
DEST_DIR="$HOME/.config/systemd/user"

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
for unit in ops-radar.service ops-radar.timer; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in ops-radar.service ops-radar.timer; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
        "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

# Validate the calendar expression before enabling (catches a too-old systemd
# that rejects the trailing timezone, rather than failing opaquely at enable).
if ! systemd-analyze calendar "*-*-* 08:00:00 ${TZ_VALUE}" >/dev/null 2>&1; then
    echo "[warn] this systemd rejects a timezone-suffixed OnCalendar." >&2
    echo "       Edit $DEST_DIR/ops-radar.timer to 'OnCalendar=*-*-* 08:00' and" >&2
    echo "       set the host timezone to ${TZ_VALUE}, then re-run." >&2
    exit 6
fi

systemctl --user daemon-reload
systemctl --user enable --now ops-radar.timer

# Load-bearing: without linger a user timer stays silent after an unattended
# reboot. Nothing else holds the WSL session up since HOS-7 (2026-09-21).
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: ops-radar.timer"
echo ""
echo "  Next fire:"
systemctl --user list-timers ops-radar.timer --no-pager || true
echo ""
echo "  Status:  systemctl --user status ops-radar.timer"
echo "  Logs:    journalctl --user -u ops-radar.service -f"
echo "  Test:    python3 scripts/ops-radar-notify.py"
