#!/usr/bin/env bash
#
# render-units.sh: the shared timezone and render steps of the systemd installers.
#
# Usage, in an installer, after $WORKSPACE and $PYTHON are resolved
# (resolve-python.sh owns the interpreter):
#
#     source "$(dirname "$0")/lib/render-units.sh"
#     TZ_VALUE="$(resolve_unit_tz "$WORKSPACE" "$PYTHON")"
#     render_unit "$TEMPLATE_DIR/$unit" "$DEST_DIR/$unit" \
#         "$WORKSPACE" "$PYTHON" "$TZ_VALUE" [extra sed -e expressions...]
#
# One copy of each, sourced by every installer, because a fix that lands in some
# of N hand-written copies is this repository's commonest defect shape.

# Prints the unit timezone. An explicit HEADING_OS_TZ wins. Otherwise the
# workspace resolver answers, because HEADING_OS_TZ lives in the gitignored .env
# and nothing exports it, so an environment-only read renders UTC on a machine
# whose timezone is configured. UTC when the resolver cannot answer.
#
# The resolver runs as a MODULE from the workspace root: running the file
# directly puts scripts/utils/ on sys.path[0], where operator.py shadows the
# stdlib operator module (fatal on Python 3.12, silently fine on 3.11).
resolve_unit_tz() {
  local workspace="${1:?resolve_unit_tz needs the workspace root}"
  local python="${2-}"
  if [ -n "${HEADING_OS_TZ:-}" ]; then
    printf '%s\n' "$HEADING_OS_TZ"
    return 0
  fi
  (cd "$workspace" && "$python" -m scripts.utils.paths tz || echo UTC)
}

# Renders one template into one unit file, substituting {{WORKSPACE}},
# {{PYTHON}} and {{TZ}}; any further arguments are passed to sed before the
# template, for an installer with placeholders of its own. Pipe delimiters,
# because the substituted paths contain slashes.
render_unit() {
  local src="${1:?render_unit needs a template}"
  local dest="${2:?render_unit needs a destination}"
  local workspace="$3" python="$4" tz="$5"
  shift 5
  sed -e "s|{{WORKSPACE}}|${workspace}|g" \
      -e "s|{{PYTHON}}|${python}|g" \
      -e "s|{{TZ}}|${tz}|g" \
      "$@" "$src" > "$dest"
}

# Sets TOOL_PATH to the installing shell's PATH unless the caller already set
# it, then refuses a value the unit cannot carry. TOOL_PATH lands inside
# `Environment="PATH=..."` (HOS-283), where a double quote or a backslash is a
# systemd escape and `%` is a specifier; before that, `|`, `&` and `\` break the
# sed substitution itself, and a newline or carriage return breaks both. None
# occurs in a real PATH, so refuse rather than escape. Exits 7 on refusal.
tool_path_or_refuse() {
  TOOL_PATH="${TOOL_PATH:-$PATH}"
  case "$TOOL_PATH" in
    *[\"\\%\|\&]* | *$'\n'* | *$'\r'*)
      echo "TOOL_PATH contains a character the unit cannot carry (\" \\ % | & or a line break):" >&2
      echo "  $TOOL_PATH" >&2
      echo "Set TOOL_PATH=... to a value without them and re-run." >&2
      exit 7
      ;;
  esac
}
