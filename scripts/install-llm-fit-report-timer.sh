#!/usr/bin/env bash
# Install the weekly LLM-fit report as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-llm-fit-report-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-llm-fit-report-timer.sh   # pin a TZ
#   scripts/install-llm-fit-report-timer.sh --uninstall                # remove the timer
#
# Renders scripts/templates/systemd/llm-fit-report.{service,timer} (substituting
# {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/, then enables the
# weekly timer. The timer fires Sunday 03:00 in the configured timezone
# (HEADING_OS_TZ, default UTC) and runs scripts/llm-fit-report.py, which writes a
# dated report into the DATA overlay. The bridge daemon ran it on that schedule
# through APScheduler until HOS-7 removed the daemon on 2026-09-21; from then until
# this timer is installed nothing runs it.
#
# Copied from install-router-accuracy-timer.sh, keeping its template+sed render
# convention and all three reboot mechanisms. The timezone is read from the
# environment (no hardcoded locale), so the templates carry no geographic signal.
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

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found - systemd user units require systemd >= 226." >&2
    echo "On WSL2 enable systemd via /etc/wsl.conf:" >&2
    echo "  [boot]" >&2
    echo "  systemd=true" >&2
    exit 5
fi

# Uninstall path: disable + remove the units, then reload.
if [[ "${1:-}" == "--uninstall" || "${1:-}" == "uninstall" ]]; then
    systemctl --user disable --now llm-fit-report.timer 2>/dev/null || true
    rm -f "$DEST_DIR/llm-fit-report.service" "$DEST_DIR/llm-fit-report.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  [ok] llm-fit-report.timer uninstalled."
    exit 0
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python3 (or python) on PATH" >&2
    exit 4
fi
for unit in llm-fit-report.service llm-fit-report.timer; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in llm-fit-report.service llm-fit-report.timer; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
        "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

# Validate the calendar expression before enabling (catches a too-old systemd that
# rejects the trailing timezone, rather than failing opaquely at enable).
if ! systemd-analyze calendar "Sun *-*-* 03:00:00 ${TZ_VALUE}" >/dev/null 2>&1; then
    echo "[warn] this systemd rejects a timezone-suffixed OnCalendar." >&2
    echo "       Edit $DEST_DIR/llm-fit-report.timer to 'OnCalendar=Sun *-*-* 03:00' and" >&2
    echo "       set the host timezone to ${TZ_VALUE}, then re-run." >&2
    exit 6
fi

systemctl --user daemon-reload
systemctl --user enable --now llm-fit-report.timer

# Load-bearing: without linger a user timer stays silent after an unattended
# reboot. Nothing else holds the WSL session up since HOS-7 (2026-09-21).
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: llm-fit-report.timer"
echo ""
echo "  Next fire:"
systemctl --user list-timers llm-fit-report.timer --no-pager || true
echo ""
echo "  Status:  systemctl --user status llm-fit-report.timer"
echo "  Logs:    journalctl --user -u llm-fit-report.service -f"
echo "  Test:    .venv/bin/python scripts/llm-fit-report.py --no-write"
echo "  Remove:  scripts/install-llm-fit-report-timer.sh --uninstall"
