#!/usr/bin/env bash
# Install the Porkbun certificate delivery as a systemd-user timer on Linux/WSL2.
#
# Usage:
#   scripts/install-porkbun-cert-timer.sh
#   HEADING_OS_TZ=America/New_York scripts/install-porkbun-cert-timer.sh   # pin a TZ
#   scripts/install-porkbun-cert-timer.sh --uninstall                 # remove the timer
#
# Renders scripts/templates/systemd/porkbun-cert-push.{service,timer} (substituting
# {{WORKSPACE}}, {{PYTHON}}, {{TZ}}) into ~/.config/systemd/user/, then enables a
# DAILY timer (05:15 in the configured timezone, and 10 minutes after the user
# manager starts, Persistent) that runs `scripts/porkbun.py ssl push`. That pulls
# the current certificate from the Porkbun API and hands it to the server only
# when it differs from the installed one; see porkbun.py for what it validates.
#
# Before installing, set PORKBUN_API_KEY, PORKBUN_SECRET_API_KEY,
# PORKBUN_CERT_DOMAIN, PORKBUN_CERT_HOST and PORKBUN_CERT_NAME in .env, and make
# sure `ssh <PORKBUN_CERT_HOST> status` reaches the server's forced command
# (scripts/server/install-tls-cert.sh). Operator approval for this timer:
# 2026-09-29, given as an explicit exception to the no-new-autonomy rule.
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
    systemctl --user disable --now porkbun-cert-push.timer 2>/dev/null || true
    rm -f "$DEST_DIR/porkbun-cert-push.service" "$DEST_DIR/porkbun-cert-push.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    echo "  [ok] porkbun-cert-push.timer uninstalled."
    exit 0
fi

if [[ -z "$PYTHON" ]]; then
    echo "No python3 (or python) on PATH" >&2
    exit 4
fi
for unit in porkbun-cert-push.service porkbun-cert-push.timer; do
    if [[ ! -f "$TEMPLATE_DIR/$unit" ]]; then
        echo "Template not found: $TEMPLATE_DIR/$unit" >&2
        exit 3
    fi
done

mkdir -p "$DEST_DIR"

for unit in porkbun-cert-push.service porkbun-cert-push.timer; do
    render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
        "$WORKSPACE" "$PYTHON" "$TZ_VALUE"
done

# Validate the calendar expression before enabling (catches a too-old systemd that
# rejects the trailing timezone, rather than failing opaquely at enable).
if ! systemd-analyze calendar "*-*-* 05:15:00 ${TZ_VALUE}" >/dev/null 2>&1; then
    echo "[warn] this systemd rejects a timezone-suffixed OnCalendar." >&2
    echo "       Edit $DEST_DIR/porkbun-cert-push.timer to 'OnCalendar=*-*-* 05:15' and" >&2
    echo "       set the host timezone to ${TZ_VALUE}, then re-run." >&2
    exit 6
fi

systemctl --user daemon-reload
systemctl --user enable --now porkbun-cert-push.timer

# Load-bearing: without linger a user timer stays silent after an unattended
# reboot. Nothing else holds the WSL session up since HOS-7 (2026-09-21).
if ! loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
    loginctl enable-linger "$USER" 2>/dev/null \
        || echo "  [hint] run once for unattended boot: loginctl enable-linger $USER"
fi

echo "  [ok] systemd user timer installed and enabled: porkbun-cert-push.timer"
echo ""
echo "  Next fire:"
systemctl --user list-timers porkbun-cert-push.timer --no-pager || true
echo ""
echo "  Status:  systemctl --user status porkbun-cert-push.timer"
echo "  Logs:    journalctl --user -u porkbun-cert-push.service -f"
echo "  Test:    .venv/bin/python scripts/porkbun.py ssl push   (same as one timer run)"
echo "  Remove:  scripts/install-porkbun-cert-timer.sh --uninstall"
