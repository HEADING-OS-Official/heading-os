#!/usr/bin/env bash
# Install the HEADING OS update manager as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-update-manager-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-update-manager-timer.sh   # pin a TZ
#
# Renders scripts/templates/systemd/update-manager.{service,timer}
# (substituting {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/,
# then enables the DAILY timer. The timer fires 07:00 in the configured timezone
# (HEADING_OS_TZ, default UTC) independent of any Claude Code session and runs
# scripts/update-manager.py check followed by scripts/update-manager.py apply
# --auto, applying only auto-tier component updates. Anything above the
# auto-tier stays queued for the operator's one-command
# `python scripts/update-manager.py apply`.
#
# STANDALONE installer mirroring scripts/install-odin-cadence-timer.sh's
# template+sed render convention (no hardcoded locale; the templates ship in the
# public engine). For unattended boot: loginctl enable-linger (done below).

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
# Unit timezone: HEADING_OS_TZ, else the workspace resolver, else UTC.
TZ_VALUE="$(resolve_unit_tz "$WORKSPACE" "$PYTHON")"

TEMPLATE_DIR="$WORKSPACE/scripts/templates/systemd"
DEST_DIR="$HOME/.config/systemd/user"
UNITS=(update-manager.service update-manager.timer)

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

# Validate the calendar expression before enabling (catches a too-old systemd
# that rejects the trailing timezone rather than failing opaquely at enable).
if ! systemd-analyze calendar "*-*-* 07:00:00 ${TZ_VALUE}" >/dev/null 2>&1; then
    echo "[warn] this systemd rejects a timezone-suffixed OnCalendar." >&2
    echo "       Edit $DEST_DIR/update-manager.timer to 'OnCalendar=*-*-* 07:00' and" >&2
    echo "       set the host timezone to ${TZ_VALUE}, then re-run." >&2
    exit 6
fi

systemctl --user daemon-reload
systemctl --user enable --now update-manager.timer
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

echo "  [ok] update-manager daily timer installed and enabled (${TZ_VALUE} 07:00)."
echo "  Status:   systemctl --user list-timers update-manager.timer --no-pager"
echo "  Dry-run:  python3 scripts/update-manager.py check"
echo "  Manual:   python  scripts/update-manager.py apply"
