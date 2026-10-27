#!/usr/bin/env bash
# Install the council model freshness check as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-council-models-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-council-models-timer.sh   # pin a TZ
#
# Renders scripts/templates/systemd/council-models-check.{service,timer}
# (substituting {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/,
# then enables the DAILY timer. The timer fires 08:30 in the configured timezone
# (HEADING_OS_TZ, default UTC) independent of any Claude Code session and runs
# scripts/council-models-notify.py, which pushes a one-line nudge to the
# operator's Telegram alert channel ONLY when a /council model pin is broken or a
# newer flagship is available (deduped, so it never re-nudges an unchanged
# finding). It never edits the pins -- adoption is the operator's one-command
# `python scripts/council-models.py --set ...`.
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
UNITS=(council-models-check.service council-models-check.timer)

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
if ! systemd-analyze calendar "*-*-* 08:30:00 ${TZ_VALUE}" >/dev/null 2>&1; then
    echo "[warn] this systemd rejects a timezone-suffixed OnCalendar." >&2
    echo "       Edit $DEST_DIR/council-models-check.timer to 'OnCalendar=*-*-* 08:30' and" >&2
    echo "       set the host timezone to ${TZ_VALUE}, then re-run." >&2
    exit 6
fi

systemctl --user daemon-reload
systemctl --user enable --now council-models-check.timer
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

echo "  [ok] council-models-check daily timer installed and enabled (${TZ_VALUE} 08:30)."
echo "  Status:   systemctl --user list-timers council-models-check.timer --no-pager"
echo "  Dry-run:  python3 scripts/council-models-notify.py --force"
echo "  Manual:   python  scripts/council-models.py --check"
