#!/usr/bin/env bash
#
# yard-teardown.sh: on Herdr's `worktree.removed`, archives the deleted YARD's
# scratch directory: it is moved, whole, into the data registry's
# `yard-scratch-archive`, merged branch or not, and never deleted (HOS-413).
#
# The logic is `scripts/yard-scratch-cleanup.py`; this only finds it. The
# plugin command runs with HERDR_PLUGIN_ROOT as its cwd and never the worktree,
# which is gone by now, so the engine clone is located from this file, three
# levels up, and the merge state it logs is asked of that clone's `main`.
set -uo pipefail

ENGINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)" || exit 1
cd "$ENGINE_ROOT" || exit 1
exec .venv/bin/python scripts/yard-scratch-cleanup.py "$@"
