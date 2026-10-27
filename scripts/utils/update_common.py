#!/usr/bin/env python3
"""Shared update-manager helpers: current-version resolution, version
comparison, and how old the recorded reading is. Lives in one place because
scripts/update-manager.py is a hyphenated CLI that cannot be imported, yet
scripts/utils/update_apply.py needs the same current-version logic. Prevents the
two copies from drifting.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scripts.utils import update_sources
from scripts.utils.atomic import atomic_write_text
from scripts.utils.update_registry import Component

# Probes whose command names a path inside the engine (the venv) run from here.
ENGINE_ROOT = Path(__file__).resolve().parent.parent.parent


def write_state(state: dict, path: Path) -> None:
    """Atomic state write through `atomic_write_text`. Shared by the CLI
    (`check`) and the apply module (`_mark_state`)."""
    atomic_write_text(path, json.dumps(state, indent=2))


def resolve_current(comp: Component) -> str:
    """The installed version of `comp`, or "" when the probe did not answer.

    `current.via` picks the probe, `shell` when absent:

    - `shell`: run `current.cmd`; with a `regex`, its first capture group, else
      the first output line. A regex that does not match yields "" (unknown)
      rather than a misleading line of noise.
    - `outdated_count`: run `current.cmd`, which must print a JSON array of the
      outdated parts of an aggregate component (`uv pip list --outdated
      --format json`); the answer is `"<n> outdated"`. Anything that is not a
      JSON array is "", because a failed listing counted as zero would report a
      venv nobody measured as up to date.
    - `claude_plugins`: `"<n> outdated"` for the Claude Code plugins, from the
      two local files Claude Code itself keeps; see `claude_plugins_outdated`.
    """
    # Every failure inside here is "unknown", which is what "" means. Only
    # TimeoutExpired was caught once, so two ordinary config-data faults
    # escaped a function documented to answer "" rather than raise. Measured
    # 2026-08-30: a component whose `current.regex` is `v(\d+` (an unbalanced
    # paren, and the registry is hand-edited YAML) raised `re.error` out of
    # `resolve_current`, and no `bash` on PATH would raise OSError the same
    # way. A third, a strict decode of a non-UTF-8 banner, was MEASURED
    # 2026-09-01. All three are now handled once, in
    # `update_sources.run_probe`, which the `latest: {via: shell}` side shares.
    via = comp.current.get("via", "shell")
    if via == "shell":
        return update_sources.run_probe(comp.current.get("cmd", ""),
                                        comp.current.get("regex"), timeout=30)
    if via == "outdated_count":
        return _outdated_count(comp.current.get("cmd", ""))
    if via == "claude_plugins":
        return claude_plugins_outdated(Path(comp.current.get("plugins_dir")
                                            or Path.home() / ".claude" / "plugins"))
    return ""


def _outdated_count(cmd: str) -> str:
    """`"<n> outdated"` from a command printing a JSON array, else ""."""
    try:
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                             errors="replace", timeout=120, check=False,
                             cwd=str(ENGINE_ROOT))
    except (subprocess.SubprocessError, OSError):
        return ""
    if res.returncode != 0:
        return ""
    try:
        rows = json.loads(res.stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(rows, list):
        return ""
    return f"{len(rows)} outdated"


def _marketplace_sha(location: Path) -> str:
    """The commit a marketplace checkout is at, or "".

    A git checkout answers through `.git/HEAD`; the official marketplace is
    fetched as an archive with no `.git` and records its commit in `.gcs-sha`
    instead (observed 2026-09-26). Anything else is "", never a guess.
    """
    try:
        gcs = location / ".gcs-sha"
        if gcs.is_file():
            return gcs.read_text(encoding="utf-8").strip()
        head = (location / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            return (location / ".git" / head[5:]).read_text(encoding="utf-8").strip()
        return head
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _plugin_behind(inst: dict, entry: dict, market: Path) -> bool | None:
    """Whether one installed plugin lags its marketplace entry; None = unreadable.

    The version Claude Code records for an install is the plugin's own
    `plugin.json` version, else the catalog entry's `version`, else a commit:
    the pinned `source.sha` for a remote source, the marketplace's commit for a
    plugin that lives inside it (observed 2026-09-26 in `installed_plugins.json`
    schema 2, where SHA-versioned installs carry the first 12 hex digits).
    Every field is type-checked before use: these are another program's files,
    and a shape this reader does not know is unread, never current.
    """
    source = entry.get("source")
    installed_ver = inst.get("version") if isinstance(inst.get("version"), str) else ""
    installed_sha = inst.get("gitCommitSha") \
        if isinstance(inst.get("gitCommitSha"), str) else ""
    catalog_ver = entry.get("version") if isinstance(entry.get("version"), str) else ""
    if isinstance(source, dict):
        want_sha = source.get("sha") if isinstance(source.get("sha"), str) else ""
        if want_sha and installed_sha:
            return not want_sha.startswith(installed_sha)
        return (installed_ver != catalog_ver) if (catalog_ver and installed_ver) else None
    if isinstance(source, str) and source:
        own = _read_json(market / source / ".claude-plugin" / "plugin.json")
        own_ver = own.get("version") if isinstance(own, dict) else None
        want = own_ver if isinstance(own_ver, str) and own_ver else catalog_ver
        if want:
            return (installed_ver != want) if installed_ver else None
        want_sha = _marketplace_sha(market)
        have = installed_sha or installed_ver
        return (not want_sha.startswith(have)) if (want_sha and have) else None
    return None


def _catalog(plugins_dir: Path, markets, market_name: str) -> tuple[Path, dict] | None:
    """A marketplace's checkout and its entries by name, or None if unreadable."""
    info = markets.get(market_name) if isinstance(markets, dict) else None
    if info is None:
        loc = plugins_dir / "marketplaces" / market_name
    elif isinstance(info, dict) and isinstance(info.get("installLocation"), str) \
            and info["installLocation"]:
        loc = Path(info["installLocation"])
    else:
        return None
    cat = _read_json(loc / ".claude-plugin" / "marketplace.json")
    if not isinstance(cat, dict) or not isinstance(cat.get("plugins"), list):
        return None
    entries = {p["name"]: p for p in cat["plugins"]
               if isinstance(p, dict) and isinstance(p.get("name"), str)}
    return loc, entries


def claude_plugins_outdated(plugins_dir: Path) -> str:
    """`"<n> outdated"` over the installed Claude Code plugins, from local files.

    Reads `installed_plugins.json` and each marketplace's catalog, both kept
    by Claude Code itself, so no network is touched; the answer is therefore
    lag behind the LOCAL catalog, as fresh as Claude Code's last marketplace
    refresh. An install is counted once per distinct (plugin, version,
    commit): the same plugin installed at one version into many projects is
    one thing to update, not many. An install whose lag cannot be read is
    counted as unread and SAID, as `"<n> outdated, <u> unread"`, because
    counting it current would be a guess. "" when the install list itself
    cannot be read or is not the schema-2 shape this reader knows.
    """
    installed = _read_json(plugins_dir / "installed_plugins.json")
    if not isinstance(installed, dict) or installed.get("version") != 2 \
            or not isinstance(installed.get("plugins"), dict):
        return ""
    markets = _read_json(plugins_dir / "known_marketplaces.json")
    catalogs: dict[str, tuple[Path, dict] | None] = {}
    outdated = unread = 0
    seen: set[tuple[str, str, str]] = set()
    for key, installs in installed["plugins"].items():
        name, _, market_name = str(key).partition("@")
        if not isinstance(installs, list) or not installs:
            unread += 1
            continue
        if market_name not in catalogs:
            catalogs[market_name] = _catalog(plugins_dir, markets, market_name)
        catalog = catalogs[market_name]
        for inst in installs:
            if not isinstance(inst, dict):
                unread += 1
                continue
            ident = (str(key), str(inst.get("version")), str(inst.get("gitCommitSha")))
            if ident in seen:
                continue
            seen.add(ident)
            entry = catalog[1].get(name) if catalog else None
            behind = _plugin_behind(inst, entry, catalog[0]) \
                if (catalog and isinstance(entry, dict)) else None
            if behind is None:
                unread += 1
            elif behind:
                outdated += 1
    return f"{outdated} outdated" + (f", {unread} unread" if unread else "")


def _norm_version(v: str):
    """Normalize for comparison so 2026.07.20 == 2026.7.20."""
    try:
        from packaging.version import Version  # noqa: PLC0415
        return ("v", str(Version(v)))
    except Exception:  # noqa: BLE001 - unparseable/absent -> compare raw stripped
        return ("s", v.strip())


def versions_differ(current: str, latest: str) -> bool:
    if not current or not latest:
        return False
    return _norm_version(current) != _norm_version(latest)


def classify(comp: Component, current: str, latest: str,
             fail_count: int) -> tuple[str, int]:
    """`(status, fail_count)` for one reading. Shared by `check` and by `apply`,
    which records the version its probe reads after the swap: until 2026-10-01
    only `check` classified, so a successful apply left the pre-apply reading in
    the state until the next day's check (MEASURED 2026-10-01: `status` printed
    `8.0.4 -> 8.0.7 pending-auto` while the binary answered 8.0.7)."""
    if comp.hold or comp.pin:
        return "held", fail_count
    if not latest or not current:
        return "unknown", fail_count      # a broken probe is unknown, never "current"
    if not versions_differ(current, latest):
        return "current", 0
    if comp.tier == "observed":
        return "observed-stale", fail_count   # informational only; observed self-updates
    if comp.tier == "notify":
        return "waiting", fail_count
    if fail_count > 0:
        return "failed", fail_count        # breaker remembers a prior auto failure while it still lags
    return "pending-auto", fail_count


# ============================================================
# How old the recorded reading is, and whether that can be answered at all
# ============================================================
#
# `status` reads the state file and polls nothing, so every version it prints
# was measured at some earlier moment. Printing them with no moment attached is
# how a reader acts on a two-day-old table believing it is today's. MEASURED
# 2026-09-20 on the operator's machine: two components showed an empty
# `current` cell, which was read as two broken probes and filed as a defect; a
# `check` run in the same hour filled both cells (`yt-dlp` empty then
# `2026.08.19`, `claude-cli` empty then `2.1.277`). The probes were fine. The
# RECORD was old, because the timer fleet had been off since 19 September, and
# nothing in the output said so.
#
# Three states, never two, and the third is the point: a reading that cannot be
# dated must never be dressed as a fresh one or as a stale one.

FRESH = "fresh"          # measured within one scheduled cycle
STALE = "stale"          # measured longer ago than that, and not refreshed since
UNDATED = "undated"      # the age, or the cadence to judge it against, is unknown

# The two words a cell uses when it holds no version, kept apart on purpose.
# An empty cell blends them, and they call for opposite actions: one is a probe
# to repair, the other is a `check` to run.
CELL_NOT_ANSWERED = "not answered"      # the probe ran at the reading's time, and gave nothing
CELL_NEVER_CHECKED = "never checked"    # in the registry, absent from the state: no probe has run

TIMER_TEMPLATE_REL = "scripts/templates/systemd/update-manager.timer"

# A daily OnCalendar, seconds optional, trailing timezone optional:
# `OnCalendar=*-*-* 07:00:00 Europe/Berlin`. Deliberately narrow. Any other
# expression is answered "this cadence cannot be read" rather than guessed at,
# because a period guessed too wide turns a silent timer into a fresh reading.
_DAILY_ONCALENDAR = re.compile(
    r"^\s*OnCalendar\s*=\s*\*-\*-\*\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s+\S+)?\s*$")


def check_period_hours(workspace_root: Path) -> tuple[float | None, str]:
    """Hours between two scheduled `check` runs, READ from the timer unit this
    repository ships, plus a sentence naming where the number came from.

    The threshold for "this reading is old" is not a round number chosen by
    hand: it is the writer's own cadence. A reading younger than one period is
    what a machine whose timer fires produces; a reading older than one period
    means at least one scheduled `check` did not land, which is the condition
    the operator needs to know about. `(None, reason)` when the unit cannot be
    read or declares a cadence this parser does not recognise, so a changed
    cadence produces "I cannot say" rather than a stale constant living on here.
    """
    path = workspace_root / TIMER_TEMPLATE_REL
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{TIMER_TEMPLATE_REL} could not be read ({exc})"
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("OnCalendar")]
    if len(lines) != 1:
        return None, (f"{TIMER_TEMPLATE_REL} declares {len(lines)} OnCalendar "
                      f"lines, so the cadence is not one interval")
    if not _DAILY_ONCALENDAR.match(lines[0]):
        return None, (f"{TIMER_TEMPLATE_REL} declares a cadence this reader "
                      f"cannot parse: {lines[0].strip()}")
    return 24.0, f"{lines[0].strip()}, in {TIMER_TEMPLATE_REL}"


def parse_stamp(raw) -> datetime | None:
    """The state's `generated` field as an aware UTC datetime, or None.

    A naive stamp is read as UTC, which is what `check` writes; anything else
    is None rather than a guess, and None is what routes the reading to
    `UNDATED`.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        stamp = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def human_age(seconds: float) -> str:
    """An elapsed span as `2d 3h`, `3h 12m`, `12m` or `45s`, always rounded
    DOWN, so the printed age never overstates how recent a reading is."""
    seconds = int(max(seconds, 0))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def reading_freshness(state: dict, workspace_root: Path,
                      now: datetime | None = None) -> dict:
    """Classify the recorded reading as FRESH, STALE or UNDATED, and write the
    one line that says which, in words, with the age in the same line.

    Returned: `state`, `age_seconds` (None when undated), `stamp`,
    `period_hours` (None when the cadence could not be read) and `line`. The
    line lives here rather than at the print site so a second reader of this
    state file says the identical thing.
    """
    now = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    raw = (state or {}).get("generated")
    stamp = parse_stamp(raw)
    period, period_source = check_period_hours(workspace_root)
    rerun = "re-measure: python scripts/update-manager.py check"
    if stamp is None:
        detail = ("carries no `generated` stamp" if not raw
                  else f"carries a `generated` stamp that cannot be read ({raw!r})")
        return {"state": UNDATED, "age_seconds": None, "stamp": None,
                "period_hours": period,
                "line": (f"reading: AGE UNKNOWN, the state file {detail}. The "
                         f"versions below cannot be dated, which is not the same "
                         f"as their being current; {rerun}")}
    age = (now - stamp).total_seconds()
    when = stamp.strftime("%Y-%m-%d %H:%M UTC")
    if age < 0:
        # A stamp in the future is a clock that moved, and a reading whose age
        # cannot be computed is undated. Calling it fresh would be the exact
        # dressing-up this function exists to refuse.
        return {"state": UNDATED, "age_seconds": None, "stamp": stamp,
                "period_hours": period,
                "line": (f"reading: AGE UNKNOWN, the state file is stamped "
                         f"{when}, which is in the future, so the clock that "
                         f"wrote it and this one disagree; {rerun}")}
    if period is None:
        return {"state": UNDATED, "age_seconds": age, "stamp": stamp,
                "period_hours": None,
                "line": (f"reading: versions measured {when}, {human_age(age)} "
                         f"ago. Whether that is stale is UNKNOWN, because the "
                         f"scheduled cadence could not be established "
                         f"({period_source})")}
    if age >= period * 3600:
        return {"state": STALE, "age_seconds": age, "stamp": stamp,
                "period_hours": period,
                "line": (f"reading: versions measured {when}, {human_age(age)} "
                         f"ago. STALE: older than the {period:g}h between "
                         f"scheduled checks, so at least one check did not land "
                         f"and nothing below has been re-measured since; "
                         f"{rerun}")}
    return {"state": FRESH, "age_seconds": age, "stamp": stamp,
            "period_hours": period,
            "line": (f"reading: versions measured {when}, {human_age(age)} ago. "
                     f"Fresh: within the {period:g}h between scheduled checks")}
