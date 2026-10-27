#!/usr/bin/env python3
"""What runs in the background of THIS session, for the /second-wind fork.

One question only: would `/clear` stop something? Answered from the Stop hook's
documented `background_tasks` and `session_crons` fields
(https://code.claude.com/docs/en/hooks#stop-input), the session's own transcript
for `/goal`, and the ralph-loop state file. A shell task is not counted:
MEASURED 2026-10-14, 21 background shell commands survived `/clear` on this
machine and none was killed. Anything that cannot be read answers "unknown",
and the caller treats unknown as running, so an error costs a compaction, never
the work.

Usage (library):
    from scripts.utils.background_work import background_state
    status, names = background_state(stop_payload, project_root)
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.utils import checkpoint_autonomy as AU
from scripts.utils import checkpoint_paths as CP

GOAL_TAG = "<command-name>/goal</command-name>"
GOAL_CLEAR_WORDS = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})


def _command_args(content: str) -> str:
    start = content.find("<command-args>")
    end = content.find("</command-args>")
    if start < 0 or end < start:
        return ""
    return content[start + len("<command-args>"):end].strip()


def goal_active(transcript_path: str | None) -> bool | None:
    """Is a `/goal` set and not cleared? None when the transcript cannot be read.

    Only an operator-typed slash command counts: a `type: user` entry whose
    string content carries the `/goal` command tag. That is how this machine's
    transcripts record `/compact` (299) and `/clear` (121), MEASURED 2026-10-14.
    Text inside tool calls, tool results or replies is never read, so a session
    that only discusses `/goal` answers False. A goal that was achieved is not
    detected (its marker's format is unseen here), which costs one compaction.
    `/goal` itself had never run on this machine on 2026-10-14; the format is
    confirmed at acceptance item 8 of the /second-wind spec.
    """
    if not transcript_path:
        return None
    active = False
    try:
        with Path(transcript_path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if GOAL_TAG not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "user":
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.lstrip().startswith("<command-"):
                    continue
                if GOAL_TAG not in content:
                    continue
                args = _command_args(content)
                active = bool(args) and args.split()[0].lower() not in GOAL_CLEAR_WORDS
    except OSError:
        return None
    return active


def _task_name(task: dict) -> str:
    kind = str(task.get("type") or "task")
    label = task.get("description") or task.get("name") or task.get("agent_type") or ""
    return f"{kind} {label!r}" if label else kind


def background_state(payload: dict, project: Path | None = None) -> tuple[str, list[str]]:
    """("none" | "running" | "unknown", names) for this Stop payload."""
    names: list[str] = []
    unknown = False

    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        unknown = True
    else:
        for task in tasks:
            if not isinstance(task, dict):
                unknown = True
                continue
            status = str(task.get("status") or "").strip().lower()
            if status in AU.TERMINAL_TASK_STATES:
                continue
            if str(task.get("type") or "").lower() == "shell":
                continue
            names.append(_task_name(task))

    crons = payload.get("session_crons")
    if not isinstance(crons, list):
        unknown = True
    else:
        for cron in crons:
            prompt = cron.get("prompt") if isinstance(cron, dict) else None
            names.append(f'/loop "{str(prompt or "")[:40]}"')

    goal = goal_active(payload.get("transcript_path"))
    if goal is None:
        unknown = True
    elif goal:
        names.append("/goal")

    if project is not None:
        owner = AU._ralph_owner(project)
        if owner is not None and owner in ("", CP.session_id(payload)):
            names.append("ralph-loop")

    if names:
        return ("running", names)
    return ("unknown", []) if unknown else ("none", [])
