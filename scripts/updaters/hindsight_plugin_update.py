#!/usr/bin/env python3
"""Apply script for the `hindsight-coding-agents` update-manager component, the
coding-agents plugin in ~/.hindsight/coding-agents behind the memory banner
(HOS-359). No ssh; versioned on its own, so no order rule.

Steps: `npm pack` the target into a new scratch directory, compare the
tarball's sha512 with the npm registry's `dist.integrity`, unpack. A changed
`hooks/hooks.json` stops here with exit 1 and a notification: a new hook set
needs a human look. Otherwise: rename the installed directory to its backup
name `coding-agents.bak-<YYYYMMDD-HHMMSS>-<old version>`, copy the new package
into a fresh ~/.hindsight/coding-agents, copy `.install-origin.json` back from
the backup, back up the skill file
~/.claude/skills/hindsight-coding-agent/SKILL.md beside itself and copy the
package's `skill/SKILL.md` over it, and run `node --check` on every script
`hooks/hooks.json` names. ~/.hindsight/coding-agent.json is never touched. A
failure moves the new directory aside as `coding-agents.failed-<timestamp>`,
renames the backup back, restores the skill file, exits 1 and notifies.

Exit codes: 0 applied, already current, or not now; 1 failed or refused.

Usage:
  .venv/bin/python scripts/updaters/hindsight_plugin_update.py
  .venv/bin/python scripts/updaters/hindsight_plugin_update.py --dry-run --current 0.8.7 --latest 0.8.8
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.updaters import hindsight_common as hc  # noqa: E402
from scripts.utils.data_path import DataPathError, data_write_dir  # noqa: E402

NAME = "hindsight-coding-agents"
PLUGIN = Path.home() / ".hindsight" / "coding-agents"
SKILL = Path.home() / ".claude" / "skills" / "hindsight-coding-agent" / "SKILL.md"
ORIGIN = ".install-origin.json"
# The hook commands name their scripts relative to a plugin-root variable. The
# brief names `${CLAUDE_PLUGIN_ROOT}`; the installed 0.8.7 `hooks/hooks.json`
# uses `${PLUGIN_ROOT}` (read 2026-11-13), so both are replaced.
ROOT_VARS = ("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
SCRIPT_RE = re.compile(r"[\w${}./-]+\.(?:js|mjs|cjs)")
T_PACK = 300
T_CHECK = 60
# The worst case from the rename of the installed plugin to the exit: the
# local renames and copies 60, `node --check` 60 per hook script (3 in 0.8.7,
# so 180), the restore 60 = 300 for 0.8.7, plus the notification. The registry
# gives this script the manager's default timeout of 900 s; `Budget.require`
# checks the sum, with the real script count and elapsed time, right before the
# rename and answers "not now" when it does not fit. Before that the installed
# plugin is untouched.
T_LOCAL = 60


def _tool_dir(name: str) -> Path | None:
    """The directory holding `name`: PATH first, else the newest nvm Node.js.
    The update timer runs with a minimal PATH that carries no nvm."""
    found = shutil.which(name)
    if found:
        return Path(found).parent
    nvm = sorted((Path.home() / ".nvm" / "versions" / "node").glob("v*/bin"),
                 key=lambda p: hc._version_tuple(hc._normalise_version(p.parent.name)))
    for bindir in reversed(nvm):
        if (bindir / name).is_file():
            return bindir
    return None


def _hook_scripts(hooks: dict, root: Path) -> list[Path]:
    """Every script path the `command` strings under `hooks` name, resolved."""
    commands: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "command" and isinstance(value, str):
                    commands.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(hooks.get("hooks") if isinstance(hooks, dict) else None)
    paths: list[Path] = []
    for cmd in commands:
        for raw in SCRIPT_RE.findall(cmd):
            for var in ROOT_VARS:
                raw = raw.replace(var, str(root))
            paths.append(Path(raw))
    return paths


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _verify(run: hc.Runner, latest: str, env: dict | None, node: str) -> str:
    """"" when the installed plugin is healthy, else why not."""
    try:
        version = _read_json(PLUGIN / "package.json").get("version")
        hooks = _read_json(PLUGIN / "hooks" / "hooks.json")
    except (OSError, ValueError, AttributeError) as exc:
        return f"installed package unreadable ({exc})"
    if hc._normalise_version(str(version)) != latest:
        return f"installed package.json says {version}, not {latest}"
    scripts = _hook_scripts(hooks, PLUGIN)
    if not scripts:
        return "hooks/hooks.json names no script to check"
    for script in scripts:
        if "$" in str(script):
            return f"unresolved variable in hook script path {script}"
        res = run.local([node, "--check", str(script)], T_CHECK, env=env)
        if not res.ok:
            return f"node --check {script} failed ({res.why()})"
    return ""


def _dry(run: hc.Runner, current: str, latest: str, package: str, ts: str) -> None:
    backup = PLUGIN.with_name(hc.backup_name(PLUGIN.name, ts, current))
    skill_backup = SKILL.with_name(hc.backup_name(SKILL.name, ts, current))
    stage = "<new scratch directory>"
    print(f"(cd {stage}) npm pack {package}@{latest}")
    run.note(f"sha512 of the tarball == {hc.npm_integrity(package, latest, True)}")
    print(f"tar xzf <tarball> -C {stage}/unpacked  (tarfile, filter=data)")
    run.note(f"compare {stage}/unpacked/package/hooks/hooks.json with {PLUGIN}/hooks/hooks.json;"
             " a difference stops here, nothing changed, notify")
    print(f"# budget before the rename: renames and copies {T_LOCAL} + node --check "
          f"{T_CHECK} per hook script + restore {T_LOCAL} + notification "
          f"{hc.NOTIFY_PER_SINK_S} per own sink; checked against apply.timeout_s with "
          f"a {hc.MARGIN_S} s margin")
    print(f"mv {PLUGIN} {backup}")
    print(f"cp -a {stage}/unpacked/package {PLUGIN}")
    print(f"cp -p {backup}/{ORIGIN} {PLUGIN}/{ORIGIN}")
    print(f"cp -p {SKILL} {skill_backup}")
    print(f"cp {stage}/unpacked/package/skill/SKILL.md {SKILL}")
    print(f"node --check <each script {PLUGIN}/hooks/hooks.json names>")


def run_update(args) -> None:
    hc.check_dry_args(args)
    run = hc.Runner(dry=args.dry_run)
    if not args.dry_run:
        _lock = hc.take_lock()  # noqa: F841 - held until the process exits
    comp = hc.component(NAME)
    budget = hc.Budget.of(comp, args.dry_run)
    current, latest = hc.read_versions(comp, args)
    hc.compare(NAME, current, latest)
    package = comp.latest.get("package", "")
    ts = hc.stamp()
    vers = f"{current} -> {latest}"
    if run.dry:
        _dry(run, current, latest, package, ts)
        raise hc.Outcome(0, f"{NAME}: applied {vers}")

    backup = PLUGIN.with_name(hc.backup_name(PLUGIN.name, ts, current))
    skill_backup = SKILL.with_name(hc.backup_name(SKILL.name, ts, current))
    for taken in (backup, skill_backup):
        if taken.exists():
            raise hc.Outcome(1, f"{taken} already exists; nothing changed")
    if not PLUGIN.is_dir() or not SKILL.is_file():
        raise hc.Outcome(1, f"{PLUGIN} or {SKILL} is missing; nothing changed")
    npm_dir, node_dir = _tool_dir("npm"), _tool_dir("node")
    if npm_dir is None or node_dir is None:
        raise hc.Outcome(1, "npm or node not found on PATH or under nvm; nothing changed")
    env = dict(os.environ, PATH=f"{node_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    node = str(node_dir / "node")
    integrity = hc.npm_integrity(package, latest, False)
    try:
        stage_root = data_write_dir("runtime:scratch/tmp")
    except DataPathError as exc:
        raise hc.Outcome(1, f"no scratch directory ({exc}); nothing changed") from exc

    with tempfile.TemporaryDirectory(prefix="hindsight-plugin.", dir=stage_root) as td:
        stage = Path(td)
        res = run.local([str(npm_dir / "npm"), "pack", f"{package}@{latest}"], T_PACK,
                        env=env, cwd=stage)
        tarballs = list(stage.glob("*.tgz"))
        if not res.ok or len(tarballs) != 1:
            raise hc.Outcome(1, f"npm pack failed ({res.why()}); nothing changed")
        if hc.file_sha512_sri(tarballs[0]) != integrity:
            raise hc.Outcome(1, "tarball integrity does not match the npm registry; "
                                "nothing changed",
                             f"Hindsight plugin update {vers}: the npm tarball did not match "
                             "its registry integrity; nothing installed")
        unpacked = stage / "unpacked"
        try:
            with tarfile.open(tarballs[0]) as tf:
                tf.extractall(unpacked, filter="data")
            new_pkg = unpacked / "package"
            new_hooks = _read_json(new_pkg / "hooks" / "hooks.json")
            old_hooks = _read_json(PLUGIN / "hooks" / "hooks.json")
        except (OSError, ValueError, tarfile.TarError) as exc:
            raise hc.Outcome(1, f"cannot unpack or read the package ({exc}); "
                                "nothing changed") from exc
        if new_hooks != old_hooks:
            line = (f"{latest} changes hooks/hooks.json; a changed hook set needs a "
                    "human look; nothing changed")
            raise hc.Outcome(1, line, f"Hindsight plugin update {vers}: {line}")
        if not (new_pkg / "skill" / "SKILL.md").is_file():
            raise hc.Outcome(1, f"{latest} carries no skill/SKILL.md; nothing changed")

        n_scripts = len(_hook_scripts(new_hooks, PLUGIN))
        budget.require(T_LOCAL + T_CHECK * n_scripts + T_LOCAL + hc.notify_worst_s(),
                       "the plugin swap",
                       f"renames and copies {T_LOCAL} + node --check {T_CHECK} x {n_scripts} "
                       f"+ restore {T_LOCAL} + notification {hc.notify_worst_s()}")
        why, moved, skill_kept = "", False, False
        try:
            PLUGIN.rename(backup)
            moved = True
            shutil.copytree(new_pkg, PLUGIN, symlinks=True)
            if (backup / ORIGIN).exists():
                shutil.copy2(backup / ORIGIN, PLUGIN / ORIGIN)
            shutil.copy2(SKILL, skill_backup)
            skill_kept = True
            shutil.copyfile(new_pkg / "skill" / "SKILL.md", SKILL)
        except OSError as exc:
            why = f"install failed ({type(exc).__name__}: {exc})"
        why = why or _verify(run, latest, env, node)
    if not why:
        raise hc.Outcome(0, f"{NAME}: applied {vers}; old plugin kept as {backup}")

    # Only undo what was done: before the rename, PLUGIN is still the old one.
    restored = []
    try:
        if moved:
            if PLUGIN.exists():
                PLUGIN.rename(PLUGIN.with_name(f"{PLUGIN.name}.failed-{ts}"))
            backup.rename(PLUGIN)
            restored.append("plugin")
        if skill_kept:
            shutil.copy2(skill_backup, SKILL)
            restored.append("skill")
    except OSError as exc:
        why += f"; RESTORE FAILED ({type(exc).__name__}: {exc})"
    line = f"{why}; restored: {', '.join(restored) or 'nothing'}; {current} back in place" \
        if "RESTORE FAILED" not in why else f"{why}; plugin state unknown"
    raise hc.Outcome(1, line, f"Hindsight plugin update {vers}: {line}")


def main() -> int:
    args = hc.base_parser(__doc__.splitlines()[0]).parse_args()
    return hc.finish(lambda: run_update(args), NAME, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
