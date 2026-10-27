"""Who produced an input turn, and the signature the record writer reads it under.

In Claude Code the `user` role is the SESSION'S INPUT CHANNEL, not a claim that
a person typed anything: briefs delivered from another session, background-task
notifications, hook feedback, system reminders, a skill's injected instructions
and the output of a `!` command all arrive on it. MEASURED 2026-09-23 over the
memory pilot's capture archive, 613 entries: 578 carried the operator's
signature and 227 of those were machine-produced, among them 133 background-task
notifications and 39 machine-to-machine briefs. A memory that attributes a brief
to the operator is worse than one that lost it, because it will be quoted back
as something he said.

One reader. `.claude/hooks/codegraph-gate.py` loads this file by path to tell a
task someone set from a machine event. It moved here from the retired record
writer's package, which signed Hindsight turns with it, on 2026-10-16.

ANCHORED ON `startswith`, NEVER ON CONTAINMENT, and the reason is this file's
own subject matter. `X-HEADING-BRIEF` appears in the body of any turn that
DISCUSSES briefs, including the one that commissioned this module; a substring
test would file that discussion as a brief. Every marker below is matched at
position 0 of the stripped body and nowhere else.

WHAT IS CLASSIFIED IS THE FORM, AND NOTHING IS DROPPED. Every turn is still
read; only the line naming its source changes.

WHAT THIS DOES NOT ESTABLISH. It reads the head of the text, so a turn whose
operator words follow an injected block (a system reminder, a hook's feedback)
is filed by the block. That misattributes an operator turn to the machine, which
is the safe direction and the deliberate one. And an operator message that
literally OPENS with one of the markers below is filed as machine; nothing in
the text can distinguish the two, and `startswith` is the narrowest test that
catches the real forms.
"""

from __future__ import annotations

# The signature a genuine operator turn has always carried. Kept byte-identical
# so that repairing the machine forms does not rewrite the true ones.
OPERATOR = "operator"
OPERATOR_PREFIX = "Operator asked:"

# (form, markers matched at position 0, the signature written for that form).
# Order is specificity: the first match wins, and nothing here overlaps today.
#
# Each signature NAMES ITS SOURCE rather than hedging, because the retrieval
# side reads these as prose. "A background task reported" is a fact about where
# the text came from; "Captured input" would be true and useless.
FORMS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "brief",
        ("X-HEADING-BRIEF:",),
        "A brief from another session, machine-to-machine, not the operator:",
    ),
    (
        "cross-session",
        ("Another Claude session sent a message:",
         "<agent-message",
         "<cross-session-message"),
        "Another Claude session sent:",
    ),
    (
        "harness-notice",
        ("[Cross-session delivery notice]",),
        "The harness reported:",
    ),
    (
        "task-notification",
        ("<task-notification>",),
        "A background task reported:",
    ),
    (
        "hook-feedback",
        ("Stop hook feedback:",
         "PreToolUse hook feedback:",
         "PostToolUse hook feedback:",
         "SessionStart hook additional context:",
         "UserPromptSubmit hook additional context:"),
        "A hook injected:",
    ),
    (
        "system-reminder",
        ("<system-reminder>",),
        "The system reminded this session:",
    ),
    (
        "skill-prompt",
        ("Base directory for this skill:", "<command-name>", "<command-message>"),
        "A skill's instructions were injected:",
    ),
    (
        "command-output",
        ("<bash-input>", "<bash-stdout>", "<bash-stderr>",
         "<local-command-stdout>", "<local-command-stderr>"),
        "A command's output was injected:",
    ),
)

SIGNATURES: dict[str, str] = {OPERATOR: OPERATOR_PREFIX,
                              **{form: sig for form, _, sig in FORMS}}


def classify(text: str) -> str:
    """The form of one captured turn. Returns `operator` when nothing matches.

    `operator` is the DEFAULT rather than a marker of its own, because a person
    typing has no marker to write. That is what made the original defect
    invisible: the absence of evidence read as evidence of the operator.
    """
    head = (text or "").lstrip()
    for form, markers, _ in FORMS:
        if head.startswith(markers):
            return form
    return OPERATOR


def signature(form: str) -> str:
    """The prefix written for a form. Unknown forms fall back to naming that.

    Never falls back to `Operator asked:`: a form this module does not recognise
    is precisely the case where claiming the operator would be a guess.
    """
    return SIGNATURES.get(form, "Captured input of an unrecognised kind:")


def sign(body: str) -> tuple[str, str]:
    """Classify `body` and return `(form, signed text)`. What the hook writes."""
    form = classify(body)
    return form, f"{signature(form)} {body}"
