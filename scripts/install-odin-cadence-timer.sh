#!/usr/bin/env bash
# Install the Odin cadence reminder as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-odin-cadence-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-odin-cadence-timer.sh   # pin a TZ
#
# Renders scripts/templates/systemd/odin-cadence.{service,timer} (substituting
# {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/ and does NOT
# enable the weekly timer: it is retired (see the RETIRED note at the bottom),
# because ops-radar carries the Odin collect/reflect signal in its daily push. A
# stale enabled instance from an older install is disabled.
#
# This is a STANDALONE installer. It deliberately does NOT extend the shared
# scripts/install-daemon-service.sh (which is .service-only) -- it mirrors that
# script's template+sed render convention so the pattern is honoured, not
# reinvented. The timezone is read from the environment (no hardcoded locale), so
# the templates carry no geographic signal and ship in the public engine.
#
# No `loginctl enable-linger` here: this installer enables no timer.

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
for unit in odin-cadence.service odin-cadence.timer; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in odin-cadence.service odin-cadence.timer; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
        "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

# Validate the calendar expression before enabling (catches a too-old systemd
# that rejects the trailing timezone, rather than failing opaquely at enable).
if ! systemd-analyze calendar "Mon *-*-* 09:00:00 ${TZ_VALUE}" >/dev/null 2>&1; then
    echo "[warn] this systemd rejects a timezone-suffixed OnCalendar." >&2
    echo "       Edit $DEST_DIR/odin-cadence.timer to 'OnCalendar=Mon 09:00' and" >&2
    echo "       set the host timezone to ${TZ_VALUE}, then re-run." >&2
    exit 6
fi

systemctl --user daemon-reload

# RETIRED (ops-radar Decision 2): the standalone weekly Odin Telegram push is no
# longer enabled. ops-radar folds the Odin collect/reflect signal into its daily
# exception-driven push, so enabling this timer too would double-ping Telegram.
# The unit files are still rendered above (harmless, and odin-cadence.py compute +
# its /prime line stay intact); we just do not enable the timer. If a stale enabled
# instance exists from a prior install, disable it.
systemctl --user disable --now odin-cadence.timer 2>/dev/null || true

echo "  [ok] odin-cadence units rendered; weekly timer RETIRED (folded into ops-radar)."
echo "       Install the replacement: scripts/install-ops-radar-timer.sh"
echo ""
echo "  odin-cadence.py compute + its /prime line remain active (reused by ops-radar)."
echo "  Status:  systemctl --user list-timers ops-radar.timer --no-pager"
