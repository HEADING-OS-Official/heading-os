#!/usr/bin/env bash
# HEADING OS Official installer: the bootstrap.
#
# Gets the two tools the real installer needs, git and uv, clones the engine at
# the latest release tag, and hands over to scripts/heading-install.py, which
# does everything else and is safe to run again. Leaves, under one folder:
#   <target>/engine   the engine
#   <target>/data     YOUR private data repository, local by default
#
# Supported: Linux, Windows through WSL2, macOS 13+. Windows without WSL is not.
#
# Usage:
#   bash install.sh [--target DIR] [--github] [--ref TAG] [--from GIT_URL] [--dry-run]
#                   [--mail CHOICE] [--github-backup yes|later] [--tz ZONE]
#                   [--proxy-signin now|later] [--answers FILE]
#
#   --target DIR    where engine/ and data/ go (default: ~/HeadingOS)
#   --github        also keep a private copy of your data on your GitHub account
#   --mail CHOICE   exchange, google or none (connect later); passed to heading-install.py
#   --github-backup yes|later
#                   a private copy of your data on GitHub now, or later; passed on
#   --tz ZONE       your time zone, such as Europe/Berlin; passed on
#   --proxy-signin now|later
#                   the Claude sign-in for the model proxy now, or later; passed on
#   --answers FILE  the intake answers in a JSON file (mode 600); passed on, never asks
#   --ref TAG       install this tag or branch instead of the latest release
#   --from GIT_URL  clone the engine from here (default: the official repository)
#   --dry-run       say what would be done; change nothing
set -euo pipefail

FROM="https://github.com/HEADING-OS-Official/heading-os.git"
TARGET="$HOME/HeadingOS"
REF=""
PASS=()
ANSWERS=""

die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
warn() { printf '\033[33m  warn\033[0m %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="${2:?--target needs a path}"; shift 2 ;;
        --ref) REF="${2:?--ref needs a tag or branch}"; shift 2 ;;
        --from) FROM="${2:?--from needs a git URL}"; shift 2 ;;
        --github|--dry-run) PASS+=("$1"); shift ;;
        --mail|--github-backup|--tz|--proxy-signin) PASS+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
        --answers) ANSWERS="${2:?--answers needs a file}"; shift 2 ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done
DRY=0
for a in "${PASS[@]+"${PASS[@]}"}"; do [ "$a" = "--dry-run" ] && DRY=1; done

case "$TARGET" in /*) ;; *) TARGET="$PWD/$TARGET" ;; esac
if [ -n "$ANSWERS" ]; then
    case "$ANSWERS" in /*) ;; *) ANSWERS="$PWD/$ANSWERS" ;; esac
    PASS+=(--answers "$ANSWERS")
fi
ENGINE="$TARGET/engine"
DATA="$TARGET/data"

# Every command below acts on THIS data repository and no other. A HEADING_OS_DATA
# inherited from another installation on this machine would otherwise send the
# hooks to that one: MEASURED 2026-10-28, the gates step re-armed the existing
# data repository's pre-push hook at the new engine. heading-install.py sets the
# variable for every child once the folder exists, and checks at the end, with
# `env -u HEADING_OS_DATA`, that a NEW terminal finds this data and no other.
if [ -n "${HEADING_OS_DATA:-}" ] && [ "$HEADING_OS_DATA" != "$DATA" ]; then
    warn "HEADING_OS_DATA points at $HEADING_OS_DATA; this install uses $DATA and leaves that one alone"
fi
unset HEADING_OS_DATA

case "$(uname -s)" in
    Linux*)
        if grep -qi microsoft /proc/version 2>/dev/null; then
            case "$TARGET" in /mnt/*) die "keep HEADING OS on the Linux filesystem (~/...), not under /mnt" ;; esac
        fi ;;
    Darwin*) ;;
    *) die "unsupported system $(uname -s). Use Linux, macOS, or WSL2 on Windows." ;;
esac

# git: from the system's package manager (it asks for your password once).
if ! command -v git >/dev/null 2>&1; then
    [ "$DRY" -eq 0 ] || die "git is not installed (a real run installs it)"
fi
# Everything the package manager must supply, worked out before asking for the
# password, so a single install asks for it once. The Node.js that
# heading-install.py fetches needs libatomic1, which a fresh Ubuntu or Mint may
# lack (MEASURED 2026-11-01: Node v26.13.0 did not start on a machine without it).
PKGS=""
command -v git >/dev/null 2>&1 || PKGS="git"
if command -v apt-get >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1 \
        && ! dpkg -s libatomic1 >/dev/null 2>&1; then
    PKGS="${PKGS:+$PKGS }libatomic1"
fi
# zstd unpacks the local search engine's .tar.zst archive (scripts/utils/install_index.py).
if command -v apt-get >/dev/null 2>&1 && ! command -v zstd >/dev/null 2>&1; then
    PKGS="${PKGS:+$PKGS }zstd"
fi
# The password goes into the desktop's own system window (pkexec), never into
# this script: run from a Claude chat it has no terminal, so sudo cannot ask
# (MEASURED 2026-11-02). Without pkexec or a desktop (WSL), and on any failure
# (Cancel, a wrong password), it stops with the one command to run in a terminal,
# never an ok and a failure further down (MEASURED 2026-11-02: an unchecked
# install printed "ok git", then the clone died).
as_root() {
    command -v pkexec >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 1
    printf 'A system window asks for your computer password once, to install: %s.\n' "$PKGS"
    pkexec "$@"
}
if [ -n "$PKGS" ] && [ "$DRY" -eq 0 ]; then
    if [ "$(uname -s)" = Darwin ]; then
        xcode-select --install || true
        die "macOS is installing the Command Line Tools, which include git; run this again when it finishes"
    elif command -v apt-get >/dev/null 2>&1; then
        as_root sh -c "apt-get update -qq && apt-get install -y -qq $PKGS" \
            || die "could not install $PKGS. Run this yourself, then run this installer again: sudo apt-get install -y $PKGS"
    elif command -v dnf >/dev/null 2>&1; then
        as_root dnf install -y -q git \
            || die "could not install git. Run this yourself, then run this installer again: sudo dnf install -y git"
    elif command -v pacman >/dev/null 2>&1; then
        as_root pacman -S --noconfirm --needed git \
            || die "could not install git. Run this yourself, then run this installer again: sudo pacman -S --needed git"
    else die "git is not installed and no known package manager was found; install git, then run this again"
    fi
fi
command -v git >/dev/null 2>&1 || die "git is still not installed"
ok "git"

# uv: its official installer, into ~/.local/bin; no password needed.
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    [ "$DRY" -eq 0 ] || die "uv is not installed (a real run installs it)"
    command -v curl >/dev/null 2>&1 || die "curl is needed to install uv; install curl, then run this again"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    command -v uv >/dev/null 2>&1 || die "uv did not install; see https://docs.astral.sh/uv/getting-started/installation/"
fi
ok "uv"

# The engine, so the installer that matches the release is the one that runs.
if [ ! -d "$ENGINE/.git" ]; then
    [ "$DRY" -eq 0 ] || { ok "would clone the engine into $ENGINE"; exit 0; }
    [ ! -e "$ENGINE" ] || [ -z "$(ls -A "$ENGINE" 2>/dev/null)" ] \
        || die "$ENGINE exists and is not the engine; move it aside, then run this again"
    if [ -z "$REF" ]; then
        # Release tags are vMAJOR.MINOR.PATCH from 3.1.0 on; the first three
        # were dated, v2026.10.1 to v2026.10.3, and count only while no
        # versioned tag exists. POSIX sort fields, not GNU `sort -V`, which the
        # stock macOS sort lacks. heading-install.py applies the same rule.
        TAGS="$(git ls-remote --tags --refs "$FROM" 'v*')" \
            || die "cannot list the releases of $FROM (network or URL); run again, or name one with --ref"
        REL="$(printf '%s\n' "$TAGS" | sed 's#.*refs/tags/##' \
            | { grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true; } | sed 's/^v//')"
        SEMVER="$(printf '%s\n' "$REL" | awk -F. 'NF && $1 + 0 < 1000')"
        [ -z "$SEMVER" ] || REL="$SEMVER"
        REF="$(printf '%s\n' "$REL" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n1)"
        [ -n "$REF" ] || die "$FROM has no release tag (vX.Y.Z) yet; to install a branch, name it: --ref main"
        REF="v$REF"
    fi
    mkdir -p "$TARGET"
    rmdir "$ENGINE" 2>/dev/null || true
    # Cloned AT the tag; never a clone and a checkout afterwards.
    git -c advice.detachedHead=false clone --quiet --branch "$REF" "$FROM" "$ENGINE"
    ok "engine $REF"
fi

exec uv run --quiet --python 3.11 --no-project "$ENGINE/scripts/heading-install.py" \
    --target "$TARGET" --from "$FROM" ${REF:+--ref "$REF"} "${PASS[@]+"${PASS[@]}"}"
