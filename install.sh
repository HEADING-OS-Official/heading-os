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
#                   [--proxy-signin now|later] [--answers FILE] [--consent VALUE]
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
#   --consent VALUE what you agree to before anything on this computer changes:
#                   all, no, or a comma list of the groups system, home,
#                   downloads and services. Without it the installer shows its
#                   plan and asks, one group at a time; with --answers it is
#                   required, because a run from a file never stops to ask
set -euo pipefail

FROM="https://github.com/HEADING-OS-Official/heading-os.git"
TARGET="$HOME/HeadingOS"
REF=""
PASS=()
ANSWERS=""
CONSENT=""
HAVE_CONSENT=0
KEEP=()  # every argument as given, minus --consent and its value: the base of a consent question's `next`
ORIG=("$@")  # every argument as given, --consent included: the `next` of the free-space refusal

die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
warn() { printf '\033[33m  warn\033[0m %s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="${2:?--target needs a path}"; KEEP+=("$1" "$2"); shift 2 ;;
        --ref) REF="${2:?--ref needs a tag or branch}"; KEEP+=("$1" "$2"); shift 2 ;;
        --from) FROM="${2:?--from needs a git URL}"; KEEP+=("$1" "$2"); shift 2 ;;
        --github|--dry-run) PASS+=("$1"); KEEP+=("$1"); shift ;;
        --mail|--github-backup|--tz|--proxy-signin) PASS+=("$1" "${2:?$1 needs a value}"); KEEP+=("$1" "$2"); shift 2 ;;
        --answers) ANSWERS="${2:?--answers needs a file}"; KEEP+=("$1" "$2"); shift 2 ;;
        --consent) CONSENT="${2:?--consent needs a value}"; HAVE_CONSENT=1; shift 2 ;;
        -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
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

# ---- Consent (HOS-340): the plan, and the person's yes, before the first change ----
# Everything above only reads this computer. The four groups, in the order they are
# asked, with the same words as CONSENT_GROUPS in scripts/utils/install_answers.py.
CONSENT_GROUPS="system home downloads services"
group_words() {
    case "$1" in
        system) printf '%s' "installing system packages with your computer password" ;;
        home) printf '%s' "putting files in your home folder" ;;
        downloads) printf '%s' "downloading from the internet" ;;
        services) printf '%s' "running programs and timers in the background" ;;
    esac
}
valid_consent() {  # all, no, or a comma list of distinct group names
    case "$1" in all|no) return 0 ;; ""|,*|*,|*,,*|*[!a-z,]*) return 1 ;; esac
    _seen=","
    _rest="$1,"
    while [ -n "$_rest" ]; do
        _g="${_rest%%,*}"
        _rest="${_rest#*,}"
        case " $CONSENT_GROUPS " in *" $_g "*) ;; *) return 1 ;; esac
        case "$_seen" in *",$_g,"*) return 1 ;; esac
        _seen="$_seen$_g,"
    done
}
agreed() { [ "$CONSENT" = all ] || case ",$CONSENT," in *",$1,"*) true ;; *) false ;; esac; }
# One JSON string: backslash and double quote escaped, newline and tab as \n and \t,
# every other byte below 0x20 as \u00XX; every other byte as it is.
json_str() {
    printf '%s' "$1" | LC_ALL=C od -An -v -tu1 | LC_ALL=C awk '
        BEGIN { printf "\"" }
        { for (i = 1; i <= NF; i++) { b = $i + 0
            if (b == 34) printf "\\\""
            else if (b == 92) printf "\\\\"
            else if (b == 10) printf "\\n"
            else if (b == 9) printf "\\t"
            else if (b < 32) printf "\\u00%02x", b
            else printf "%c", b } }
        END { printf "\"" }'
}
# One word for a shell command line, quoted the way Python's shlex.quote does.
shq() {
    case "$1" in
        "") printf "''" ;;
        *[!A-Za-z0-9@%+=:,./_-]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\"'\"'/g")" ;;
        *) printf '%s' "$1" ;;
    esac
}
# The last line of every run that stops here, the shape scripts/heading-install.py prints.
status() {  # state say next [options as JSON]
    printf 'HEADING-INSTALL: {"state": %s, "say": %s, "next": %s' "$(json_str "$1")" "$(json_str "$2")" \
        "$(json_str "$3")"
    [ -z "${4:-}" ] || printf ', "options": %s' "$4"
    printf '}\n'
}
plan_block() {
    case "$1" in
        system)
            if [ -z "$PKGS" ]; then
                printf '%s\n' "Nothing: everything this group needs is already installed."
            elif [ "$(uname -s)" = Darwin ]; then
                printf '%s\n' "- Apple's Command Line Tools, which include git; macOS asks for them in its own window."
            else
                printf '%s\n' "- The system packages $PKGS, through a system window that asks for your computer password once."
            fi ;;
        home)
            printf '%s\n' \
                "- The uv tool, at ~/.local/bin/uv, when it is missing." \
                "- The folder $TARGET, with engine/ (HEADING OS itself) and data/ (your own private data)." \
                "- Claude Code settings in ~/.claude and ~/.claude.json: the plugins HEADING OS uses, and its connections to your memory and to the issue tracker." \
                "- Tools in ~/.local/bin: herdr, lific, lific-mcp, hindsight and heading." \
                "- Programs in ~/.local/share/heading-os: Node.js, CLIProxyAPI, ollama with its search model, Hindsight, the Hindsight web console and AgentsView." \
                "- Settings in ~/.config/heading-os, ~/.config/herdr, ~/.config/lific, ~/.config/hindsight and ~/.hindsight, and model sign-ins in ~/.cli-proxy-api." \
                "- A few lines in your shell startup files, such as ~/.profile and ~/.bashrc, so a new terminal finds these tools and your data." \
                "- At the end, $TARGET/installed.txt: what was installed, and how to remove each part by hand." ;;
        downloads)
            printf '%s\n' \
                "- astral.sh: the uv tool, when it is missing." \
                "- github.com: HEADING OS itself; the releases of Herdr, CLIProxyAPI, ollama, Lific, Hindsight's command and AgentsView; the Claude Code plugin catalogue; and, if you choose a GitHub backup, your private copy there." \
                "- nodejs.org: Node.js, when Node.js 22 or newer is missing." \
                "- pypi.org and files.pythonhosted.org: the Python libraries of HEADING OS and of Hindsight." \
                "- registry.npmjs.org: the Hindsight web console; and through npx, the Claude Code memory plugin." \
                "- Through ollama: the search language model bge-m3, about 1.2 GB." \
                "- Through uv: Python 3.11, when it is missing. Hindsight also fetches its own embedding models the first time it starts." \
                "- api.anthropic.com: the list of Claude models, only when an Anthropic API key is set." \
                "- Google, only if you connect Google mail: the sign-in, Gmail and Google Calendar." \
                "- In total about 13 GB on disk once installed; 15 GB free is needed to start." ;;
        services)
            printf '%s\n' \
                "- On Linux as systemd user services and timers in ~/.config/systemd/user, on macOS as launchd agents in ~/Library/LaunchAgents. On Linux they keep running while you are logged out (linger is turned on for your account)." \
                "- Services: cliproxyapi, embedding-server (ollama), lific, agentsview, hindsight-server and hindsight-control-plane." \
                "- Timers: memory-index-refresh, hindsight-sync (with hindsight-capture-check), hindsight-snapshot, proxy-usage-dashboard, archive-transcripts, chronicle, council-models-check, daemon-watchdog, datastore-map, dream-shadow, inference-guard, memory-hygiene, odin-propose, ops-radar, recall-calibration, reminders, router-accuracy, and on Linux update-manager." \
                "- Only if you connect Exchange mail: sentinel and sync-exchange-daemon, which read your mailbox." ;;
    esac
}
plan_section() {  # one group: its words, then its block
    _w="$(group_words "$1")"
    printf '%s%s:\n' "$(printf '%s' "$_w" | cut -c1 | tr '[:lower:]' '[:upper:]')" "$(printf '%s' "$_w" | cut -c2-)"
    plan_block "$1"
}

# ---- Free space (HOS-341): after the options and the platform, before consent and any change ----
# The checks above it only read this computer; it sits here because it needs shq and status.
# 15 GiB free where the install goes. MEASURED by HELM on test VM 101 after a 47 of 47
# install on 2026-11-03 (du -sh): ~/.local 11G, ~/HeadingOS 1.1G, ~/.cache 408M,
# ~/.claude 23M, about 13 GB in all; 15 GB leaves room for the downloads before they unpack.
# GB here is 1024^3 bytes, as df -h counts.
MIN_FREE_KB=15728640
if [ "$DRY" -eq 0 ]; then
    DF_AT="$TARGET"
    while [ ! -e "$DF_AT" ]; do DF_AT="$(dirname "$DF_AT")"; done
    # The available column is the one before Capacity (NN%), so a device name with a space cannot shift it.
    FREE_KB="$(df -Pk "$DF_AT" 2>/dev/null | awk 'NR == 2 { for (i = 2; i <= NF; i++) if ($i ~ /^[0-9]+%$/) { print $(i - 1); exit } }')"
    case "$FREE_KB" in
        ""|*[!0-9]*) warn "could not read the free space at $DF_AT; the install needs about 15 GB" ;;
        *)
            if [ "$FREE_KB" -lt "$MIN_FREE_KB" ]; then
                SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
                NEXT="bash $(shq "$SELF")"
                for a in ${ORIG[@]+"${ORIG[@]}"}; do NEXT="$NEXT $(shq "$a")"; done
                SAY="HEADING OS needs 15 GB free to start (it uses about 13 GB once installed), and this computer has $((FREE_KB / 1048576)) GB free where it would install. Free some space, then run this again."
                printf '%s\n' "$SAY"
                status failed "$SAY" "$NEXT"
                exit 1
            fi ;;
    esac
fi

if [ "$HAVE_CONSENT" -eq 1 ] && ! valid_consent "$CONSENT"; then
    die "--consent takes all, no, or a comma list of system, home, downloads and services, each once (not: $CONSENT)"
fi
if [ "$HAVE_CONSENT" -eq 1 ] && [ "$CONSENT" = no ]; then
    printf '%s\n' "Nothing was changed."
    status stopped "You stopped the install before it changed anything. Nothing was installed." ""
    exit 6
fi
if [ -n "$ANSWERS" ] && [ "$HAVE_CONSENT" -eq 0 ]; then
    status failed "An install run from an answers file never stops to ask. Add --consent all, or the list of groups you agree to, to the command." ""
    exit 2
fi
ALL=1
for g in $CONSENT_GROUPS; do agreed "$g" || ALL=0; done
[ "$ALL" -eq 0 ] || CONSENT=all
FINAL=()  # added to the handover only once consent covers all four groups
[ "$CONSENT" != all ] || FINAL=(--consent all)
if [ "$DRY" -eq 0 ] && [ "$CONSENT" != all ] && [ -n "$ANSWERS" ]; then
    # A run from a file never asks: it refuses a group this script itself would need, and hands
    # the list on, so heading-install.py refuses each later step whose group it leaves out.
    NEED=""
    [ -z "$PKGS" ] || NEED="system"
    if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ] || [ ! -d "$ENGINE/.git" ]; then
        NEED="$NEED home downloads"
    fi
    # The handover `uv run --python 3.11` downloads Python when uv finds none; `uv python find` never downloads.
    UVBIN="$(command -v uv 2>/dev/null || true)"
    [ -n "$UVBIN" ] || UVBIN="$HOME/.local/bin/uv"
    if [ -x "$UVBIN" ] && ! "$UVBIN" python find 3.11 >/dev/null 2>&1; then
        NEED="$NEED downloads"
    fi
    for g in $NEED; do
        agreed "$g" || { status failed "The answers file gives no consent for $(group_words "$g"), and an install run from a file never stops to ask." ""; exit 2; }
    done
    FINAL=(--consent "$CONSENT")
elif [ "$DRY" -eq 0 ] && [ "$CONSENT" != all ]; then
    ASK=""
    for g in $CONSENT_GROUPS; do agreed "$g" || { ASK="$g"; break; }; done
    SAY=""
    if [ "$HAVE_CONSENT" -eq 0 ]; then
        SAY="Before anything on this computer changes, here is what the install will do, in four groups. You agree to each group on its own."
        for g in $CONSENT_GROUPS; do SAY="$SAY

$(plan_section "$g")"; done
    else
        SAY="$(plan_section "$ASK")"
    fi
    SAY="$SAY

Do you agree to $(group_words "$ASK")?"
    YES="${CONSENT:+$CONSENT,}$ASK"
    SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    NEXT="bash $(shq "$SELF")"
    for a in ${KEEP[@]+"${KEEP[@]}"}; do NEXT="$NEXT $(shq "$a")"; done
    NEXT="$NEXT --consent {answer}"
    printf '%s\n' "$SAY"
    status need-answer "$SAY" "$NEXT" \
        "[{\"label\": \"Yes\", \"value\": $(json_str "$YES")}, {\"label\": \"Yes to all steps, do not ask again\", \"value\": \"all\"}, {\"label\": \"No, stop here\", \"value\": \"no\"}]"
    exit 3
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
    --target "$TARGET" --from "$FROM" ${REF:+--ref "$REF"} "${PASS[@]+"${PASS[@]}"}" \
    ${FINAL[@]+"${FINAL[@]}"}
