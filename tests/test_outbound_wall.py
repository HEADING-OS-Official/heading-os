"""The outbound wall (HOS-272): MCP sends, shares and deletes, Exchange invites and
mail deletion, and the git subcommands that write a
commit of their own, all through `check_release_gate`.

Failure B: without this wall a yard, or a session steered by fetched content,
reaches a third party or rewrites a branch on nobody's word. Both directions
are asserted in one test, floors outside the loops, so a case table that came
back short cannot pass while checking less than it claims.

The dispatcher is loaded from `OUTBOUND_WALL_DISPATCH` when it is set, so the
same file can be run against a previous `_dispatch.py` extracted OUTSIDE the
checkout (K011); `WORKSPACE` is then pointed back at this checkout so both
copies resolve the same roots.

HOS-324 (2026-11-02) adds Google Calendar connector writes, which carry no send
word in their names: each is a send the operator must ask for, while calendar
reads and Gmail drafting stay free.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FAKE_SESSION = "zzqx0000-0000-4000-8000-000000000000"

# Counted 2026-11-02 (HOS-324), when the Google Calendar cases were added, less
# the five cases of the assistant-channel capture the official tree does not ship.
REFUSED_FLOOR = 48
ALLOWED_FLOOR = 41

NEUTRAL = "посмотри, что там"
SEND = "send it"
SHARE = "расшарь файл"
DELETE = "delete it"
DELETE_RU = "удали их"
COMMIT = "commit and merge"


@pytest.fixture
def dispatch():
    target = Path(os.environ.get("OUTBOUND_WALL_DISPATCH")
                  or ROOT / ".claude" / "hooks" / "_dispatch.py")
    if not target.is_file():
        pytest.fail(f"no dispatcher at {target}")
    saved = sys.path[:]
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location("heading_dispatch_outbound", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.WORKSPACE = ROOT
        yield module
    finally:
        sys.path[:] = saved


def _transcript(path: Path, prompt: str) -> Path:
    """One turn in the record shapes the harness writes: typed, then capped."""
    records = [
        {"type": "user", "promptSource": "typed", "sessionId": FAKE_SESSION,
         "message": {"role": "user", "content": prompt}},
        {"type": "last-prompt", "lastPrompt": prompt, "sessionId": FAKE_SESSION},
        {"type": "assistant", "sessionId": FAKE_SESSION,
         "message": {"role": "assistant", "content": []}},
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in records), encoding="utf-8")
    return path


def _bash(command: str) -> tuple[str, dict]:
    return "Bash", {"command": command}


def _mcp(tool: str) -> tuple[str, dict]:
    return tool, {}


MEETING = ".venv/bin/python scripts/sync-exchange.py --create-meeting X --time T"

# (name, (tool, input), the word that releases it)
_GATED = [
    ("gmail-send", _mcp("mcp__claude_ai_Gmail__send_message"), SEND),
    ("gmail-reply", _mcp("mcp__claude_ai_Gmail__reply"), SEND),
    ("gmail-forward", _mcp("mcp__claude_ai_Gmail__forward"), SEND),
    ("drive-share", _mcp("mcp__claude_ai_Google_Drive__share_file"), SHARE),
    ("gmail-trash", _mcp("mcp__claude_ai_Gmail__trash_message"), DELETE),
    ("lific-delete", _mcp("mcp__lific__delete"), DELETE),
    ("hindsight-delete", _mcp("mcp__hindsight-main__delete_document"), DELETE_RU),
    ("hindsight-invalidate", _mcp("mcp__hindsight-main__invalidate_memory"), DELETE),
    ("hindsight-clear", _mcp("mcp__hindsight-main__clear_mental_model"), DELETE),
    ("camel-send", _mcp("mcp__zzqx__sendMessage"), SEND),
    ("camel-delete", _mcp("mcp__zzqx__deleteFile"), DELETE),
    ("camel-share", _mcp("mcp__zzqx__shareDocument"), SHARE),
    ("exchange-invites", _bash(MEETING + " --attendees a --send-invites"), SEND),
    ("exchange-invites-first", _bash(
        "python3 scripts/sync-exchange.py --send-invites --create-meeting X --time T"),
     SEND),
    ("exchange-invites-abbrev", _bash(
        "python3 scripts/sync-exchange.py --cr X --time T --se"), SEND),
    ("exchange-delete", _bash("python3 scripts/sync-exchange.py --delete S --yes"),
     DELETE),
    ("exchange-delete-abbrev", _bash("python3 scripts/sync-exchange.py --de S"),
     DELETE_RU),
    ("git-merge", _bash("git merge feature"), COMMIT),
    ("git-merge-continue", _bash("git merge --continue"), COMMIT),
    ("git-cherry-pick", _bash("git cherry-pick abc"), COMMIT),
    ("git-revert", _bash("git revert abc"), COMMIT),
    ("git-am", _bash("git am x.patch"), COMMIT),
    ("git-pull", _bash("git pull"), COMMIT),
    # HOS-324: a calendar write can invite or notify a third party, and its name
    # carries no send word, so the calendar server is read by verb.
    ("calendar-create", _mcp("mcp__claude_ai_Google_Calendar__create_event"), SEND),
    ("calendar-update", _mcp("mcp__claude_ai_Google_Calendar__update_event"), SEND),
    ("calendar-respond", _mcp("mcp__claude_ai_Google_Calendar__respond_to_event"), SEND),
    ("calendar-sharing", _mcp("mcp__claude_ai_Google_Calendar__update_calendar_sharing"),
     SEND),
]

# (name, (tool, input), prompt): refused, each for lack of its own word.
REFUSED = [(f"{name}-unasked", call, NEUTRAL) for name, call, _w in _GATED] + [
    ("lific-delete-on-a-send-word", _mcp("mcp__lific__delete"), SEND),
] + [
    # Words about sharing that order nothing (HOS-272 security review).
    (f"{tool}-on-{n}", _mcp(tool), prompt)
    for tool in ("mcp__claude_ai_Gmail__send_message",
                 "mcp__claude_ai_Google_Drive__share_file")
    for n, prompt in (("an-infinitive", "хочу поделиться мыслью"),
                      ("give-access", "дай доступ к логам"),
                      ("open-access", "открой доступ к файлу"),
                      ("and-share", "and share the repo layout with me"),
                      ("negated-share", "не расшарь файл"),
                      ("negated-share-ru", "не поделись этим"),
                      ("an-opinion", "поделись мнением"),
                      ("podelis-file", "поделись файлом"),
                      ("share-it", "share it"),
                      ("please-share", "please share the file"))
]

ALLOWED = [(f"{name}-asked", call, word) for name, call, word in _GATED] + [
    ("meeting-without-invites", _bash(MEETING), NEUTRAL),
    ("git-merge-no-commit", _bash("git merge --no-ff --no-commit feature"), NEUTRAL),
    ("git-merge-abort", _bash("git merge --abort"), NEUTRAL),
    ("git-cherry-pick-n", _bash("git cherry-pick -n abc"), NEUTRAL),
    ("gmail-untrash", _mcp("mcp__claude_ai_Gmail__untrash_message"), NEUTRAL),
    ("gmail-list-drafts", _mcp("mcp__claude_ai_Gmail__list_drafts"), NEUTRAL),
    ("camel-untrash", _mcp("mcp__zzqx__untrashMessage"), NEUTRAL),
    ("camel-list-drafts", _mcp("mcp__zzqx__listDrafts"), NEUTRAL),
    ("hindsight-recall", _mcp("mcp__hindsight-main__recall"), NEUTRAL),
    ("drive-share-on-a-send-word", _mcp("mcp__claude_ai_Google_Drive__share_file"),
     "отправь файл"),
    # HOS-324: calendar reads stay free, and so does drafting (M002.1 needs it).
    ("calendar-list", _mcp("mcp__claude_ai_Google_Calendar__list_events"), NEUTRAL),
    ("calendar-search", _mcp("mcp__claude_ai_Google_Calendar__search_events"), NEUTRAL),
    ("calendar-suggest", _mcp("mcp__claude_ai_Google_Calendar__suggest_time"), NEUTRAL),
    ("gmail-create-draft", _mcp("mcp__claude_ai_Gmail__create_draft"), NEUTRAL),
]


def test_the_outbound_wall_refuses_unasked_and_hears_the_operator(dispatch, tmp_path):
    assert len(REFUSED) >= REFUSED_FLOOR, (
        f"{len(REFUSED)} refusal cases, below the {REFUSED_FLOOR} counted 2026-11-02")
    assert len(ALLOWED) >= ALLOWED_FLOOR, (
        f"{len(ALLOWED)} acceptance cases, below the {ALLOWED_FLOOR} counted 2026-11-02")

    def decide(name: str, call: tuple[str, dict], prompt: str):
        tool, tool_input = call
        path = _transcript(tmp_path / f"{name}.jsonl", prompt)
        return dispatch.check_release_gate(
            {"tool_name": tool, "tool_input": tool_input,
             "transcript_path": str(path)})

    survivors = [name for name, call, prompt in REFUSED
                 if decide(f"refuse-{name}", call, prompt) is None]
    assert not survivors, f"the release gate released on {survivors}"

    deaf = [name for name, call, prompt in ALLOWED
            if decide(f"allow-{name}", call, prompt) is not None]
    assert not deaf, f"the release gate did not hear the operator on {deaf}"
