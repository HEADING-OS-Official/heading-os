#!/usr/bin/env bash
#
# yard-teardown.sh: on Herdr's `worktree.removed`, removes the deleted YARD's
# scratch directory under the data root, but only when its branch is merged
# into `main`. An unmerged yard keeps its scratch and the plugin log says why.
#
# The logic is `scripts/yard-scratch-cleanup.py`; this only finds it. The
# plugin command runs with HERDR_PLUGIN_ROOT as its cwd and never the worktree,
# which is gone by now, so the engine clone is located from this file, three
# levels up, and the merge question is asked of that clone's `main`.
set -uo pipefail

ENGINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)" || exit 1
cd "$ENGINE_ROOT" || exit 1
exec .venv/bin/python scripts/yard-scratch-cleanup.py "$@"
