#!/usr/bin/env python3
"""Every home tool by absolute path, and the check that finds one named bare (HOS-312, F5).

A hook command or an MCP server command the install registers with Claude Code
must name each tool the install put under the home folder by its absolute
path: a Claude Code session started before the install (whose PATH has no
~/.local/bin or Node.js directory) then sees the same tools as one started
after. System programs under /usr or /bin (python3, sh, bash, git) may stay
bare.

    home_tools(home, engine)      name -> absolute path, only those on disk
    bare_home_tools(home, tools)  one line per registered command whose first
                                  word is a bare name in `tools`

Read by the hindsight-claude step (scripts/utils/install_hindsight.py), which
rewrites a bare hook command and a bare MCP server command (the Hindsight
plugin registers its server as `node`), and by scripts/doctor.py. heading-install.py does
not import this module, so it may import engine modules; it does so inside
`home_tools`, because install_hindsight imports this module.

Usage:
    from scripts.utils.install_commands import bare_home_tools, home_tools
    findings = bare_home_tools(Path.home(), home_tools(Path.home(), engine_root))
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

ENGINE_PYTHON = ".venv/bin/python"


def node_bin_dir(home: Path) -> Path:
    # Restates node_home() / "current" / "bin" of scripts/heading-install.py,
    # which is a script and is not imported.
    return home / ".local" / "share" / "heading-os" / "node" / "current" / "bin"


def claude_settings_path(home: Path) -> Path:
    """The Claude Code user settings: <CLAUDE_CONFIG_DIR, else home/.claude>/settings.json."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else home / ".claude") / "settings.json"


def home_tools(home: Path, engine: Path | None = None) -> dict[str, str]:
    """Each tool the install puts under `home`, name -> path, unresolved; only those that exist."""
    from scripts.utils.install_herdr_cliproxy import Host, cliproxy_bin, herdr_bin
    from scripts.utils.install_hindsight import cli_path
    from scripts.utils.install_lific import binary_path

    paths: dict[str, Path] = {
        "node": node_bin_dir(home) / "node",
        "npx": node_bin_dir(home) / "npx",
        "hindsight": cli_path(home),
        "herdr": herdr_bin(Host(home=home)),
        "lific": binary_path(home),
        "cli-proxy-api": cliproxy_bin(home),
    }
    for name in ("uv", "claude"):
        found = shutil.which(name)
        if found and found.startswith(str(home) + "/"):
            paths[name] = Path(found)
    if engine is not None:
        # Named by the relative form the engine's own commands use; any form but the absolute is bare.
        paths[ENGINE_PYTHON] = engine / ".venv" / "bin" / "python"
    return {name: str(path) for name, path in paths.items() if path.exists()}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _first_word(command: str) -> str | None:
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    return words[0] if words else None


def hook_commands(settings) -> list[tuple[str, dict]]:
    """(event, hook entry) for every `hooks.<event>[].hooks[]` entry holding a string command."""
    out = []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for event, groups in (hooks or {}).items() if isinstance(hooks, dict) else ():
        for group in groups if isinstance(groups, list) else ():
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else ():
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    out.append((event, hook))
    return out


def bare_home_tools(home: Path, tools: dict[str, str]) -> list[str]:
    """`<file>: <event or server>: <first word>` for each registered command naming a home tool bare."""
    from scripts.utils.install_lific import claude_config_path

    found = []
    settings_file = claude_settings_path(home)
    for event, hook in hook_commands(_read_json(settings_file)):
        word = _first_word(hook["command"])
        if word in tools and not word.startswith("/"):
            found.append(f"{settings_file}: {event}: {word}")
    config_file = claude_config_path(home)
    config = _read_json(config_file)
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    for server, entry in (servers or {}).items() if isinstance(servers, dict) else ():
        word = entry.get("command") if isinstance(entry, dict) else None
        if isinstance(word, str) and word in tools and not word.startswith("/"):
            found.append(f"{config_file}: {server}: {word}")
    return found


# ============================================================
# Toolchain paths pinned to a missing or non-default version (HOS-395)
# ============================================================
#
# A unit, a hook or an MCP server that names a tool by absolute path keeps
# naming it after an upgrade: when the nvm default Node moved to a new version,
# hooks and a rendered unit still called the old one, and no check said so.
# `toolchain_pins` reads those paths from disk (never through systemctl) and
# reports each one that is missing, or inside an nvm Node version that is not
# the current default. Read-only.

UNIT_SUFFIXES = (".service", ".timer", ".path", ".socket")
_EXEC_DIRECTIVES = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop")
_EXEC_PREFIX = "-@!+:"
_ALIAS_HOPS = 5


def _unit_files(units_dir: Path) -> list[Path]:
    """Unit files in `units_dir` (symlinks to files count), then each `<unit>.d/*.conf` beside them."""
    if not units_dir.is_dir():
        return []
    files = []
    for entry in sorted(units_dir.iterdir()):
        if entry.suffix in UNIT_SUFFIXES and entry.is_file():
            files.append(entry)
        elif entry.name.endswith(".d") and Path(entry.name[:-2]).suffix in UNIT_SUFFIXES and entry.is_dir():
            files.extend(sorted(c for c in entry.glob("*.conf") if c.is_file()))
    return files


def _unit_directives(text: str) -> list[tuple[str, str]]:
    """(directive, value) for each assignment line, continuation lines joined, comments skipped."""
    out, pending = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        # A comment line is ignored even inside a continued directive, as systemd does.
        if line.startswith(("#", ";")) or (not pending and not line):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line, pending = pending + line, ""
        key, sep, value = line.partition("=")
        if sep:
            out.append((key.strip(), value.strip()))
    return out


def _unit_paths(text: str) -> list[tuple[str, str, str]]:
    """(directive, what, absolute path) named by the Exec* and Environment=PATH lines of a unit."""
    found = []
    for key, value in _unit_directives(text):
        if key in _EXEC_DIRECTIVES:
            word = _first_word(value.lstrip(_EXEC_PREFIX))
            if word and word.startswith("/"):
                found.append((key, "executable", word))
        elif key == "Environment":
            try:
                assignments = shlex.split(value)
            except ValueError as exc:
                # The value is never printed: an Environment= line can carry a secret.
                print(f"install_commands: an Environment= line was not parsed ({exc})", file=sys.stderr)
                continue
            for item in assignments:
                name, sep, val = item.partition("=")
                if sep and name == "PATH":
                    found.extend((key, "path-dir", d) for d in val.split(":") if d.startswith("/"))
    return found


def _mcp_blocks(config) -> list[dict]:
    """The `mcpServers` dicts of a Claude Code config: the top-level one, then each project's."""
    blocks = [config.get("mcpServers")] if isinstance(config, dict) else []
    projects = config.get("projects") if isinstance(config, dict) else None
    for project in projects.values() if isinstance(projects, dict) else ():
        if isinstance(project, dict):
            blocks.append(project.get("mcpServers"))
    return [b for b in blocks if isinstance(b, dict)]


def _mcp_commands(config) -> list[tuple[str, str]]:
    """(server, command) under the top-level mcpServers and each project's; args and env are never read."""
    out = []
    for servers in _mcp_blocks(config):
        for server, entry in servers.items():
            command = entry.get("command") if isinstance(entry, dict) else None
            if isinstance(command, str):
                out.append((server, command))
    return out


def _version_tuple(name: str) -> tuple[int, int, int] | None:
    parts = name[1:].split(".") if name.startswith("v") else []
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def _installed_node_versions(nvm_dir: Path) -> list[tuple[tuple[int, int, int], str]]:
    """(version tuple, directory name) of every installed nvm Node, in numeric order."""
    root = nvm_dir / "versions" / "node"
    if not root.is_dir():
        return []
    found = [(_version_tuple(d.name), d.name) for d in root.iterdir() if d.is_dir()]
    return sorted((t, n) for t, n in found if t is not None)


def nvm_default_version(nvm_dir: Path) -> str | None:
    """The version directory name (`vX.Y.Z`) nvm's default alias resolves to, or None."""
    alias_dir = nvm_dir / "alias"
    try:
        value = (alias_dir / "default").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    for _ in range(_ALIAS_HOPS):
        target = alias_dir / value
        if not value or ".." in Path(value).parts or Path(value).is_absolute() or not target.is_file():
            break
        try:
            value = target.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
    else:
        if value and ".." not in Path(value).parts and (alias_dir / value).is_file():
            return None  # still an alias after the last hop
    installed = _installed_node_versions(nvm_dir)
    if value in ("node", "stable"):
        return installed[-1][1] if installed else None
    bare = value[1:] if value.startswith("v") else value
    parts = bare.split(".")
    if not bare or len(parts) > 3 or not all(p.isdigit() for p in parts):
        return None
    if len(parts) == 3:
        return "v" + ".".join(str(int(p)) for p in parts)
    want = tuple(int(p) for p in parts)
    matches = [n for t, n in installed if t[:len(want)] == want]
    return matches[-1] if matches else None


def _nvm_version_of(path: str, nvm_dir: Path) -> str | None:
    """The `<V>` of a path inside `<nvm_dir>/versions/node/<V>/`, compared as written."""
    try:
        rel = Path(path).relative_to(nvm_dir / "versions" / "node")
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def toolchain_pins(home: Path, units_dir: Path, settings_files: list[Path],
                   mcp_files: list[Path], nvm_dir: Path) -> list[dict]:
    """Each absolute toolchain path that is MISSING, or STALE (inside an nvm Node version that is not the default).

    A finding: {"kind", "what", "file", "where", "path"}; one per (file, where, path).
    UNRESOLVED nvm-default when nvm is in use and its default resolves to nothing;
    then no STALE finding is made. Only the files the arguments name are read;
    `home` is accepted so every input is a parameter.
    """
    candidates: list[tuple[str, str, str, str]] = []  # (file, where, what, path)
    for unit in _unit_files(units_dir):
        text = unit.read_text(encoding="utf-8", errors="replace")
        candidates.extend((str(unit), key, what, path) for key, what, path in _unit_paths(text))
    for settings_file in settings_files:
        for event, hook in hook_commands(_read_json(settings_file)):
            word = _first_word(hook["command"])
            if word and word.startswith("/"):
                candidates.append((str(settings_file), event, "executable", word))
    for config_file in mcp_files:
        for server, command in _mcp_commands(_read_json(config_file)):
            if command.startswith("/"):
                candidates.append((str(config_file), server, "executable", command))

    findings: list[dict] = []
    default = None
    if nvm_dir.is_dir():
        default = nvm_default_version(nvm_dir)
        if default is None:
            findings.append({"kind": "UNRESOLVED", "what": "nvm-default",
                             "file": str(nvm_dir / "alias" / "default"), "where": "", "path": ""})
    seen: set[tuple[str, str, str]] = set()
    for file, where, what, path in candidates:
        if (file, where, path) in seen:
            continue
        seen.add((file, where, path))
        if not os.path.exists(path):
            kind = "MISSING"
        elif default is not None and _nvm_version_of(path, nvm_dir) not in (None, default):
            kind = "STALE"
        else:
            continue
        findings.append({"kind": kind, "what": what, "file": file, "where": where, "path": path})
    return findings


def toolchain_pin_line(finding: dict, home: Path) -> str:
    """`<kind> <what> <file>: <where>: <path>`, the home folder shown as `~`."""
    line = f"{finding['kind']} {finding['what']} {finding['file']}: {finding['where']}: {finding['path']}"
    return line.replace(str(home), "~").rstrip()


def default_toolchain_pin_args(engine: Path) -> dict:
    """The keyword arguments of `toolchain_pins` for this machine; doctor and ops-radar both use it."""
    from scripts.utils.install_lific import claude_config_path

    home = Path.home()
    mcp_files = [claude_config_path(home)]
    if (engine / ".mcp.json").exists():
        mcp_files.append(engine / ".mcp.json")
    nvm = os.environ.get("NVM_DIR")
    return {
        "home": home,
        "units_dir": home / ".config" / "systemd" / "user",
        "settings_files": [claude_settings_path(home), engine / ".claude" / "settings.json",
                           engine / ".claude" / "settings.local.json"],
        "mcp_files": mcp_files,
        "nvm_dir": Path(nvm) if nvm else home / ".nvm",
    }
