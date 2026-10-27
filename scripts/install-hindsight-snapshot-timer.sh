#!/usr/bin/env bash
# Install the daily Hindsight snapshot (scripts/hindsight-snapshot.py take) as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-hindsight-snapshot-timer.sh
#   scripts/install-hindsight-snapshot-timer.sh --uninstall
#
# Renders scripts/templates/systemd/hindsight-snapshot.{service,timer} (substituting
# {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/, then enables a
# daily timer that writes what lives only in the Hindsight bank to disk.
#
# Operator approval: 2026-10-05, "Yes, let's do it. Go" (translated) on the plan that makes Hindsight
# the main memory with a daily on-disk snapshot (prompt archive hindsight-primary-memory,
# HOS-203). Installing it is HELM's, after the merge.
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
    systemctl --user disable --now hindsight-snapshot.timer 2>/dev/null || true
    rm -f "$DEST_DIR/hindsight-snapshot.service" "$DEST_DIR/hindsight-snapshot.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  [ok] hindsight-snapshot.timer uninstalled."
    exit 0
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python3 (or python) on PATH" >&2
    exit 4
fi
for unit in hindsight-snapshot.service hindsight-snapshot.timer; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

# Render both units with portable sed. Pipe markers chosen because the paths may
# contain forward slashes.
for unit in hindsight-snapshot.service hindsight-snapshot.timer; do
    sed -e "s|{{WORKSPACE}}|${WORKSPACE}|g" \
        -e "s|{{PYTHON}}|${PYTHON}|g" \
        -e "s|{{TZ}}|${TZ_VALUE}|g" \
        "$TEMPLATE_DIR/$unit" > "$DEST_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now hindsight-snapshot.timer

# Load-bearing, never optional: nothing else holds the WSL session up (HOS-7,
# 2026-09-21), so without linger the timer is armed and never fires after an
# unattended reboot.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: hindsight-snapshot.timer"
echo ""
echo "  Next fire:"
systemctl --user list-timers hindsight-snapshot.timer --no-pager || true
echo ""
echo "  Status:  systemctl --user status hindsight-snapshot.timer"
echo "  Logs:    journalctl --user -u hindsight-snapshot.service -f"
echo "  Status of the copy:  .venv/bin/python scripts/hindsight-snapshot.py status"
echo "  Run one now:         .venv/bin/python scripts/hindsight-snapshot.py take"
echo "  Remove:  scripts/install-hindsight-snapshot-timer.sh --uninstall"
