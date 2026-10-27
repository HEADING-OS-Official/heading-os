#!/usr/bin/env bash
# HEADING OS Official installer.
#
# Leaves two repositories side by side and ready to work:
#   <parent>/.heading-os        the engine, cloned at the latest release tag
#   <parent>/.heading-os-data   YOUR private data repository, created for you
#
# Supported: Linux, Windows through WSL2, macOS 13+. Windows without WSL is not.
# Safe to run again: every step that is already done is skipped.
#
# Usage:
#   bash install.sh [--dir PARENT] [--local-data] [--ref TAG] [--from GIT_URL]
#
#   --dir PARENT    where both repositories go (default: ~/ai/claude-workspaces)
#   --local-data    create the data repository locally only, with no GitHub repo
#   --ref TAG       install this tag or branch instead of the latest release
#   --from GIT_URL  clone the engine from here (default: the official repository)
set -euo pipefail

FROM="https://github.com/HEADING-OS-Official/heading-os.git"
PARENT="$HOME/ai/claude-workspaces"
REF=""
LOCAL_DATA=0

die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  warn\033[0m %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) PARENT="${2:?--dir needs a path}"; shift 2 ;;
        --ref) REF="${2:?--ref needs a tag or branch}"; shift 2 ;;
        --from) FROM="${2:?--from needs a git URL}"; shift 2 ;;
        --local-data) LOCAL_DATA=1; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

# Absolute before anything uses it: step 3 changes directory into the engine,
# and a relative --dir would then point every later path somewhere else.
case "$PARENT" in /*) ;; *) PARENT="$PWD/$PARENT" ;; esac
ENGINE="$PARENT/.heading-os"
DATA="$PARENT/.heading-os-data"

# Every command below acts on THIS data repository and no other. A HEADING_OS_DATA
# inherited from another installation on this machine would otherwise send the
# hooks to that one: MEASURED 2026-10-28, step 6 re-armed the existing data
# repository's pre-push hook at the new engine before step 7 noticed.
if [ -n "${HEADING_OS_DATA:-}" ] && [ "$HEADING_OS_DATA" != "$DATA" ]; then
    warn "HEADING_OS_DATA points at $HEADING_OS_DATA; this install uses $DATA and leaves that one alone"
fi
export HEADING_OS_DATA="$DATA"

step "1. Platform"
case "$(uname -s)" in
    Linux*)
        if grep -qi microsoft /proc/version 2>/dev/null; then
            case "$PARENT" in /mnt/*) die "keep HEADING OS on the Linux filesystem (~/...), not under /mnt" ;; esac
            ok "Linux on WSL2"
        else
            ok "Linux"
        fi ;;
    Darwin*) ok "macOS" ;;
    *) die "unsupported system $(uname -s). Use Linux, macOS, or WSL2 on Windows." ;;
esac

step "2. Tools"
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is not installed. $2"; ok "$1"; }
need git "Install git from your package manager."
need uv "Install uv: see https://docs.astral.sh/uv/getting-started/installation/"
if [ "$LOCAL_DATA" -eq 0 ]; then
    need gh "Install the GitHub CLI, or re-run with --local-data."
    gh auth status >/dev/null 2>&1 || die "gh is not signed in. Run: gh auth login"
    ok "gh is signed in"
fi
if command -v claude >/dev/null 2>&1; then
    ok "claude"
else
    warn "Claude Code is not installed. Install it before your first session: see docs/INSTALL.md"
fi

step "3. Engine"
mkdir -p "$PARENT"
if [ -z "$REF" ]; then
    # A release is resolved or the install stops; a branch is installed only
    # when it is named with --ref, never as a silent fallback.
    TAGS="$(git ls-remote --tags --refs "$FROM" 'v*')" \
        || die "cannot list the releases of $FROM (network or URL); re-run, or name one with --ref"
    # Release tags are vMAJOR.MINOR.PATCH from 3.1.0 on; the first three were
    # dated, v2026.10.1 to v2026.10.3. Anything else (a pre-release, a stray v*
    # tag) is not a release. A dated tag outranks any version number, so it is
    # considered only while no versioned tag exists. Sorted with POSIX sort
    # fields, not GNU `sort -V`, which the stock macOS sort lacks.
    REL="$(printf '%s\n' "$TAGS" | sed 's#.*refs/tags/##' \
        | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true; } | sed 's/^v//')"
    SEMVER="$(printf '%s\n' "$REL" | awk -F. 'NF && $1 + 0 < 1000')"
    [ -z "$SEMVER" ] || REL="$SEMVER"
    REF="$(printf '%s\n' "$REL" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n1)"
    [ -z "$REF" ] || REF="v$REF"
    [ -n "$REF" ] || die "$FROM has no release tag (vX.Y.Z) yet; to install a branch, name it: --ref main"
fi
if [ -d "$ENGINE/.git" ]; then
    ok "engine already cloned at $ENGINE"
    if [ -n "$REF" ]; then
        [ -z "$(git -C "$ENGINE" status --porcelain)" ] || die "$ENGINE has uncommitted changes; commit or stash them, then re-run"
        git -C "$ENGINE" fetch --tags --quiet origin
        git -C "$ENGINE" checkout --quiet "$REF"
        ok "engine at $REF"
    fi
elif [ -e "$ENGINE" ]; then
    die "$ENGINE exists and is not a git clone; move it aside, then re-run"
else
    if [ -n "$REF" ]; then
        git clone --quiet --branch "$REF" "$FROM" "$ENGINE"
    else
        git clone --quiet "$FROM" "$ENGINE"
    fi
    ok "engine cloned into $ENGINE${REF:+ at $REF}"
fi
cd "$ENGINE"

step "4. Python environment"
uv sync --quiet
ok "dependencies installed into $ENGINE/.venv"
PY="$ENGINE/.venv/bin/python"

step "5. Your private data repository"
if [ -d "$DATA" ] && [ -f "$DATA/.schema-version" ]; then
    ok "data repository already present at $DATA"
elif [ -e "$DATA" ] && [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
    die "$DATA exists, is not empty and is not a HEADING OS data repository; move it aside, then re-run"
elif [ "$LOCAL_DATA" -eq 1 ]; then
    "$PY" scripts/create-data-repo.py --path "$DATA" --no-remote
else
    "$PY" scripts/create-data-repo.py --path "$DATA"
fi

step "6. Settings and gates"
if [ -f .env ]; then ok ".env already present"; else cp .env.example .env; ok ".env created from .env.example"; fi
"$ENGINE/.venv/bin/pre-commit" install >/dev/null
ok "commit gates armed"
"$PY" scripts/install-git-hooks.py
bash scripts/setup-platform.sh
bash scripts/setup-platform.sh --check

step "7. Check"
# Without the variable exported above: this checks what a new shell will find.
FOUND="$(env -u HEADING_OS_DATA "$PY" -c 'from scripts.utils.paths import get_data_root; print(get_data_root())')"
[ "$(cd "$FOUND" && pwd -P)" = "$(cd "$DATA" && pwd -P)" ] || die "the engine resolves its data at $FOUND, not $DATA"
ok "the engine finds your data at $DATA"

cat <<EOF

HEADING OS is installed.

Next:
  1. cd "$ENGINE"
  2. Put the keys you have into .env (see docs/INSTALL.md).
  3. Run: claude
  4. Trust this folder when Claude asks.
  5. In Claude, run: /setup-wizard
  6. Then run: /prime

Optional tools (memory, task tracker, parallel work, other models): see docs/.
EOF
