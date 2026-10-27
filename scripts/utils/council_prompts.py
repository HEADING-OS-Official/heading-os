"""Shared council prompt builders.

Single source of truth for the company context block and the independent/critique
prompt builders consumed by the council consult wrappers (gemini/grok/kimi/chatgpt).
Extracted 2026-06-18, when the third model (Kimi) was added. The only read is
the operator's `advisor_context`, through `get_operator()`, which never raises.
"""
from __future__ import annotations

from scripts.utils.untrusted_input import neutralise_closing_tags

#: What a council consult opens with on a clone whose operator.yaml carries no
#: `advisor_context`. The operator's own block (company, vocabulary, constraints)
#: is instance data and lives in the data repository, never here.
GENERIC_ADVISOR_BLOCK = (
    "You are advising the operator of this HEADING OS workspace, who leads a company. "
    "No business context is configured, so ask for what you need rather than assume it."
)


def advisor_block() -> str:
    """The consult's opening context: `advisor_context` from operator.yaml, else the generic block."""
    from scripts.utils.operator_identity import get_operator
    return get_operator().get("advisor_context", "").strip() or GENERIC_ADVISOR_BLOCK

# A council consult wants a compact position, not an essay, so both builders ask
# for one. It became a parameter on 2026-08-23: the 2026-08-23 engine audit sent
# its per-file shards through `kimi-consult --mode independent`, and every shard
# inherited this cap while its own question said "list EVERY defect". The cap is
# right for the thing these builders were written for and wrong for enumeration,
# so the caller now chooses. Pass "" (or None) to omit the sentence entirely.
DEFAULT_LENGTH_HINT = "Aim for 200-400 words."


# The draft and the context are pasted text and can carry a literal closing
# tag; `neutralise_closing_tags` keeps one from ending the wrapper early.
_WRAPPERS = ("draft", "context")


def _tagged(tag: str, text: str) -> str:
    """Wrap ``text`` in ``<tag>`` with any inner closing tag neutralised."""
    body = neutralise_closing_tags(text.strip(), _WRAPPERS)
    return f"<{tag}>\n{body}\n</{tag}>"


def _with_hint(instruction: str, length_hint: str | None) -> str:
    """Append the length sentence to an Output instruction, or leave it off."""
    hint = (length_hint or "").strip()
    return f"{instruction} {hint}" if hint else instruction


def build_independent_prompt(question: str, context: str = "",
                             length_hint: str | None = DEFAULT_LENGTH_HINT) -> str:
    """Build the independent-perspective prompt for a council advisor.

    Frames the receiving model as a second-opinion advisor that reasons from
    first principles and reaches its own conclusion without deferring to any
    prior framing. ``context``, if non-empty, is placed in a ``<context>`` tag
    under a ``## Context`` section BEFORE the question, so the long material
    comes first and the question sits next to the output instruction.
    ``length_hint`` is the closing length instruction; pass ``""`` or
    ``None`` for an enumerating task that must not be capped. Returns the full
    prompt string.
    """
    parts = [advisor_block(), ""]
    parts.append("## Your role")
    parts.append(
        "You are a second-opinion advisor. The user is consulting you "
        "because they want an independent perspective. Reason from first "
        "principles. Do not defer to anyone else's framing or proposed "
        "answer. Reach your own conclusion."
    )
    if context.strip():
        parts.append("")
        parts.append("## Context")
        parts.append(_tagged("context", context))
    parts.append("")
    parts.append("## Question")
    parts.append(question.strip())
    parts.append("")
    parts.append("## Output")
    parts.append(
        _with_hint(
            "Reason through the problem. Provide your conclusion as a clear "
            "position with the reasoning behind it. State explicitly what you "
            "would do, what risks you see, and what assumptions you are "
            "making.",
            length_hint,
        )
    )
    return "\n".join(parts)


def build_critique_prompt(draft: str, context: str = "",
                          length_hint: str | None = DEFAULT_LENGTH_HINT) -> str:
    """Build the critique prompt for a council advisor.

    Frames the receiving model as a critical reviewer whose job is to find
    flaws, missing angles, weak assumptions, and unstated risks in ``draft``,
    which is placed in a ``<draft>`` tag. ``context``, if non-empty, is appended
    in a ``<context>`` tag under a ``## Context`` section.
    ``length_hint`` is the closing length instruction; pass ``""`` or ``None``
    for an enumerating task that must not be capped. Returns the full prompt
    string.
    """
    parts = [advisor_block(), ""]
    parts.append("## Your role")
    parts.append(
        "You are a critical reviewer. The user has produced a draft "
        "(proposal, argument, message, decision). Your job is to find "
        "flaws, missing angles, weak assumptions, and unstated risks. "
        "Be direct. Be specific. Disagreement is more valuable than "
        "agreement here."
    )
    parts.append("")
    parts.append("## Draft to critique")
    parts.append(_tagged("draft", draft))
    if context.strip():
        parts.append("")
        parts.append("## Context")
        parts.append(_tagged("context", context))
    parts.append("")
    parts.append("## Output")
    parts.append(
        _with_hint(
            "Identify the strongest objections to this draft. List the "
            "assumptions that, if wrong, would change the conclusion. Name "
            "the angles or evidence that are missing. End with: would you "
            "ship this as-is, or what would you change first?",
            length_hint,
        )
    )
    return "\n".join(parts)
