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
