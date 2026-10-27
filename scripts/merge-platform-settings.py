#!/usr/bin/env python3
"""Merge a platform settings template into the live settings, losing nothing.

Replaces a plain `cp "$TEMPLATE" "$TARGET"` in `scripts/setup-platform.sh`.
MEASURED 2026-09-02 against the live workspace: one such copy would have
discarded 29 permission entries and three per-instance top-level keys
(`autoMemoryDirectory`, `outputStyle`, `enabledPlugins`), and losing the first
silently redirects every memory write.

The rule that decides every case: **the template proposes, the live file
disposes.** A key the live file already has keeps its live value. A key only the
template has is added. Permission lists are unioned, because a permission is a
grant and dropping one breaks a workflow that used to run. Standard library
only, so it runs on a fresh clone before `.venv` exists.

A hook REGISTRATION is **(event, matcher, hook identity)**, the identity being
the `*.py` token in the command, else the whole command. The matcher decides
which tool calls a hook sees, so two groups differing only in matcher are two
walls; the command as a whole is not the identity, because its bootstrap has
been rewritten without any hook changing. Asking per event name instead missed
registrations added inside an existing event (2026-09-21). The merge and
`--check` share that one function, so what the check calls missing is exactly
what the install adds.

`--check` names every registration the live file lacks and exits 1 when the
live file is absent or short (`.claude/settings.local.json` is gitignored and is
where the session hooks are registered). It says a hook is REGISTERED, not that
it RAN.

Timeouts are raised to the template's, never lowered (2026-10-06: a raised
template timeout never reached the live file and `--check` said armed). A live
timeout at or above the template's is kept; `--check` names every one below it.

Apart from that raise, the merge only APPENDS. It never removes a live group, so
when a template rewrites a matcher (`Read` to `Read|Grep|Glob`) the new group
lands beside the old one and those tools fire the hook twice; removing the stale
group is a decision about deleting local content and is not taken here.

The backup taken before a write goes to this checkout's scratch directory in
the data repository (registry name `checkout-scratch`); beside the live file
only where that cannot be resolved (no data repository, or no `.venv` yet).
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Lists under `permissions` are GRANTS. Union rather than replace: a permission
# present locally and absent from the template is one the operator added on
# purpose, and dropping it breaks a workflow that used to run without asking.
PERMISSION_LISTS = ("allow", "deny", "ask")

#: Any `*.py` token inside a hook's command string. The commands wrap the hook
#: in a `python3 -c` bootstrap, so the script name is the only stable part of
#: them; the surrounding bootstrap differs between platforms and has been
#: rewritten twice without any hook changing.
_PY_TOKEN = re.compile(r"[\w.-]+\.py")

#: A double-quoted span, which is where the bootstrap program sits. What is left
#: outside it are the hook's own arguments.
_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"')


def _matcher_of(group: object) -> str:
    """A group's matcher, normalised. A missing one is its own value, `""`."""
    if not isinstance(group, dict):
        return ""
    matcher = group.get("matcher")
    return matcher if isinstance(matcher, str) else ""


def _entry_ids(event: object, matcher: str, entry: object) -> set[tuple[str, str, str]]:
    """The registrations one hook entry makes, as (event, matcher, identity).

    An entry naming no `*.py` file at all falls back to its whole command with
    whitespace collapsed. Without that fallback a hook that is not a Python
    script has no identity, reads as already present, and is never installed --
    which is this script's own defect wearing different clothes.

    The `--flags` passed to the hook outside the bootstrap's quotes are part
    of the identity (engine audit F-8.1, 2026-10-16): the template registers
    `checkpoint-offer.py` twice on `Stop`, once plain and once `--rewake`, and
    an identity of the script name alone read the second as present when only
    the first was, so `--check` said armed over a missing registration.
    """
    if not isinstance(entry, dict):
        return set()
    command = entry.get("command")
    if not isinstance(command, str):
        return set()
    names = set(_PY_TOKEN.findall(command))
    if not names:
        names = {"command:" + " ".join(command.split())}
    flags = sorted(t for t in _QUOTED.sub(" ", command).split()
                   if t.startswith("--") and t != "--")
    if flags:
        names = {f"{name} {' '.join(flags)}" for name in names}
    return {(str(event), matcher, name) for name in names}


def hook_registrations(settings: dict) -> set[tuple[str, str, str]]:
    """Every `(event, matcher, hook)` triple a settings mapping registers.

    Pure, and deliberately tolerant of a malformed `hooks` block: a settings
    file that has been hand-edited into a shape this does not recognise yields
    FEWER triples, so `--check` reports it as short rather than as armed and the
    merge offers the registration again. Failing toward over-reporting is the
    required direction here, per `.claude/rules/scope-claims.md`.

    The matcher is part of the identity because it decides which tool calls the
    hook ever sees. Five `PreToolUse` groups in the live template register the
    same `_dispatch.py` under five matchers; an identity without the matcher
    collapses them into one and reports four missing walls as armed.
    """
    out: set[tuple[str, str, str]] = set()
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                continue
            matcher = _matcher_of(group)
            for entry in entries:
                out |= _entry_ids(event, matcher, entry)
    return out


def _timeout_of(entry: object) -> float | None:
    """An entry's numeric `timeout`, or None when it has none."""
    if not isinstance(entry, dict):
        return None
    value = entry.get("timeout")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def template_timeouts(settings: dict) -> dict[tuple[str, str, str], float]:
    """The highest `timeout` the template gives each registration."""
    out: dict[tuple[str, str, str], float] = {}
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            matcher = _matcher_of(group)
            for entry in group["hooks"]:
                t = _timeout_of(entry)
                if t is None:
                    continue
                for rid in _entry_ids(event, matcher, entry):
                    out[rid] = max(out.get(rid, t), t)
    return out


def short_timeouts(template: dict, live: dict) -> list[tuple[tuple[str, str, str], float, float]]:
    """Every live registration whose timeout is below the template's: (registration, live, template)."""
    want = template_timeouts(template)
    out = []
    hooks = live.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            matcher = _matcher_of(group)
            for entry in group["hooks"]:
                have = _timeout_of(entry)
                if have is None:
                    continue
                for rid in sorted(_entry_ids(event, matcher, entry)):
                    if rid in want and want[rid] > have:
                        out.append((rid, have, want[rid]))
    return sorted(out)


def merge_hooks(t_hooks: dict, l_hooks: dict) -> dict:
    """Install every template registration the live file lacks, and nothing else.

    Per REGISTRATION, never per event name: see the module docstring for the two
    measurements that retired the event-name test. An event the live file does
    not have arrives whole, exactly as before. An event it does have keeps every
    group it has, byte for byte, and gains one appended group per template group
    that carries at least one registration the live file lacks -- carrying only
    the missing entries, so a partly-installed group cannot double-register the
    half that is already there.

    Appending a group rather than editing the live group of the same matcher is
    deliberate. It touches nothing local, the matcher travels with the entries
    so the behaviour is identical, and the harness runs the matching hooks of an
    event in parallel, so their order across groups carries no meaning. The live
    template already registers five `Stop` groups under one matcher, so this is
    the file's own shape and not a new one.
    """
    if not isinstance(l_hooks, dict):
        # A live `hooks` that is not an object registers nothing with the
        # harness, so there is nothing in it to preserve, and the template's
        # registrations must still land. The version this replaced raised a
        # TypeError here for every shape but `[]`, which turned a hand-edited
        # settings file into a traceback out of a setup script.
        l_hooks = {}
    if not isinstance(t_hooks, dict):
        return dict(l_hooks)

    # Deep-copied, because a timeout may be raised below and the caller's live
    # mapping must stay exactly what it read.
    out: dict = {event: copy.deepcopy(groups) for event, groups in l_hooks.items()}
    present = hook_registrations({"hooks": l_hooks})

    want = template_timeouts({"hooks": t_hooks})
    for event, groups in out.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            matcher = _matcher_of(group)
            for entry in group["hooks"]:
                have = _timeout_of(entry)
                if have is None:
                    continue
                target = max((want[r] for r in _entry_ids(event, matcher, entry) if r in want), default=None)
                if target is not None and target > have:
                    entry["timeout"] = target

    for event, groups in t_hooks.items():
        if event not in out:
            out[event] = copy.deepcopy(groups)
            present |= hook_registrations({"hooks": {event: groups}})
            continue
        if not isinstance(groups, list) or not isinstance(out[event], list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = _matcher_of(group)
            entries = group.get("hooks")
            entries = entries if isinstance(entries, list) else []
            missing = [entry for entry in entries
                       if _entry_ids(event, matcher, entry) - present]
            if not missing:
                continue
            landed = copy.deepcopy(group)
            landed["hooks"] = copy.deepcopy(missing)
            out[event].append(landed)
            for entry in missing:
                present |= _entry_ids(event, matcher, entry)

    return out


def merge_settings(template: dict, live: dict) -> dict:
    """Template proposes, live disposes. Pure: no I/O, no clock, no globals.

    Deliberately shallow apart from `permissions` and `hooks`. A deep merge of
    arbitrary nested JSON invents a policy for structures nobody has thought
    about, and the wrong guess there is as silent as the copy this replaces.
    Where a live value exists at the top level, it wins unexamined.
    """
    merged = dict(live)

    for key, value in template.items():
        if key not in merged:
            merged[key] = value

    # `permissions`: union each grant list, keep the live order, append the
    # template's additions. Order is preserved because these files are read by
    # humans and a reshuffled list produces a diff nobody can review.
    t_perms = template.get("permissions") or {}
    l_perms = merged.get("permissions") or {}
    if t_perms or l_perms:
        out = dict(l_perms)
        for name in PERMISSION_LISTS:
            live_entries = list(l_perms.get(name, []))
            seen = set(live_entries)
            for entry in t_perms.get(name, []):
                if entry not in seen:
                    live_entries.append(entry)
                    seen.add(entry)
            if live_entries or name in t_perms or name in l_perms:
                out[name] = live_entries
        for name, value in t_perms.items():
            if name not in out:
                out[name] = value
        merged["permissions"] = out

    # `hooks`: a REGISTRATION the template adds is new capability and must land,
    # whether or not its event already has a sibling. What identity a
    # registration has, and the two measurements behind it: the module
    # docstring and `merge_hooks`.
    t_hooks = template.get("hooks") or {}
    l_hooks = merged.get("hooks") or {}
    if t_hooks or l_hooks:
        merged["hooks"] = merge_hooks(t_hooks, l_hooks)

    return merged


def _load(path: Path, what: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read the {what} at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"the {what} at {path} is not valid JSON: {exc}. Refusing to "
            f"merge, because guessing what it meant is how the live file gets "
            f"replaced by a guess."
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(f"the {what} at {path} is not a JSON object")
    return data


def _describe(before: dict, after: dict) -> list[str]:
    """What the merge added, in the operator's terms."""
    lines = []
    new_keys = sorted(set(after) - set(before))
    if new_keys:
        lines.append(f"added top-level key(s): {', '.join(new_keys)}")
    b = before.get("permissions") or {}
    a = after.get("permissions") or {}
    for name in PERMISSION_LISTS:
        gained = len(a.get(name, [])) - len(b.get(name, []))
        if gained:
            lines.append(f"added {gained} `{name}` permission entry(ies)")
    gained_events = sorted(set(after.get("hooks") or {}) - set(before.get("hooks") or {}))
    if gained_events:
        lines.append(f"added hook event(s): {', '.join(gained_events)}")
    # Per registration, not per event: the whole point of this change is that a
    # registration can land inside an event that was already there, and a report
    # that only counts new event names would stay silent about exactly that.
    for event, matcher, name in sorted(hook_registrations(after)
                                       - hook_registrations(before)):
        where = f" (matcher: {matcher})" if matcher else ""
        lines.append(f"registered {name} on {event}{where}")
    for (event, matcher, name), have, want in short_timeouts(after, before):
        where = f" (matcher: {matcher})" if matcher else ""
        lines.append(f"raised the timeout of {name} on {event}{where} from {have:g} to {want:g} s")
    return lines


def stale_registrations(live: dict, hooks_dir: Path) -> list[tuple[str, str, str]]:
    """Live registrations whose `*.py` hook is not in `hooks_dir` (engine audit F-8.2).

    The merge never removes a live group, so a hook deleted from the engine
    stays registered until someone edits the live file by hand, and the harness
    runs a missing script on every matching event.
    """
    out = set()
    for event, matcher, name in hook_registrations(live):
        script = name.split(" ", 1)[0]
        if script.endswith(".py") and not (hooks_dir / script).is_file():
            out.add((event, matcher, name))
    return sorted(out)


#: The env keys whose values are thresholds the template sets (engine audit
#: F-10.2): a live value that differs is reported, never changed.
_THRESHOLD_ENV = re.compile(r"THRESHOLD|WINDOW|WAIT|STEP|DEPTH")


def env_differences(template: dict, live: dict) -> list[tuple[str, object, object]]:
    """(key, live, template) for every threshold the two files disagree on.

    A key only the live file has counts too: it is a setting no template knows,
    which is how a dead one stays behind.
    """
    t_env = template.get("env") if isinstance(template.get("env"), dict) else {}
    l_env = live.get("env") if isinstance(live.get("env"), dict) else {}
    keys = {k for k in set(t_env) | set(l_env) if _THRESHOLD_ENV.search(k)}
    return sorted((k, l_env.get(k), t_env.get(k)) for k in keys
                  if l_env.get(k) != t_env.get(k))


def _check(template_path: Path, target_path: Path) -> int:
    """Name every hook registration the live file lacks. 0 armed, 1 short."""
    template = _load(template_path, "template")
    expected = hook_registrations(template)

    if not expected:
        # A template that registers nothing would make every clone pass, which
        # is a guard green over an empty corpus. Refuse instead.
        print(f"{template_path} registers no hooks at all. Refusing to certify "
              f"any clone against it.", file=sys.stderr)
        return 1

    if not target_path.exists():
        print(f"NOT ARMED: {target_path} does not exist, so 0 of "
              f"{len(expected)} session hook registrations are present.")
        print(f"  Fix: bash scripts/setup-platform.sh")
        return 1

    live = _load(target_path, "live settings file")
    missing = sorted(expected - hook_registrations(live))
    short = short_timeouts(template, live)
    stale = stale_registrations(live, template_path.parent / "hooks")

    # Reported, never failed on: the live file disposes, and a threshold the
    # operator set on purpose is his. The line makes a difference visible.
    for key, have, want in env_differences(template, live):
        if want is None:
            print(f"env: {key}={have} is set live and named by no template")
        else:
            print(f"env: {key}={have} live, {want} in {template_path.name}")

    if stale:
        print(f"STALE: {target_path.name} registers {len(stale)} hook(s) whose "
              f"script is not in {template_path.parent / 'hooks'}; remove them "
              f"from the live file by hand:")
        for event, matcher, script in stale:
            where = f"  (matcher: {matcher})" if matcher else ""
            print(f"  {event:18s} {script}{where}")
        if not missing and not short:
            return 1

    if short:
        print(f"NOT ARMED: {target_path.name} gives {len(short)} hook registration(s) "
              f"a shorter timeout than {template_path.name}, so the harness can cut "
              f"them before they finish:")
        for (event, matcher, script), have, want in short:
            where = f"  (matcher: {matcher})" if matcher else ""
            print(f"  {event:18s} {script}  {have:g} s, template {want:g} s{where}")
        print(f"  Fix: bash scripts/setup-platform.sh")
        if not missing:
            return 1

    if missing:
        print(f"NOT ARMED: {target_path.name} is missing "
              f"{len(missing)} of {len(expected)} hook registration(s) that "
              f"{template_path.name} defines:")
        for event, matcher, script in missing:
            where = f"  (matcher: {matcher})" if matcher else ""
            print(f"  {event:18s} {script}{where}")
        print(f"  Fix: bash scripts/setup-platform.sh")
        return 1

    # Says only what a comparison of two files establishes, never that a hook
    # ran. Counted per registration (event, matcher, hook), not per hook.
    print(f"armed: {target_path.name} registers all {len(expected)} "
          f"registration(s) that {template_path.name} defines")
    return 0


def _self_test() -> int:
    """Timeout scenarios on invented settings. Pure: no file is read or written."""
    def settings(timeout):
        entry = {"type": "command", "command": "python3 an-invented-hook.py"}
        if timeout is not None:
            entry["timeout"] = timeout
        return {"hooks": {"UserPromptSubmit": [{"hooks": [entry]}]}, "outputStyle": "invented"}

    def timeout_in(s):
        return s["hooks"]["UserPromptSubmit"][0]["hooks"][0].get("timeout")

    failed = 0
    cases = [
        ("a lower live timeout is raised to the template's", settings(10), settings(12), 12, 1),
        ("a higher live timeout is kept", settings(20), settings(12), 20, 0),
        ("an equal live timeout is kept", settings(12), settings(12), 12, 0),
        ("a live entry with no timeout is left alone", settings(None), settings(12), None, 0),
    ]
    for name, live, template, want, short_before in cases:
        before = json.dumps(live, sort_keys=True)
        merged = merge_settings(template, live)
        problems = []
        if timeout_in(merged) != want:
            problems.append(f"merged timeout {timeout_in(merged)}, want {want}")
        if json.dumps(live, sort_keys=True) != before:
            problems.append("the live mapping passed in was modified")
        if len(short_timeouts(template, live)) != short_before:
            problems.append(f"check before merge saw {len(short_timeouts(template, live))} short, want {short_before}")
        if short_timeouts(template, merged):
            problems.append("check after merge still reports a short timeout")
        if merged.get("outputStyle") != "invented":
            problems.append("a local key was lost")
        if want == 12 and short_before and not any("raised the timeout" in line for line in _describe(live, merged)):
            problems.append("the report does not say the timeout was raised")
        print(("FAIL " if problems else "PASS ") + name + (": " + "; ".join(problems) if problems else ""))
        failed += bool(problems)
    print(f"{len(cases) - failed}/{len(cases)} passed")
    return 1 if failed else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return _self_test()
    parser = argparse.ArgumentParser(
        description="Merge a settings template into the live settings file.")
    parser.add_argument("template", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--force", action="store_true",
                        help="REPLACE the live file with the template, "
                             "discarding every local key. Backed up first.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; write nothing")
    parser.add_argument("--check", action="store_true",
                        help="report whether the live file registers every hook "
                             "the template does; write nothing; exit 1 if not")
    parser.add_argument("--backup-dir", type=Path,
                        help="where the pre-merge backup goes (setup-platform.sh "
                             "passes the data repository's runtime:settings-backup/state); "
                             "default: beside the live file")
    args = parser.parse_args()

    if not args.template.is_file():
        print(f"template not found: {args.template}", file=sys.stderr)
        return 1

    if args.check:
        return _check(args.template, args.target)

    if not args.target.exists():
        # First install. No merge to do and nothing to lose, so this path needs
        # no JSON parsing at all and works on a clone that has no venv yet.
        if args.dry_run:
            print(f"would create {args.target} from {args.template.name}")
            return 0
        args.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.template, args.target)
        print(f"created {args.target.name} from {args.template.name}")
        return 0

    template = _load(args.template, "template")
    live = _load(args.target, "live settings file")

    result = template if args.force else merge_settings(template, live)
    changes = _describe(live, result)

    if args.force:
        lost = sorted(set(live) - set(template))
        if lost:
            print(f"--force DISCARDS local key(s): {', '.join(lost)}")

    if result == live:
        print(f"{args.target.name} already carries everything in "
              f"{args.template.name}; nothing to do")
        return 0

    if args.dry_run:
        print(f"would update {args.target.name}:")
        for line in changes or ["values differ"]:
            print(f"  {line}")
        return 0

    # Back up BEFORE writing: into --backup-dir when given, which
    # setup-platform.sh resolves to the data repository (engine audit F-10.16);
    # beside the file otherwise, where `.gitignore` covers
    # `.claude/settings.local.json.bak-*`. `.astimezone()` rather than
    # `get_default_tz()`, to stay standard-library on a fresh clone.
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup_dir = args.backup_dir if args.backup_dir else args.target.parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{args.target.name}.bak-{stamp}"
    shutil.copy2(args.target, backup)

    # Atomic: a half-written settings file is a workspace that will not start.
    tmp = args.target.with_name(f"{args.target.name}.tmp")
    tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    tmp.replace(args.target)

    print(f"updated {args.target.name} (backup: {backup})")
    for line in changes or ["values differ"]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
