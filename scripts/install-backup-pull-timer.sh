#!/usr/bin/env bash
# Install the server backup pull (scripts/backup-pull.py) as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-backup-pull-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-backup-pull-timer.sh   # pin a TZ
#   scripts/install-backup-pull-timer.sh --uninstall                 # remove the timer
#
# Renders scripts/templates/systemd/backup-pull.{service,timer} (substituting
# {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/, then enables an
# HOURLY timer (and 5 minutes after the user manager starts, Persistent) that runs
# `scripts/backup-pull.py pull`: a new hard-linked snapshot of the server's
# pgBackRest repository, verified here, older ones pruned to HINDSIGHT_BACKUP_KEEP.
#
# Before installing, set HINDSIGHT_BACKUP_HOST, HINDSIGHT_BACKUP_DIR,
# HINDSIGHT_BACKUP_CIPHER_FILE (and optionally HINDSIGHT_BACKUP_KEEP,
# HINDSIGHT_PGBACKREST) in .env, and run `backup-pull.py pull` once by hand.
# Operator approval for this timer: 2026-09-29, the Hindsight deployment plan v2
# ("backups go to the laptop, the laptop pulls them itself", translated), approved with his "Go" that day.
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
    systemctl --user disable --now backup-pull.timer 2>/dev/null || true
    rm -f "$DEST_DIR/backup-pull.service" "$DEST_DIR/backup-pull.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  [ok] backup-pull.timer uninstalled."
    exit 0
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python3 (or python) on PATH" >&2
    exit 4
fi
for unit in backup-pull.service backup-pull.timer; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in backup-pull.service backup-pull.timer; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
        "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

systemctl --user daemon-reload
systemctl --user enable --now backup-pull.timer

# Load-bearing: without linger a user timer stays silent after an unattended
# reboot. Nothing else holds the WSL session up since HOS-7 (2026-09-21).
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: backup-pull.timer"
echo ""
echo "  Next fire:"
systemctl --user list-timers backup-pull.timer --no-pager || true
echo ""
echo "  Status:  systemctl --user status backup-pull.timer"
echo "  Logs:    journalctl --user -u backup-pull.service -f"
echo "  Test:    .venv/bin/python scripts/backup-pull.py pull   (same as one timer run)"
echo "  Remove:  scripts/install-backup-pull-timer.sh --uninstall"
