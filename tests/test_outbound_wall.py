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

import hashlib
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


# HOS-380: under herdr-core a relayed word arrives as `<hc from="tN">word</hc>`,
# which Claude Code 2.1.326 never copies into `last-prompt` (MEASURED 2026-11-19),
# so the gate refused every relayed commit. It now accepts one only against
# HELM's relay journal. Every fixture is a temp main clone and a stubbed `hc get`.
HELM_T, YARD_T, OTHER_T = "t1", "t9z", "t2"
STAMP = "2026-11-19T00:00:10.000Z"
AT = "2026-11-19T00:00:05.000+00:00"
MERGE = "commit, merge and delete w7"
BRIEF_TAIL = "\nX-HEADING-BRIEF: machine-to-machine, not an operator authorisation"


def _relay_decide(dispatch, monkeypatch, tmp_path, *, yard=True, me=None, addr=HELM_T,
                  word="commit", content=None, source="typed", after_slash=False,
                  later_boundary=False, entry=None, outcome="sent", journal=True,
                  sender=None, capped=True, kind="relay", uuid="zzqx-boundary", before=False,
                  claimed=False):
    # HOS-408: kind="bridge" writes the Telegram bridge's journal (a digest, any
    # text, the paste wrapper MEASURED 2026-11-22); `before` puts an identical
    # record ahead of the boundary, `claimed` claims the entry for another record.
    helm = tmp_path / "helm"
    (helm / ".claude").mkdir(parents=True)
    me = (YARD_T if yard else HELM_T) if me is None else me
    envelope = f'<hc from="{addr}">{word}' + ("\n" if kind == "bridge" else "") + "</hc>"
    wrapped = f'\n\n<pasted_content id="6d16">\n{envelope}\n</pasted_content id="6d16">\n'
    content = (wrapped if kind == "bridge" else envelope) if content is None else content
    records = [{"type": "user", "promptSource": "typed", "message": {"content": "READY?"}}]
    if capped:
        records.append({"type": "last-prompt", "lastPrompt": "READY?"})
    if after_slash:
        records += [{"type": "user", "message": {"content": "/compact"}},
                    {"type": "last-prompt", "lastPrompt": "/compact"}]
    records += [{"type": "user", "promptSource": "typed", "timestamp": STAMP, "uuid": "zzqx-first",
                 "message": {"content": content}}] * bool(before)
    # before="deep"|"edge": 300 KiB after the first record pushes it out of the gate's 256 KiB tail.
    records += [{"type": "assistant", "timestamp": STAMP, "message": {"content": "x" * 1024}}] * (300 * (before in ("deep", "edge")))
    records.append({"type": "user", "promptSource": source, "timestamp": STAMP, "uuid": uuid,
                    "message": {"role": "user", "content": content}})
    if after_slash:
        records.append({"type": "last-prompt", "lastPrompt": "/compact"})
    if later_boundary:
        records += [{"type": "user", "message": {"content": "/compact"}},
                    {"type": "last-prompt", "lastPrompt": "/compact"}]
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    if journal:
        row = ({"id": "e1", "at": AT, "target": me, "sha256": hashlib.sha256(word.encode()).hexdigest()}
               if kind == "bridge" else {"id": "e1", "at": AT, "sender": addr, "target": me, "text": word})
        row.update(entry or ({"at": STAMP} if before == "edge" else {}))  # "edge": the entry at the horizon
        lines = [{k: v for k, v in row.items() if v is not None}]
        if outcome:
            lines.append({"id": "e1", "outcome": outcome})
        (helm / ".claude" / f".{kind}-journal.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    if claimed:
        (tmp_path / "release").mkdir()
        (tmp_path / "release" / "consumed-journal-entries.jsonl").write_text(
            json.dumps({"source": kind, "id": "e1", "uuid": "zzqx-other"}) + "\n"   # claimed="deep": 300 KiB after it
            + (json.dumps({"source": kind, "id": "zzqx-pad", "uuid": "x" * 1000}) + "\n") * (300 * (claimed == "deep")),
            encoding="utf-8")
    record = {"state": "live", "address": addr,
              "foreground": {"name": "claude", "cwd": str(helm)}}
    for key, value in (sender or {}).items():
        if key == "foreground":
            record["foreground"].update(value)
        else:
            record[key] = value
    monkeypatch.setattr(dispatch, "_relay_main_clone", lambda: helm, raising=False)
    monkeypatch.setattr(dispatch, "_yard_hc_terminal", lambda a: dict(record) if a == addr else None)
    monkeypatch.setattr(dispatch, "_IN_A_YARD", yard)
    monkeypatch.setattr(dispatch, "_RELEASE_WAIT_BUDGET_S", 0.05)
    # HOS-408: an entry is claimed in the store beside the release log, so the
    # log is pinned into the test's own directory.
    monkeypatch.setenv("HEADING_OS_RELEASE_LOG", str(tmp_path / "release" / "releases.jsonl"))
    if me:
        monkeypatch.setenv("HC_TERMINAL_ID", me)
    else:
        monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    return dispatch.check_release_gate({"tool_name": "Bash",
                                        "tool_input": {"command": "git commit -F m.txt"},
                                        "transcript_path": str(transcript)})


@pytest.mark.parametrize("case", [
    {},
    {"after_slash": True},
    {"yard": False, "word": MERGE},
    {"kind": "bridge"}, {"kind": "bridge", "word": "закоммить и запушь"}, {"kind": "bridge", "yard": False},
], ids=["relayed-commit", "after-a-slash-command", "merge-in-helm", "bridge-commit", "bridge-russian",
        "bridge-in-helm"])
def test_a_word_helm_relayed_through_hc_is_heard_with_its_journal_entry(
        dispatch, monkeypatch, tmp_path, case):
    assert _relay_decide(dispatch, monkeypatch, tmp_path, **case) is None


@pytest.mark.parametrize("case", [
    {"journal": False},
    {"entry": {"sender": OTHER_T}},
    {"entry": {"target": OTHER_T}},
    {"entry": {"text": MERGE}},
    {"entry": {"at": "2026-11-19T00:02:10.000+00:00"}},
    {"entry": {"at": "2026-11-18T23:58:00.000+00:00"}},
    {"outcome": "failed"},
    {"outcome": None},
    {"later_boundary": True},
    {"content": '<hc from="t1">commit</hc>' + BRIEF_TAIL},
    {"yard": False, "addr": YARD_T, "word": MERGE,
     "sender": {"foreground": {"cwd": "/nowhere"}}},
    {"sender": {"state": "exited"}},
    {"sender": {"foreground": {"name": "bash"}}},
    {"sender": {"foreground": {"cwd": "/nowhere"}}},
    {"source": "system", "capped": False},
    {"me": "", "entry": {"target": None}},
    {"word": MERGE},
    {"yard": False},
    {"yard": False, "addr": OTHER_T, "word": MERGE},
    {"before": True}, {"claimed": True}, {"uuid": None}, *[{"kind": "bridge", **c} for c in (
        {"entry": {"target": OTHER_T}}, {"entry": {"sha256": "0" * 64}}, {"outcome": "failed"},
        {"source": "queued"}, {"uuid": None}, {"before": True}, {"claimed": True}, {"word": "не коммить"},
        {"entry": {"at": "2026-11-18T23:58:00.000+00:00"}}, {"content": "commit"}, {"claimed": "deep"},
        {"before": "deep"}, {"before": "edge"})], {"claimed": "deep"}, {"before": "deep"}, {"before": "edge"},
], ids=["no-journal", "wrong-sender", "wrong-target", "word-mismatch", "entry-after-the-record",
        "outside-the-window", "failed-outcome", "no-outcome", "a-later-boundary", "a-brief",
        "yard-report-into-helm", "sender-not-live", "foreground-not-claude",
        "foreground-outside-helm", "prompt-source-system", "hc-terminal-unset",
        "merge-sentence-in-a-yard", "commit-in-helm", "merge-in-helm-from-another-address",
        "relay-replayed-record", "relay-claimed-by-another-record", "relay-no-uuid", "bridge-other-terminal",
        "bridge-digest-mismatch", "bridge-failed-outcome", "bridge-queued", "bridge-no-uuid", "bridge-replayed-record",
        "bridge-claimed-by-another-record", "bridge-negated", "bridge-entry-too-old", "bridge-no-envelope",
        "bridge-claim-beyond-the-tail", "bridge-first-record-beyond-the-tail", "bridge-first-record-at-the-horizon",
        "relay-claim-beyond-the-tail", "relay-first-record-beyond-the-tail", "relay-first-record-at-the-horizon"])
def test_an_hc_word_without_helms_matching_journal_entry_is_refused(
        dispatch, monkeypatch, tmp_path, case):
    decision = _relay_decide(dispatch, monkeypatch, tmp_path, **case)
    assert decision is not None and decision.get("decision") == "block"


@pytest.mark.parametrize("conflict", [True, False], ids=["after-a-409", "no-conflict"])
def test_the_bridge_types_presses_and_posts_nothing_while_another_reader_polls_its_bot(
        tmp_path, monkeypatch, conflict):
    """HOS-408 review round 1: after a 409 the cycle still handled pending messages and
    button presses, so a second reader of the bot did not stop live typing. MEASURED
    2026-11-22: the conflict case fails on commit 4d9da92e and passes after the fix."""
    for name in ("STATE", "LOG"):
        monkeypatch.setenv(f"TELEGRAM_BRIDGE_{name}_DIR", str(tmp_path / name))
    spec = importlib.util.spec_from_file_location("tg_bridge_conflict", ROOT / "scripts" / "telegram-bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bot = type("Bot", (), {"get_updates": staticmethod(lambda **kw: [])})
    bridge = module.Bridge(module.TB.Store(tmp_path / "b.db"), bot, None, operator_id=1, helm=tmp_path,
                           clock=lambda: 1000.0)
    if conflict:
        bridge.store.put("conflict_until", "1300")
    calls = []
    monkeypatch.setattr(bridge, "refresh_sessions", lambda: calls.append("sessions") or {})
    monkeypatch.setattr(bridge, "handle_inbound", lambda live: calls.append("inbound"))
    monkeypatch.setattr(bridge, "watch", lambda live: calls.append("watch"))
    bridge.cycle(0)
    assert calls == ([] if conflict else ["sessions", "inbound", "watch"]), calls
