#!/usr/bin/env python3
"""lot-map.py - a map of where a Claude Code session's work went, from its transcript.

A terminal rebuild of the idea behind lot-map (github.com/alexeyw/lot-map, a
Claude Code mod), made from its README alone, read 2026-11-22: no line of its
code was read, downloaded or copied. lot-map is a live pane in which every
tool call, model step, hook and skill sends a pulse from the centre to one of
ten STATIONS (You, Model, Memory, Files, Shell, Network, Browser, Agents,
Skills, Hooks). This script reads the same events from the session transcript
Claude Code already writes, so it needs no mod, no hook and no install, and
it works on any past session as well as on the running one (`--follow`).

What it prints, from a transcript:

- the board: per station, the calls, the failures and refusals, the time
  spent, and the tokens the context grew by with those calls' answers;
- the route: the last stations the main loop went through, in order;
- the rows: the last calls, one line each, `●` running, `·` done, `✕` failed
  or refused, `◆` a change of model;
- with `--map`, the board drawn in braille dots, spokes thickening with use.

What it keeps out, by the README's own rule: a label holds a NAME, never
content. A file is its last two path parts, a web address its host, a shell
command the description Claude wrote for it or the command's name, a search
the tool's name, a memory call the tool's name, a hook refusal the tool it
stopped. Command lines, queries, file text and tool results are read to
classify a call and never printed.

It reads files and nothing else: no network, no model, no subprocess, no
write. `--follow` is a foreground loop the operator stops with Ctrl-C.

Usage:
    python scripts/lot-map.py                       # newest session of this checkout
    python scripts/lot-map.py --project <checkout>  # newest session of another checkout
    python scripts/lot-map.py --session <id> --project <checkout>
    python scripts/lot-map.py --transcript <file.jsonl>
    python scripts/lot-map.py --map --rows 30
    python scripts/lot-map.py --follow              # redraw every second until Ctrl-C
    python scripts/lot-map.py --json                # the board as JSON

Persistence: none. Reads the transcript store, writes nothing, listens on no port.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.checkpoint_paths import transcript_dir  # noqa: E402
from scripts.utils.data_path import DataPathError, data_path  # noqa: E402
from scripts.utils.session_scope import _subagent_transcripts  # noqa: E402

# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------

STATIONS = ("you", "model", "memory", "files", "shell", "network",
            "browser", "agents", "skills", "hooks")

NAMES = {"you": "You", "model": "Model", "memory": "Memory", "files": "Files",
         "shell": "Shell", "network": "Network", "browser": "Browser",
         "agents": "Agents", "skills": "Skills", "hooks": "Hooks"}

WORDS = {"message": "your message", "answer": "answer", "compaction": "context compaction",
         "web_search": "web search", "loading_tools": "loading tools",
         "subagent": "subagent", "refused": "refused by a hook",
         "added": "added context", "took": "took a moment",
         "now": "now", "idle": "nothing runs", "route": "route", "calls": "calls",
         "errors": "errors", "time": "time", "context": "context"}

# Tools by station. Anything not named falls to the rules in `tool_station`.
YOU_TOOLS = {"AskUserQuestion", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList",
             "TaskGet", "ExitPlanMode", "EnterPlanMode"}
FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead",
              "Grep", "Glob", "LSP"}
SHELL_TOOLS = {"Bash", "PowerShell"}
AGENT_TOOLS = {"Agent", "Task", "Workflow", "ScheduleWakeup", "CronCreate", "CronDelete",
               "CronList", "RemoteTrigger", "Monitor", "TaskOutput", "TaskStop",
               "SendMessage", "ListAgents", "BashOutput", "KillShell"}
SKILL_TOOLS = {"Skill", "ToolSearch", "SlashCommand", "ListMcpResourcesTool",
               "ReadMcpResourceTool"}

# This workspace's own defaults for the README's `memoryServers`,
# `memoryCommands` and `serverStations` options. Each is overridable on the
# command line; none is a path.
MEMORY_SERVERS = {"hindsight-main", "hindsight", "memory"}
MEMORY_COMMANDS = {"hindsight", "hindsight-remember", "hindsight-snapshot",
                   "hindsight-sync", "memory-index", "recall"}
SERVER_STATIONS = {"codegraph": "files", "agent-watch": "agents", "playwright": "browser"}
COMMAND_STATIONS = {"herdr": "agents", "hc": "agents", "herdr-brief": "agents",
                    "herdr-relay": "agents", "herdr-wait": "agents"}

# Shell command names, per the README's "What lights up".
FILE_COMMANDS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg",
                 "ag", "find", "ls", "mv", "cp", "rm", "mkdir", "rmdir", "touch", "sed",
                 "tee", "wc", "sort", "uniq", "cut", "diff", "stat", "du", "ln", "chmod",
                 "chown", "realpath", "readlink", "file", "tree", "zcat", "gzip", "gunzip"}
NETWORK_COMMANDS = {"curl", "wget", "gh", "ssh", "scp", "sftp", "rsync", "nc", "ncat",
                    "telnet", "ftp", "ping", "dig", "nslookup", "whois", "http", "https"}
NETWORK_SUBCOMMANDS = {"git": {"push", "pull", "fetch", "clone", "ls-remote"},
                       "npm": {"install", "i", "ci", "publish", "update"},
                       "pnpm": {"install", "add"}, "yarn": {"install", "add"},
                       "pip": {"install", "download"}, "pip3": {"install", "download"},
                       "uv": {"sync", "add", "pip"}, "docker": {"pull", "push", "login"},
                       "apt": {"install", "update"}, "apt-get": {"install", "update"}}
# Commands that neither do work of their own nor change the line's station.
NEUTRAL = {"cd", "pushd", "popd", "export", "set", "unset", "echo", "printf", "true",
           "false", ":", "test", "[", "[[", "sleep", "wait", "source", ".", "local",
           "read", "shift", "exit", "return"}
KEYWORDS = {"for", "do", "done", "if", "then", "else", "elif", "fi", "while", "until",
            "case", "esac", "in", "!", "{", "}", "(", ")", "time", "function", "select"}
WRAPPERS = {"sudo", "env", "timeout", "nohup", "xargs", "nice", "ionice", "stdbuf",
            "exec", "command", "builtin", "watch", "strace", "chronic", "unbuffer"}
# A wrapper's options that take the next word as their argument.
WRAPPER_ARGS = {"sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"},
                "timeout": {"-s", "-k", "--signal", "--kill-after"},
                "xargs": {"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"},
                "nice": {"-n"}, "ionice": {"-c", "-n", "-p"},
                "env": {"-u", "-C", "-S"}, "watch": {"-n"}, "strace": {"-e", "-o", "-p"}}
SHELLS = {"bash", "sh", "zsh", "dash"}
INTERPRETERS = {"python", "python3", "python3.11", "python3.12", "node", "ruby",
                "perl", "deno", "bun", "tsx"}

LINE_LIMIT = 8000  # the README reads a line up to its first 8,000 characters
MAX_NESTING = 64  # nested $( levels scanned before a span runs to the end of the line
WINDOW_DEFAULT = 1_000_000  # context-bar's FULL_WINDOW, operator decision 2026-11-08

# ---------------------------------------------------------------------------
# Shell command classification
# ---------------------------------------------------------------------------

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")  # `x=1`, and `x+=1` appends
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")  # a shell name, ASCII
_NAME_CHARS = _NAME_START | frozenset("0123456789")


def strip_heredocs(text: str) -> str:
    """The command with every here-document body removed: its text is not read."""
    out, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        tags = [m.group(2) for m in _HEREDOC.finditer(line)]
        i += 1
        for tag in tags:
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1
    return "\n".join(out)


def _closing_paren(text: str, j: int, level: int = 0) -> int:
    """The index of the `)` that closes a `$(` whose body starts at `j`, or len(text).

    Quoted text and escaped characters inside the body are skipped, so the `)`
    in `$(echo ")")` does not close it. A nested `$(` is scanned by its own call,
    with fresh quotes, also inside double quotes, so `$(echo "$(date)")` closes
    where the shell closes it; a `$((` arithmetic span is skipped to its `))`,
    and a bare `(` (a subshell) is matched by its own `)`. Past
    MAX_NESTING levels the span runs to the end of the line instead of
    recursing further, so a crafted line cannot exhaust the stack.
    """
    quote, n, parens = "", len(text), 0
    if level > MAX_NESTING:
        return n
    while j < n:
        ch = text[j]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            j += 1
        elif text.startswith("$((", j):
            depth, j = 2, j + 3
            while j < n and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            continue
        elif text.startswith("$(", j):
            j = _closing_paren(text, j + 2, level + 1)
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            parens += 1
        elif ch == ")":
            if not parens:
                return j
            parens -= 1
        j += 1
    return n


def substitutions(text: str) -> tuple[str, list[str]]:
    """`(text with $(...) and backticks blanked, the inner command lines)`."""
    inner: list[str] = []
    out, i, n = [], 0, len(text)
    single = double = False  # the shell expands nothing in single quotes or after a backslash
    while i < n:
        ch = text[i]
        if single:
            single = ch != "'"
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == "'" and not double:
            single = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            double = not double
            out.append(ch)
            i += 1
            continue
        if text.startswith("$(", i) and not text.startswith("$((", i):
            k = _closing_paren(text, i + 2)
            inner.append(text[i + 2:k])
            out.append(" ")
            i = k + 1
        elif text[i] == "`":
            j = text.find("`", i + 1)
            j = n if j == -1 else j
            inner.append(text[i + 1:j])
            out.append(" ")
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out), inner


def split_commands(text: str) -> list[str]:
    """The simple commands of a line: split on `;`, `&`, `|` and newlines outside quotes.

    A `(` where a command would start opens a subshell, so it and its `)` split too:
    `(cd x; curl y)` is `cd x` and `curl y`.
    """
    parts, buf, quote, i, sub = [], [], "", 0, 0
    blank = True  # buf holds only spaces and tabs, the shell's blanks
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
            buf.append(ch)
        elif ch == "\\" and i + 1 < len(text):
            buf.append(text[i:i + 2])
            blank = False
            i += 2
            continue
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
            blank = False
        elif ch in ";&|\n" or (ch == "(" and blank) or (ch == ")" and sub):
            sub += {"(": 1, ")": -1}.get(ch, 0)
            parts.append("".join(buf))
            buf, blank = [], True
        else:
            buf.append(ch)
            blank = blank and ch in " \t"
        i += 1
    parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _tokens(segment: str) -> list[str]:
    """Shell words: quotes, backslashes and a `#` comment at a word's start.

    The same rules, line for line, as `words` in the mod's stations.ts, so the
    two copies split every line alike; an unclosed quote runs to the end. An
    array assignment is one word: an unquoted `(` right after an unquoted
    `name=` opens it and blanks inside it do not split, up to its unquoted `)`.
    Deciding here, where quotes are still visible, keeps `x='(a' curl y` two
    words apart from the array `x=(a b)`.
    """
    out, cur, has, quote, i, n = [], [], False, "", 0, len(segment)
    # how far the word so far is an unquoted `name=`, one character at a time:
    # 0 empty, 1 a name, 2 a name and `=` or `+=`, 3 anything else, 4 a name and `+`
    shape = 0
    array = False  # inside an unquoted `name=(...)`
    while i < n:
        ch = segment[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < n:
                # in double quotes a backslash escapes only $ ` " \ and newline
                nxt = segment[i + 1]
                cur.append(nxt if nxt in '$`"\\\n' else ch + nxt)
                i += 1
            else:
                cur.append(ch)
        elif ch in "'\"":
            quote, has, shape = ch, True, 3
        elif ch == "\\" and i + 1 < n:
            cur.append(segment[i + 1])
            i, has, shape = i + 1, True, 3
        elif ch == "#" and not has:
            break
        elif ch == "(" and not array and shape == 2:
            cur.append(ch)
            array, shape = True, 3
        elif ch == ")" and array:
            cur.append(ch)
            array = False
        elif ch in " \t\r\n" and array:
            cur.append(" ")
        elif ch in " \t\r\n":
            if has:
                out.append("".join(cur))
            cur, has, shape = [], False, 0
        else:
            cur.append(ch)
            has = True
            if shape == 0:
                shape = 1 if ch in _NAME_START else 3
            elif shape == 1:
                shape = 1 if ch in _NAME_CHARS else (2 if ch == "=" else (4 if ch == "+" else 3))
            elif shape == 4:  # a name and `+`: `x+=(a)` appends to an array
                shape = 2 if ch == "=" else 3
            else:
                shape = 3
        i += 1
    if has:
        out.append("".join(cur))
    return out


def _base(word: str) -> str:
    """A program by its name, also from a full or quoted path; `CURL.EXE` is `curl`."""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _stem(word: str) -> str:
    name = _base(word)
    for ext in (".py", ".sh", ".js", ".mjs", ".ts"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _named(name: str, names: set[str] | dict) -> str | None:
    """The README's match: the command itself, or it followed by `-`, `_` or `.`."""
    for candidate in names:
        if name == candidate or (name.startswith(candidate)
                                 and name[len(candidate)] in "-_."):
            return candidate
    return None


@dataclass
class Opts:
    memory_servers: set[str] = field(default_factory=lambda: set(MEMORY_SERVERS))
    memory_commands: set[str] = field(default_factory=lambda: set(MEMORY_COMMANDS))
    server_stations: dict[str, str] = field(default_factory=lambda: dict(SERVER_STATIONS))
    command_stations: dict[str, str] = field(default_factory=lambda: dict(COMMAND_STATIONS))
    memory_roots: list[Path] = field(default_factory=list)
    hook_reasons: bool = False


def _simple(words: list[str], opts: Opts, depth: int) -> list[tuple[str, str]]:
    """`[(station, name)]` for one simple command, unwrapped."""
    if words and words[0] in ("for", "select", "case"):
        return []  # a loop's header names a variable and a list, not a command
    i = 0
    while i < len(words):
        w = words[i]
        if w in KEYWORDS or _ASSIGN.match(w):
            i += 1  # an array `x=(a b)` is one word already, see `_tokens`
            continue
        b = _base(w)
        if b in ("uv", "uvx") and words[i + 1:i + 2] == ["run"]:
            i += 2  # `uv run x` runs x; a loop, so a long chain needs no recursion
            continue
        if b in WRAPPERS:
            i += 1
            # the wrapper's own options and arguments: flags, durations, assignments
            while i < len(words) and (words[i].startswith("-") or _ASSIGN.match(words[i])
                                      or (b == "timeout" and re.match(r"^[0-9]", words[i]))):
                takes = WRAPPER_ARGS.get(b, set())
                i += 2 if words[i] in takes else 1
            continue
        break
    if i >= len(words):
        return []
    name = _base(words[i])
    if name.startswith("$"):
        name = "sh"  # a command held in a variable: its name is not known here
    rest = words[i + 1:]
    if name in SHELLS or name == "eval":
        if name == "eval":
            return classify_line(" ".join(rest), opts, depth + 1)
        if "-c" in rest:
            k = rest.index("-c")
            if k + 1 < len(rest):
                return classify_line(rest[k + 1], opts, depth + 1)
        script = next((r for r in rest if not r.startswith("-")), None)
        name = _stem(script) if script else name
    elif name in INTERPRETERS or name.startswith("python3"):
        # `python scripts/x.py`: the script is the command; `python -c` stays Shell
        script = next((r for r in rest if not r.startswith("-")), None)
        inline = {"-c", "-e", "-p", "-r", "-E", "--eval", "--print"} & set(rest)
        if script and not inline and "-m" not in rest:
            name = _stem(script)
        elif "-m" in rest and rest.index("-m") + 1 < len(rest):
            name = rest[rest.index("-m") + 1].lower()
    elif name == "npx":
        return [("network" if "-y" in rest or "--yes" in rest else "shell", "npx")]
    if name == "find":
        if "-exec" in rest or "-execdir" in rest:
            k = rest.index("-exec") if "-exec" in rest else rest.index("-execdir")
            inner = [r for r in rest[k + 1:] if r not in (";", "+", "{}")]
            return [("files", "find")] + _simple(inner, opts, depth)
        return [("files", "find")]
    configured = _named(name, opts.command_stations)
    memory = _named(name, opts.memory_commands)
    if memory:
        return [("memory", name)]
    if configured:
        return [(opts.command_stations[configured], name)]
    if name in NEUTRAL:
        return [("neutral", name)]
    if name in NETWORK_COMMANDS:
        return [("network", name)]
    sub = NETWORK_SUBCOMMANDS.get(name)
    if sub and any(r in sub for r in rest[:2]):
        return [("network", name)]
    if name in FILE_COMMANDS:
        return [("files", name)]
    return [("shell", name)]


def classify_line(line: str, opts: Opts, depth: int = 0) -> list[tuple[str, str]]:
    """Every simple command on a shell line, as `[(station, name)]`."""
    if depth > 4:
        return [("shell", "sh")]
    text, inner = substitutions(strip_heredocs(line[:LINE_LIMIT]))
    found: list[tuple[str, str]] = []
    for segment in split_commands(text):
        words = _tokens(segment)
        # redirection targets are not commands
        words = [w for w in words if not re.match(r"^[0-9]*[<>]", w)]
        if words:
            found += _simple(words, opts, depth)
    for sub in inner:
        found += classify_line(sub, opts, depth + 1)
    return found


def shell_station(command: str, opts: Opts) -> tuple[str, str]:
    """One station for a whole line, and the first real command's name.

    Network wins (a line that reaches the network goes there, whatever else it
    runs); then a configured station; then Memory; Files only when every
    command on the line works on files and nothing else; otherwise Shell.
    """
    parts = [p for p in classify_line(command, opts) if p[0] != "neutral"]
    if not parts:
        return "shell", (classify_line(command, opts) or [("shell", "sh")])[0][1]
    stations = {s for s, _ in parts}
    first = parts[0][1]
    for winner in ("network",):
        if winner in stations:
            return winner, next(n for s, n in parts if s == winner)
    for s, n in parts:
        if s not in ("files", "shell", "memory"):
            return s, n
    if "memory" in stations:
        return "memory", next(n for s, n in parts if s == "memory")
    if stations == {"files"}:
        return "files", first
    return "shell", next((n for s, n in parts if s == "shell"), first)


# ---------------------------------------------------------------------------
# Tool classification and labels
# ---------------------------------------------------------------------------

def _two_parts(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return "/".join(parts[-2:]) if parts else path


def _host(url: str) -> str:
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://([^/?#]*)", url or "")
    if not m:
        return "?"
    host = m.group(2).rsplit("@", 1)[-1]
    if not host or "\\" in host or " " in host:
        return m.group(1)
    return host.split(":")[0] if host.count(":") == 1 else host


# C0, DEL, C1 (U+009B is a one-byte CSI) and the bidi overrides: none reaches the terminal.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]+")


def _clean(text: str, limit: int = 60) -> str:
    """Control characters out, one line, cut at `limit`."""
    text = _CONTROL.sub(" ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _in_memory(path: str, opts: Opts) -> bool:
    if not path:
        return False
    try:
        p = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for root in opts.memory_roots:
        if p == root or root in p.parents:
            return True
    # Claude Code's own memory folder: <projects>/<slug>/memory/
    parts = p.parts
    return ("projects" in parts and "memory" in parts
            and parts.index("memory") == parts.index("projects") + 2)


def tool_station(name: str, data: dict, opts: Opts) -> tuple[str, str]:
    """`(station, label)` for one tool_use block. The label holds a name, never content."""
    words = WORDS
    if name.startswith("mcp__"):
        _, server, *tool = name.split("__", 2)
        tool_name = tool[0] if tool else ""
        if server in opts.memory_servers:
            ident = data.get("id") or data.get("document_id") or data.get("memory_id")
            return "memory", f"{server}: {tool_name}" + (f" {_clean(ident, 24)}" if ident else "")
        configured = opts.server_stations.get(server)
        if configured:
            return configured, f"{server}: {tool_name}"
        if "browser" in server or "chrome" in server:
            return "browser", f"{server}: {tool_name}"
        return "network", f"{server}: {tool_name}"
    if name in FILE_TOOLS:
        path = data.get("file_path") or data.get("notebook_path") or ""
        if name in ("Grep", "Glob"):
            where = data.get("path") or ""
            return ("memory" if _in_memory(where, opts) else "files"), name
        label = f"{name} {_two_parts(path)}" if path else name
        return ("memory" if _in_memory(path, opts) else "files"), label
    if name in SHELL_TOOLS:
        station, cmd = shell_station(str(data.get("command") or ""), opts)
        desc = data.get("description")
        label = _clean(desc) if desc else cmd
        if data.get("run_in_background"):
            label += " (bg)"
        return station, label
    if name == "WebSearch":
        return "network", words["web_search"]
    if name == "WebFetch":
        return "network", f"WebFetch {_host(str(data.get('url') or ''))}"
    if name in YOU_TOOLS:
        return "you", name
    if name in AGENT_TOOLS:
        desc = data.get("description") or data.get("subagent_type") or ""
        return "agents", f"{name} {_clean(desc, 40)}".strip()
    if name == "ToolSearch":
        return "skills", words["loading_tools"]
    if name in SKILL_TOOLS:
        skill = data.get("skill") or data.get("command") or ""
        return "skills", f"{name} {_clean(skill, 30)}".strip()
    if name.startswith(("browser", "Browser", "Chrome")):
        return "browser", name
    return "shell", name


# ---------------------------------------------------------------------------
# Reading a transcript into events
# ---------------------------------------------------------------------------

@dataclass
class Call:
    station: str
    label: str
    start: datetime | None
    end: datetime | None = None
    state: str = "running"  # running | done | failed | refused | info
    sub: bool = False        # made by a subagent
    ctx: int | None = None   # tokens the context grew by with this call's answer
    shared: bool = False     # ctx is an even share of one step's growth
    kind: str = "call"       # call | message | step | answer | hook | switch | compaction
    tool: str = ""           # the tool's name, for a refusal's label
    hook_memory: bool = False

    def __post_init__(self) -> None:
        # Every label is cleaned once, here: it may carry model-chosen text (a path,
        # a host, a command name) that must never reach the terminal as a control.
        self.label = _clean(self.label, 200)

    @property
    def seconds(self) -> float | None:
        if self.start and self.end:
            return max(0.0, (self.end - self.start).total_seconds())
        return None


def _ts(record: dict) -> datetime | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _records(path: Path):
    """Each JSON record of a transcript, streamed; an unparseable line is skipped and counted."""
    skipped = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(record, dict):
                yield record
    if skipped:
        print(f"lot-map: {skipped} unreadable line(s) skipped in {path.name}", file=sys.stderr)


def _context(usage: dict) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total or None


def _short_model(model: str) -> str:
    m = re.match(r"^claude-([a-z]+)-(\d+)-(\d+)", model or "")
    return f"{m.group(1)} {m.group(2)}.{m.group(3)}" if m else _clean(model or "?", 40)


_MEMORY_HOOK = re.compile(r"hindsight|memory_facts|<memory", re.IGNORECASE)


@dataclass
class Session:
    path: Path
    calls: list[Call] = field(default_factory=list)
    model: str = ""
    context: int | None = None
    steps: int = 0
    turns: int = 0
    started: datetime | None = None
    last: datetime | None = None
    subagents: int = 0


def read_session(path: Path, opts: Opts) -> Session:
    s = Session(path=path)
    words = WORDS
    by_id: dict[str, Call] = {}
    step_calls: dict[str, list[Call]] = {}
    step_order: list[tuple[str, int | None]] = []  # (request id, context), main loop only
    seen_steps: set[str] = set()
    compact_after: set[int] = set()  # step index after which a compaction happened

    def take(records, sub: bool) -> None:
        for r in records:
            t = r.get("type")
            ts = _ts(r)
            if ts:
                s.started = s.started or ts
                s.last = ts if not s.last or ts > s.last else s.last
            if t == "assistant":
                msg = r.get("message") or {}
                rid = r.get("requestId") or msg.get("id") or r.get("uuid")
                if not sub and not r.get("isSidechain"):
                    model = msg.get("model") or ""
                    if model and model != "<synthetic>":
                        if s.model and model != s.model:
                            s.calls.append(Call("model", f"{_short_model(s.model)} → "
                                                f"{_short_model(model)}", ts, ts, "info",
                                                kind="switch"))
                        s.model = model
                    ctx = _context(msg.get("usage"))
                    if rid not in seen_steps:
                        seen_steps.add(rid)
                        s.steps += 1
                        step_order.append((rid, ctx))
                        s.calls.append(Call("model", _short_model(model), ts, ts, "done",
                                            kind="step"))
                    elif ctx:
                        step_order[-1] = (rid, ctx) if step_order[-1][0] == rid else step_order[-1]
                    if ctx:
                        s.context = ctx
                for block in msg.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    data = block.get("input") if isinstance(block.get("input"), dict) else {}
                    station, label = tool_station(str(block.get("name") or "?"), data, opts)
                    call = Call(station, label, ts, sub=sub, tool=str(block.get("name") or "?"))
                    by_id[block.get("id") or f"x{len(by_id)}"] = call
                    s.calls.append(call)
                    if not sub:
                        step_calls.setdefault(rid, []).append(call)
            elif t == "user":
                msg = r.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str) and not sub and not r.get("isMeta"):
                    if not content.startswith("<"):
                        s.turns += 1
                        s.calls.append(Call("you", words["message"], ts, ts, "done",
                                            kind="message"))
                    continue
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and not sub and not r.get("isMeta"):
                        text = str(block.get("text") or "")
                        if text and not text.startswith(("<", "[Request interrupted")):
                            s.turns += 1
                            s.calls.append(Call("you", words["message"], ts, ts, "done",
                                                kind="message"))
                    if block.get("type") != "tool_result":
                        continue
                    call = by_id.get(block.get("tool_use_id"))
                    if not call:
                        continue
                    call.end = ts
                    text = block.get("content")
                    text = text if isinstance(text, str) else json.dumps(text)[:400]
                    if r.get("toolDenialKind") or re.match(r"^\w+:\w+ hook error", text or ""):
                        call.state = "refused"
                        reason = ""
                        if opts.hook_reasons:
                            reason = ": " + _clean(text.split("\n", 1)[0].split("error:", 1)[-1], 72)
                        s.calls.append(Call("hooks", f"{words['refused']} "
                                            f"{call.tool}{reason}", ts, ts,
                                            "refused", sub=sub, kind="hook"))
                    elif block.get("is_error"):
                        call.state = "failed"
                    else:
                        call.state = "done"
            elif t == "attachment" and not sub:
                a = r.get("attachment") or {}
                kind = a.get("type")
                if kind == "hook_additional_context":
                    body = json.dumps(a.get("content"))[:4000]
                    hook = a.get("hookName") or a.get("hookEvent") or "hook"
                    memory = bool(_MEMORY_HOOK.search(body))
                    s.calls.append(Call("memory" if memory else "hooks",
                                        f"{hook}: {words['added']}", ts, ts, "done",
                                        kind="hook", hook_memory=memory))
                elif kind == "hook_blocking_error":
                    hook = a.get("hookName") or a.get("hookEvent") or "hook"
                    reason = ""
                    if opts.hook_reasons:
                        err = a.get("blockingError") or {}
                        err = err.get("blockingError") if isinstance(err, dict) else err
                        reason = ": " + _clean(str(err).split("\n", 1)[0], 72)
                    s.calls.append(Call("hooks", f"{hook}{reason}", ts, ts, "refused",
                                        kind="hook"))
            elif t == "system" and not sub:
                sub_t = r.get("subtype")
                if sub_t == "compact_boundary":
                    compact_after.add(len(step_order))
                    s.calls.append(Call("model", words["compaction"], ts, ts, "done",
                                        kind="compaction"))
                elif sub_t == "turn_duration":
                    # the answer's row carries the length of the whole turn
                    ms = r.get("durationMs")
                    began = (datetime.fromtimestamp(ts.timestamp() - ms / 1000, ts.tzinfo)
                             if isinstance(ms, (int, float)) and ts else None)
                    s.calls.append(Call("you", words["answer"], began, ts, "done",
                                        kind="answer"))
                elif sub_t == "stop_hook_summary":
                    for info in r.get("hookInfos") or []:
                        ms = info.get("durationMs") if isinstance(info, dict) else None
                        if isinstance(ms, (int, float)) and ms >= 1000 and ts:
                            began = datetime.fromtimestamp(ts.timestamp() - ms / 1000, ts.tzinfo)
                            s.calls.append(Call("hooks", f"Stop: {words['took']}",
                                                began, ts, "done", kind="hook"))

    take(_records(path), sub=False)
    sidecars = _subagent_transcripts(path) or []
    s.subagents = len(sidecars)
    for sidecar in sidecars:
        take(_records(sidecar), sub=True)

    # The README's context cost: a step's growth goes to the calls whose answers
    # went with the next step; 1 call takes it whole, 2-3 share it evenly (~),
    # more get none on their rows. A compaction between puts nothing anywhere.
    for k in range(len(step_order) - 1):
        rid, before = step_order[k]
        after = step_order[k + 1][1]
        calls = step_calls.get(rid) or []
        if not calls or before is None or after is None or (k + 1) in compact_after:
            continue
        growth = after - before
        if growth <= 0:
            continue
        if len(calls) == 1:
            calls[0].ctx = growth
        elif len(calls) <= 3:
            for c in calls:
                c.ctx, c.shared = growth // len(calls), True
        elif len({c.station for c in calls}) == 1:
            calls[0].ctx, calls[0].shared = growth, True  # the station's sum keeps it
    # Subagent calls interleave with the main loop's by time; an answer row sorts at
    # its end, not at the turn's start. A record with no time keeps its place.
    def when(c: Call) -> float:
        moment = c.end if c.kind == "answer" else (c.start or c.end)
        return moment.timestamp() if moment else 0.0
    s.calls.sort(key=when)
    return s


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

COLORS = {"you": "38;5;117", "model": "38;5;183", "memory": "38;5;214", "files": "38;5;114",
          "shell": "38;5;250", "network": "38;5;81", "browser": "38;5;45",
          "agents": "38;5;177", "skills": "38;5;222", "hooks": "38;5;174"}
RED, DIM, BOLD, RESET = "31", "2", "1", "0"


class Paint:
    def __init__(self, on: bool):
        self.on = on

    def __call__(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.on and code else text


def _k(n: int | None, shared: bool = False) -> str:
    if not n:
        return ""
    v = f"{n / 1000:.0f}k" if n >= 1000 else str(n)
    return f"{'~' if shared else ''}+{v}"


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    m, sec = divmod(int(seconds), 60)
    return f"{m}m{sec:02d}s" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def board(s: Session) -> dict:
    out = {st: {"calls": 0, "errors": 0, "seconds": 0.0, "context": 0, "hook_memory": 0}
           for st in STATIONS}
    for c in s.calls:
        if c.kind in ("answer", "switch"):
            continue
        row = out[c.station]
        row["calls"] += 1
        row["errors"] += c.state in ("failed", "refused")
        row["seconds"] += c.seconds or 0.0 if c.kind == "call" else 0.0
        row["context"] += c.ctx or 0
        row["hook_memory"] += c.hook_memory
    return out


def route(s: Session, n: int = 6) -> list[str]:
    stops: list[str] = []
    for c in s.calls:
        if c.kind != "call" or c.sub:
            continue
        if not stops or stops[-1] != c.station:
            stops.append(c.station)
    return stops[-n:]


def rows(s: Session, limit: int | None, include_steps: bool) -> list[tuple[Call, int]]:
    """The list under the map, consecutive done calls at one station folded (`+N`)."""
    out: list[tuple[Call, int]] = []
    for c in s.calls:
        if c.kind == "step" and not include_steps:
            continue
        prev = out[-1] if out else None
        if (prev and c.kind == "call" and prev[0].kind == "call" and c.state == "done"
                and prev[0].state == "done" and prev[0].station == c.station
                and prev[0].sub == c.sub):
            folded = prev[0]
            merged = Call(folded.station, folded.label, folded.start, c.end or folded.end,
                          "done", folded.sub,
                          ((folded.ctx or 0) + (c.ctx or 0)) or None,
                          folded.shared or c.shared or (folded.ctx is None) != (c.ctx is None))
            out[-1] = (merged, prev[1] + 1)
            continue
        out.append((c, 0))
    running = [r for r in out if r[0].state == "running"]
    done = [r for r in out if r[0].state != "running"]
    if limit is None:
        return out
    keep_running = running[-max(1, limit // 2):] if running else []
    return done[-(limit - len(keep_running)):] + keep_running if limit > len(keep_running) else keep_running


MARKS = {"running": "●", "done": "·", "failed": "✕", "refused": "✕", "info": "◆"}


def render_rows(s: Session, opts: Opts, paint: Paint, limit: int | None,
                include_steps: bool, width: int) -> list[str]:
    names = NAMES
    lines = []
    for c, extra in rows(s, limit, include_steps):
        mark = MARKS[c.state]
        mcode = RED if c.state in ("failed", "refused") else (COLORS[c.station] if c.state == "running" else DIM)
        if c.kind == "switch":
            mcode = COLORS["model"]
        station = names[c.station]
        label = ("↳ " if c.sub else "") + c.label + (f" +{extra}" if extra else "")
        right = _dur(c.seconds) if c.kind in ("call", "answer", "hook") else "-"
        cost = _k(c.ctx, c.shared)
        tail = f"{right}{(' ' + cost) if cost else ''}"
        room = max(10, width - 13 - len(tail) - 2)
        label = label if len(label) <= room else label[: room - 1] + "…"
        lines.append(f"{paint(mcode, mark)} {paint(COLORS[c.station], station.ljust(9))} "
                     f"{label.ljust(room)} {paint(DIM, tail)}")
    return lines


def render_board(s: Session, opts: Opts, paint: Paint, window: int) -> list[str]:
    names, w = NAMES, WORDS
    b = board(s)
    pct = f"{100 * s.context / window:.0f}%" if s.context and window else "?"
    pcode = RED if s.context and s.context >= 0.9 * window else ("33" if s.context and s.context >= 0.7 * window else "")
    span = (s.last - s.started).total_seconds() if s.started and s.last else None
    head = (f"{paint(BOLD, 'lot-map')}  {s.path.stem[:8]}  {_short_model(s.model)}  "
            f"{w['context']} {paint(pcode, pct)} ({_k(s.context).lstrip('+') or '?'} / "
            f"{window // 1000}k)  steps {s.steps}  turns {s.turns}  "
            f"subagents {s.subagents}  span {_dur(span)}")
    lines = [head, ""]
    lines.append(paint(DIM, f"{'':10}{w['calls']:>7}{w['errors']:>8}{w['time']:>10}{w['context']:>11}"))
    for st in STATIONS:
        r = b[st]
        if not r["calls"]:
            continue
        mem = f"  ↓{r['hook_memory']}" if r["hook_memory"] else ""
        err = paint(RED, f"{r['errors']:>8}") if r["errors"] else f"{'':>8}"
        lines.append(f"{paint(COLORS[st], names[st].ljust(10))}{r['calls']:>7}{err}"
                     f"{_dur(r['seconds'] or None) if st not in ('you', 'model') else '':>10}"
                     f"{_k(r['context']):>11}{mem}")
    rt = route(s)
    lines += ["", f"{w['route']}: " + " → ".join(paint(COLORS[x], names[x]) for x in rt)]
    running = [c for c in s.calls if c.state == "running" and c.kind == "call"]
    now = (", ".join(f"{names[c.station]} {c.label}" for c in running[-3:])
           if running else w["idle"])
    lines.append(f"{w['now']}: {now}")
    return lines


# --- the braille map ---------------------------------------------------------

_DOT = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (1, 0): 0x08, (1, 1): 0x10,
        (1, 2): 0x20, (0, 3): 0x40, (1, 3): 0x80}


class Canvas:
    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        self.dots = [[0] * cols for _ in range(rows)]
        self.color: list[list[str]] = [[""] * cols for _ in range(rows)]
        self.text: dict[tuple[int, int], tuple[str, str]] = {}

    def dot(self, x: int, y: int, color: str) -> None:
        cx, cy = x // 2, y // 4
        if 0 <= cx < self.cols and 0 <= cy < self.rows:
            self.dots[cy][cx] |= _DOT[(x % 2, y % 4)]
            self.color[cy][cx] = color

    def line(self, x0, y0, x1, y1, color: str) -> None:
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) or 1
        for i in range(steps + 1):
            self.dot(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps), color)

    def write(self, col: int, row: int, text: str, color: str) -> None:
        for i, ch in enumerate(text):
            if 0 <= col + i < self.cols and 0 <= row < self.rows:
                self.text[(col + i, row)] = (ch, color)

    def lines(self, paint: Paint) -> list[str]:
        out = []
        for y in range(self.rows):
            buf = []
            for x in range(self.cols):
                if (x, y) in self.text:
                    ch, col = self.text[(x, y)]
                    buf.append(paint(col, ch))
                elif self.dots[y][x]:
                    buf.append(paint(self.color[y][x], chr(0x2800 + self.dots[y][x])))
                else:
                    buf.append(" ")
            out.append("".join(buf).rstrip())
        return out


def render_map(s: Session, opts: Opts, paint: Paint, window: int,
               cols: int = 76, rows_: int = 21) -> list[str]:
    names = NAMES
    b = board(s)
    c = Canvas(cols, rows_)
    W, H = cols * 2, rows_ * 4
    cx, cy = W / 2, H / 2
    rx, ry = W * 0.36, H * 0.40
    total = sum(v["calls"] for v in b.values()) or 1
    running = {x.station for x in s.calls if x.state == "running" and x.kind == "call"}
    for i, st in enumerate(STATIONS):
        a = 2 * math.pi * i / len(STATIONS) - math.pi / 2
        sx, sy = cx + rx * math.cos(a), cy + ry * math.sin(a)
        used = b[st]["calls"]
        color = (RED if b[st]["errors"] else COLORS[st]) if used else DIM
        if used:
            c.line(cx + 7 * math.cos(a), cy + 7 * math.sin(a), sx, sy, COLORS[st])
            if used / total > 0.15:  # a well-worn spoke gets a second line
                ox, oy = -math.sin(a), math.cos(a)
                c.line(cx + 7 * math.cos(a) + ox, cy + 7 * math.sin(a) + oy, sx + ox, sy + oy,
                       COLORS[st])
        # the station: an empty ring unused, a ring with a dot used, a full one at work
        for k in range(16):
            t = 2 * math.pi * k / 16
            c.dot(round(sx + 3 * math.cos(t)), round(sy + 2 * math.sin(t)), color)
        if used:
            c.dot(round(sx), round(sy), color)
        if st in running:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    c.dot(round(sx + dx), round(sy + dy), COLORS[st])
        label = names[st]
        extra = []
        if used:
            extra.append(str(used))
        if b[st]["errors"]:
            extra.append(f"!{b[st]['errors']}")
        if b[st]["context"]:
            extra.append(_k(b[st]["context"]))
        text = label + (" " + " ".join(extra) if extra else "")
        col = int(sx / 2)
        row = int(sy / 4)
        if math.cos(a) < -0.2:
            col = col - len(text) - 2
        elif math.cos(a) > 0.2:
            col = col + 2
        else:
            col = col - len(text) // 2
        row = row - 1 if math.sin(a) < -0.5 else (row + 1 if math.sin(a) > 0.5 else row)
        c.write(max(0, min(cols - len(text), col)), row, text, COLORS[st] if used else DIM)
    # the centre: its outline is the context window, filling clockwise from the top
    fill = min(1.0, (s.context or 0) / window) if window else 0.0
    ccode = RED if fill >= 0.9 else ("33" if fill >= 0.7 else COLORS["model"])
    for k in range(40):
        t = 2 * math.pi * k / 40 - math.pi / 2
        c.dot(round(cx + 6 * math.cos(t)), round(cy + 4.5 * math.sin(t)),
              ccode if k / 40 < fill else DIM)
    pct = f"{100 * fill:.0f}%"
    c.write(int(cx / 2) - len(pct) // 2, int(cy / 4) - 1, pct, ccode)
    core = "CLAUDE"
    c.write(int(cx / 2) - len(core) // 2, int(cy / 4), core, BOLD)
    model = _short_model(s.model)
    c.write(int(cx / 2) - len(model) // 2, int(cy / 4) + 1, model, DIM)
    return c.lines(paint)


# ---------------------------------------------------------------------------
# Choosing the transcript
# ---------------------------------------------------------------------------

def pick_transcript(args) -> Path:
    if args.transcript:
        path = Path(args.transcript).expanduser()
        if not path.is_file():
            raise SystemExit(f"lot-map: no transcript at {path}")
        return path
    project = Path(args.project).expanduser() if args.project else Path.cwd()
    directory = transcript_dir(project)
    if directory is None or not directory.is_dir():
        raise SystemExit(f"lot-map: no transcript directory for {project}")
    if args.session:
        path = directory / f"{args.session}.jsonl"
        if not path.is_file():
            raise SystemExit(f"lot-map: no session {args.session} under {directory}")
        return path
    found = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not found:
        raise SystemExit(f"lot-map: no transcripts under {directory}")
    return found[-1]


def memory_roots() -> list[Path]:
    """The workspace's memory folders, by logical name; none on a public clone."""
    try:
        return [data_path("auto-memory").resolve()]
    except DataPathError as exc:
        print(f"lot-map: memory folder `auto-memory` not resolved: {exc}", file=sys.stderr)
        return []


def _pairs(values: list[str] | None, valid: set[str] | None = None) -> dict[str, str]:
    out = {}
    for item in values or []:
        for part in item.split(","):
            key, sep, value = part.partition("=")
            if sep and key.strip() and (valid is None or value.strip() in valid):
                out[key.strip()] = value.strip()
            elif part.strip():
                print(f"lot-map: ignored `{part.strip()}` (want name=station)", file=sys.stderr)
    return out


def render(s: Session, opts: Opts, args, paint: Paint) -> str:
    width = args.width or min(120, max(60, _term_width()))
    out = render_board(s, opts, paint, args.window)
    if args.map:
        out += [""] + render_map(s, opts, paint, args.window)
    out += [""] + render_rows(s, opts, paint, None if args.rows == "all" else int(args.rows),
                              args.steps, width)
    return "\n".join(out)


def _mtime(path: Path) -> float | None:
    """A file's mtime, or None for one gone between listing and stat (the frame still redraws)."""
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 100


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lot-map.py", description=(
        "A map of where a Claude Code session's work went: stations, route and rows, "
        "read from its transcript. Reads files only; sends nothing anywhere."))
    where = p.add_argument_group("which session")
    where.add_argument("--transcript", help="a transcript file (.jsonl)")
    where.add_argument("--project", help="a checkout; its newest session unless --session")
    where.add_argument("--session", help="a session id under --project (or this checkout)")
    p.add_argument("--map", action="store_true", help="draw the board in braille dots")
    p.add_argument("--rows", default="16", help="rows under the board: a number or `all`")
    p.add_argument("--steps", action="store_true", help="list model steps as rows too")
    p.add_argument("--window", type=int, default=WINDOW_DEFAULT,
                   help=f"context window in tokens (default {WINDOW_DEFAULT})")
    p.add_argument("--hook-reasons", action="store_true",
                   help="show the first line of a hook's refusal (it may quote what it found)")
    p.add_argument("--memory-server", action="append", help="MCP servers that light Memory")
    p.add_argument("--memory-command", action="append", help="commands that light Memory")
    p.add_argument("--server-station", action="append", help="server=station, comma-separated")
    p.add_argument("--command-station", action="append", help="command=station, comma-separated")
    p.add_argument("--json", action="store_true", help="print the board as JSON")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--width", type=int, help="row width (default: the terminal's)")
    p.add_argument("--follow", action="store_true", help="redraw every second until Ctrl-C")
    p.add_argument("--for", dest="for_seconds", type=float,
                   help="with --follow, stop after this many seconds")
    args = p.parse_args(argv)
    if args.rows != "all" and not args.rows.isdigit():
        p.error("--rows takes a number or `all`")

    opts = Opts(hook_reasons=args.hook_reasons, memory_roots=memory_roots())
    for item in args.memory_server or []:
        opts.memory_servers |= {x.strip() for x in item.split(",") if x.strip()}
    for item in args.memory_command or []:
        opts.memory_commands |= {x.strip() for x in item.split(",") if x.strip()}
    stations = set(STATIONS) - {"model", "memory", "hooks"}
    opts.server_stations.update(_pairs(args.server_station, stations))
    opts.command_stations.update(_pairs(args.command_station, stations))

    path = pick_transcript(args)
    paint = Paint(sys.stdout.isatty() and not args.no_color and not args.json)

    if args.json:
        s = read_session(path, opts)
        print(json.dumps({"transcript": str(path), "model": _clean(s.model, 80), "context": s.context,
                          "window": args.window, "steps": s.steps, "turns": s.turns,
                          "subagents": s.subagents, "route": route(s), "board": board(s)},
                         indent=2, ensure_ascii=False))
        return 0
    if not args.follow:
        print(render(read_session(path, opts), opts, args, paint))
        return 0
    deadline = time.monotonic() + args.for_seconds if args.for_seconds else None
    mtime = None
    try:
        while deadline is None or time.monotonic() < deadline:
            # the transcript and every subagent sidecar beside it: either may grow
            current = tuple(_mtime(f) for f in [path, *(_subagent_transcripts(path) or [])])
            if current != mtime:
                mtime = current
                frame = render(read_session(path, opts), opts, args, paint)
                sys.stdout.write("\x1b[H\x1b[2J" + frame + "\n" if paint.on else frame + "\n\n")
                sys.stdout.flush()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
