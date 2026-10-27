#!/usr/bin/env bash
# Install the Hindsight sync (scripts/hindsight-sync.py) as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-hindsight-sync-timer.sh
#   HINDSIGHT_SYNC_LIMIT=200 HINDSIGHT_SYNC_MAX_PENDING=80 scripts/install-hindsight-sync-timer.sh
#   scripts/install-hindsight-sync-timer.sh --uninstall
#
# Renders scripts/templates/systemd/hindsight-sync.{service,timer} (substituting
# {{WORKSPACE}}, {{PYTHON}}, {{TZ}}, {{STATE}}, {{LIMIT}}, {{MAX_PENDING}}) into
# ~/.config/systemd/user/, then enables a timer every 15 minutes that sends new
# conversation turns and new or changed workspace files to the Hindsight bank. The
# first runs ARE the backfill, paced by --max-pending so the server's queue never
# grows past what it can digest.
#
# It also renders and enables hindsight-capture-check.{service,timer} (HOS-200): once a
# day, `hindsight-sync.py capture-check` asks the bank whether every interactive session
# of the last 25 hours has its conversation document, and fails the unit on a miss.
# Operator decision, 2026-10-05, on the daily check: "yes" (translated).
#
# Operator approval: 2026-09-30, "start" (translated) on the whole-workspace Hindsight plan
# (prompt archive hindsight-whole-workspace-memory), archives excluded.
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
PYTHON="$(resolve_python "$WORKSPACE")"

# Unit timezone: resolved through the workspace resolver rather than read from
# the environment alone. HEADING_OS_TZ lives in the gitignored .env and is
# exported by nothing, so an environment-only read renders UTC on a machine
# whose timezone is correctly configured. An explicit HEADING_OS_TZ=X still wins.
# Invoked as a MODULE, from the workspace root. Running the file directly puts
# scripts/utils/ on sys.path[0], where operator.py shadows the stdlib operator
# that collections imports -- measured fatal on Python 3.12 (the service host)
# and silently fine on 3.11 (the laptop), so the || echo UTC fallback below was
# swallowing it as a plain "no timezone configured".
TZ_VALUE="${HEADING_OS_TZ:-$(cd "$WORKSPACE" && "$PYTHON" -m scripts.utils.paths tz || echo UTC)}"

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
    systemctl --user disable --now hindsight-sync.timer hindsight-capture-check.timer 2>/dev/null || true
    rm -f "$DEST_DIR/hindsight-sync.service" "$DEST_DIR/hindsight-sync.timer" \
          "$DEST_DIR/hindsight-capture-check.service" "$DEST_DIR/hindsight-capture-check.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  [ok] hindsight-sync.timer and hindsight-capture-check.timer uninstalled."
    exit 0
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python3 (or python) on PATH" >&2
    exit 4
fi
UNITS="hindsight-sync.service hindsight-sync.timer hindsight-capture-check.service hindsight-capture-check.timer"
for unit in $UNITS; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

STATE="$(cd "$WORKSPACE" && "$PYTHON" scripts/data-path.py path runtime:hindsight-sync/state)"
mkdir -p "$STATE"
# 200 and 80 since 2026-09-30 (operator decision: "yes, do it", translated): with 12 parallel extraction calls the
# server retained about 98 documents an hour and sat idle at 0 pending, so 100 files per
# 15 min behind a 40-operation gate fed it too little.
LIMIT="${HINDSIGHT_SYNC_LIMIT:-200}"
MAX_PENDING="${HINDSIGHT_SYNC_MAX_PENDING:-80}"

# Render both units with portable sed. Pipe markers chosen because the paths may
# contain forward slashes.
for unit in $UNITS; do
    sed -e "s|{{WORKSPACE}}|${WORKSPACE}|g" \
        -e "s|{{PYTHON}}|${PYTHON}|g" \
        -e "s|{{TZ}}|${TZ_VALUE}|g" \
        -e "s|{{STATE}}|${STATE}|g" \
        -e "s|{{LIMIT}}|${LIMIT}|g" \
        -e "s|{{MAX_PENDING}}|${MAX_PENDING}|g" \
        "$TEMPLATE_DIR/$unit" > "$DEST_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now hindsight-sync.timer hindsight-capture-check.timer

# Load-bearing, never optional: nothing else holds the WSL session up (HOS-7,
# 2026-09-21), so without linger the timer is armed and never fires after an
# unattended reboot.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timers installed and enabled: hindsight-sync.timer, hindsight-capture-check.timer"
echo ""
echo "  Next fire:"
systemctl --user list-timers hindsight-sync.timer hindsight-capture-check.timer --no-pager || true
echo ""
echo "  Status:  systemctl --user status hindsight-sync.timer"
echo "  Logs:    journalctl --user -u hindsight-sync.service -f"
echo "  Plan:    .venv/bin/python scripts/hindsight-sync.py plan --wave all"
echo "  Ledger:  .venv/bin/python scripts/hindsight-sync.py status"
echo "  Capture: .venv/bin/python scripts/hindsight-sync.py capture-check"
echo "  Remove:  scripts/install-hindsight-sync-timer.sh --uninstall"
