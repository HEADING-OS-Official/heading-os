#!/usr/bin/env bash
#
# resolve-python.sh — the single owner of "which interpreter does this unit run?"
#
# Usage, in an installer, after $WORKSPACE is resolved:
#
#     source "$(dirname "$0")/lib/resolve-python.sh"
#     PYTHON="$(resolve_python "$WORKSPACE")"
#
# Priority, and it is the whole contract:
#
#   1. An explicit `PYTHON=` from the caller. Always wins, verbatim, including a
#      path that does not exist -- the caller's installer reports that in its own
#      words, and a helper that second-guesses an explicit override is a helper
#      that silently installs a unit the operator did not ask for.
#   2. `$WORKSPACE/.venv/bin/python`, the pinned toolchain, when it is there.
#      Modern Linux (Ubuntu 24.04+, Fedora 38+) enforces PEP 668 against system
#      Python, so the venv interpreter is preferred over a bare python3 even
#      where the bare one would import.
#   3. The first `python3`/`python` on PATH, when the venv is genuinely absent.
#      May be empty; the caller decides what to do about that.
#
# WHY THIS FILE EXISTS
#
# MEASURED 2026-09-06: seven installed units ran on /usr/bin/python3 instead of
# the pinned .venv/bin/python, because half the installers that render
# {{PYTHON}} baked the first python on PATH into ExecStart=. Hence one function,
# sourced by every installer, rather than a hand-written copy in each. Nothing
# catches a new installer that writes its own copy instead (no test since the
# 2026-09-21 test-suite split).

resolve_python() {
  local workspace="${1:?resolve_python needs the workspace root}"

  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "$PYTHON"
    return 0
  fi

  if [ -x "$workspace/.venv/bin/python" ]; then
    printf '%s\n' "$workspace/.venv/bin/python"
    return 0
  fi

  # May print nothing. An installer that cannot proceed without an interpreter
  # checks for the empty string itself and exits with its own message.
  printf '%s\n' "$(command -v python3 || command -v python || true)"
  return 0
}
