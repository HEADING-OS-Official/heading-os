#!/usr/bin/env bash
# Install the proxy-usage dashboard rebuild as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-proxy-usage-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-proxy-usage-timer.sh   # pin a TZ
#   scripts/install-proxy-usage-timer.sh --uninstall                     # remove the timer
#
# Renders scripts/templates/systemd/proxy-usage-dashboard.{service,timer}
# (substituting {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/,
# then enables a timer that runs `scripts/proxy-usage.py dashboard` every 5
# minutes. That command rewrites the one dashboard file (registry name
# proxy-usage-reports, `proxy-usage.html`) only when the usage ledger changed,
# and exits 0 doing nothing otherwise.
#
# Operator approval for this timer: 2026-10-25, his "yes" (translated) to the recommendation of
# a fixed file, a 5-minute user timer that skips when nothing changed, and a meta
# refresh in the page.
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

# Interpreter and unit timezone, each resolved by its one owner.
source "$(dirname "$0")/lib/resolve-python.sh"
source "$(dirname "$0")/lib/render-units.sh"
PYTHON="$(resolve_python "$WORKSPACE")"
TZ_VALUE="$(resolve_unit_tz "$WORKSPACE" "$PYTHON")"

TEMPLATE_DIR="$WORKSPACE/scripts/templates/systemd"
DEST_DIR="$HOME/.config/systemd/user"
UNITS=(proxy-usage-dashboard.service proxy-usage-dashboard.timer)

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found - systemd user units require systemd >= 226." >&2
    echo "On WSL2 enable systemd via /etc/wsl.conf:" >&2
    echo "  [boot]" >&2
    echo "  systemd=true" >&2
    exit 5
fi

# Uninstall path: disable + remove the units, then reload.
if [[ "${1:-}" == "--uninstall" || "${1:-}" == "uninstall" ]]; then
    systemctl --user disable --now proxy-usage-dashboard.timer 2>/dev/null || true
    rm -f "$DEST_DIR/proxy-usage-dashboard.service" "$DEST_DIR/proxy-usage-dashboard.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  [ok] proxy-usage-dashboard.timer uninstalled."
    exit 0
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python3 (or python) on PATH" >&2
    exit 4
fi
for unit in "${UNITS[@]}"; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in "${UNITS[@]}"; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
        "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

# Validate the RENDERED calendar expression before enabling, so a systemd that
# rejects a timezone suffix fails here with an explanation.
RENDERED_CAL="$(grep -m1 '^OnCalendar=' "$DEST_DIR/proxy-usage-dashboard.timer" | sed 's|^OnCalendar=||')"
if ! systemd-analyze calendar "$RENDERED_CAL" >/dev/null 2>&1; then
    echo "[warn] this systemd rejects the calendar expression: $RENDERED_CAL" >&2
    echo "       Drop the trailing timezone from $DEST_DIR/proxy-usage-dashboard.timer and set" >&2
    echo "       the host timezone to ${TZ_VALUE}, then re-run." >&2
    exit 6
fi

systemctl --user daemon-reload
systemctl --user enable --now proxy-usage-dashboard.timer

# Load-bearing, never optional: nothing else holds the WSL session up (HOS-7,
# 2026-09-21), so without linger the timer is armed and never fires after an
# unattended reboot.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: proxy-usage-dashboard.timer"
echo "       interpreter: ${PYTHON}"
echo ""
echo "  Next fire:"
systemctl --user list-timers proxy-usage-dashboard.timer --no-pager || true
echo ""
echo "  Status:  systemctl --user status proxy-usage-dashboard.timer"
echo "  Logs:    journalctl --user -u proxy-usage-dashboard.service -f"
echo "  Test:    .venv/bin/python scripts/proxy-usage.py dashboard --force   (one rebuild)"
echo "  Remove:  scripts/install-proxy-usage-timer.sh --uninstall"
