"""Cross-vendor LLM fallback for long-running daemons.

Track A of the LLM-fit logging project (the `plans` archive, 2026-05-24-llm-fit-logging-
three-tracks.md). When Anthropic API errors out on a retriable failure
(connection reset, timeout, 429, 5xx), this module cascades the same prompt
through a configured fallback chain (Kimi / ChatGPT via the existing
council wrappers) so the daemon keeps producing output instead of going
silent for the duration of the outage. Only the vendors in
``TRUSTED_FALLBACK_VENDORS`` are ever called: the prompts carry ceo-only
mail and calendar content.

Permanent errors (auth, bad request) are not retried - the next vendor
would reject the same payload for the same reason.

Tool-use is NOT supported on the fallback path. None of the current Track A
targets (sentinel, email-intelligence) use tools, but if
a future caller does, set ``allow_fallback_for_tool_use=False`` (default) and
the wrapper will re-raise the original Anthropic error rather than silently
drop the tool definitions.

Cross-platform by construction: imports the kimi-consult.py /
chatgpt-consult.py wrappers as Python libraries (no subprocess / no shell).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import yaml

from scripts.utils import claude_models
from scripts.utils.sensitive import sensitivity_is_declared
from scripts.utils.workspace import get_workspace_root

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = get_workspace_root()
CONFIG_PATH = WORKSPACE_ROOT / "config" / "llm_fallback.yaml"

_config_cache: dict | None = None


@dataclass
class LLMResult:
    """Normalised return shape for fallback-aware LLM calls.

    Callers that previously did ``response.content[0].text.strip()`` should
    now do ``result.text``. Vendor + model_id + fallback_triggered let the
    caller make any vendor-specific post-processing decisions (or just log
    them).
    """
    text: str
    vendor: str           # "anthropic" | "kimi" | "chatgpt"
    model_id: str
    fallback_triggered: bool
    primary_error: str | None = None
    raw_anthropic_response: Any | None = None  # populated only when vendor == "anthropic"
    fallback_attempts: list[dict] = field(default_factory=list)


class ClaudeStopped(RuntimeError):
    """Anthropic answered, but the answer is not one to read as text.

    `stop_reason` is `refusal` (a safety classifier declined; `category` names
    which) or `max_tokens` (the cap was spent, usually on thinking, before the
    reply was finished). Both used to reach the caller as ordinary text: an
    empty or half-written string that failed to parse and was replaced by a
    placeholder, so sentinel scored the message 5 with "LLM response could not
    be parsed" and nobody could tell a refusal from a cut-off from a model that
    wrote bad JSON. This is raised instead and never cascades to another
    vendor: neither case is an outage, and a prompt one vendor refused is not
    one to hand to the next.
    """

    def __init__(self, stop_reason: str, model: str, category: str | None = None):
        self.stop_reason = stop_reason
        self.model = model
        self.category = category
        detail = f" (category {category})" if category else ""
        super().__init__(f"anthropic {model} stopped with stop_reason={stop_reason}{detail}")


def _load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"llm_fallback config missing at {CONFIG_PATH}. "
            "Restore it from git."
        )
    _config_cache = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return _config_cache


def _tier_for_model(model: str) -> str:
    """Substring match on the Anthropic model string -> tier name.

    The tier names are `claude_models.FAMILIES`, read rather than restated. Until
    2026-09-27 this function held its own list of three and the resolver held
    four, so a call made with a `fable` model reached the cascade and raised
    ValueError here instead of falling back. A family added to the resolver now
    arrives here with it, and `config/llm_fallback.yaml` needs its chain.
    Substring rather than prefix, so a provider-prefixed id still resolves.
    """
    m = model.lower()
    for family in claude_models.FAMILIES:
        if family in m:
            return family
    raise ValueError(f"cannot derive tier from model name: {model!r}")


def _flatten_to_prompt(system: Any, messages: list[dict]) -> str:
    """Collapse Anthropic-shape system+messages into a single plaintext prompt.

    Anthropic accepts ``system`` as either a string or a list of blocks
    (each ``{"type": "text", "text": "...", "cache_control": {...}}``). The
    fallback wrappers take a single string. We concatenate in a way that
    preserves the semantic structure - the fallback model has no concept of
    cache_control or system separation, but it does respond well to a plain
    "SYSTEM:\n... USER:\n..." layout.

    Skips non-text blocks defensively; if the caller is sending image blocks
    we have nothing meaningful to send to a text-only fallback.
    """
    parts: list[str] = []
    if isinstance(system, str) and system.strip():
        parts.append(f"SYSTEM:\n{system.strip()}")
    elif isinstance(system, list):
        text_chunks = [
            blk["text"] for blk in system
            if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text")
        ]
        if text_chunks:
            parts.append("SYSTEM:\n" + "\n\n".join(c.strip() for c in text_chunks))
    for msg in messages:
        role = (msg.get("role") or "").upper() or "USER"
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(f"{role}:\n{content.strip()}")
        elif isinstance(content, list):
            text_chunks = [
                blk["text"] for blk in content
                if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text")
            ]
            if text_chunks:
                parts.append(f"{role}:\n" + "\n\n".join(c.strip() for c in text_chunks))
    return "\n\n".join(parts)


def _is_retriable_anthropic_error(exc: Exception) -> bool:
    """True iff the exception class matches the retriable list in config."""
    retriable_names = set(_load_config().get("retriable_anthropic_errors", []))
    cls_chain = {c.__name__ for c in type(exc).__mro__}
    return bool(retriable_names & cls_chain)


# Track B: objective signals that a cheaper tier could have sufficed.
# NEVER asks Claude its opinion (self-preference bias). Only inspects the
# response: token count, tool use presence, stop reason. The aggregation
# script (scripts/llm-fit-report.py) buckets traces by skill and reports
# what fraction were flagged - acting on the data is a SEPARATE later
# decision, not part of Phase 2.

_DOWNGRADE_OUTPUT_TOKEN_THRESHOLD = 500


def _compute_downgrade_signal(raw_response: Any, model_used: str) -> dict | None:
    """Inspect an Anthropic response for objective downgrade signals.

    Returns None when the response shape doesn't match (Kimi/ChatGPT
    fallback path - their wrappers return plain strings, not Message
    objects). Returns a dict of signals + a derived ``downgrade_candidate``
    flag otherwise.
    """
    try:
        output_tokens = getattr(raw_response.usage, "output_tokens", None)
        stop_reason = getattr(raw_response, "stop_reason", None)
        has_tool_use = any(
            getattr(b, "type", None) == "tool_use" for b in (raw_response.content or [])
        )
    except Exception:
        return None
    if output_tokens is None:
        return None
    # Haiku-tier calls are already cheap; flagging them as "downgrade
    # candidate" is noise. Only flag Sonnet/Opus calls that produced a
    # short, single-shot, normal-stop response.
    is_already_cheap = "haiku" in (model_used or "").lower()
    downgrade_candidate = (
        not is_already_cheap
        and output_tokens < _DOWNGRADE_OUTPUT_TOKEN_THRESHOLD
        and not has_tool_use
        and stop_reason == "end_turn"
    )
    return {
        "output_tokens": output_tokens,
        "stop_reason": stop_reason,
        "has_tool_use": has_tool_use,
        "downgrade_candidate": downgrade_candidate,
    }


def _tag_langfuse(result: LLMResult, skill_name: str) -> None:
    """Best-effort: tag the current Langfuse trace with vendor + fallback +
    downgrade signals.

    Silent on any failure - observability stuttering must never crash a
    daemon tick.
    """
    try:
        from langfuse import get_client  # type: ignore[import-not-found]
        tags = [
            f"vendor:{result.vendor}",
            f"model:{result.model_id}",
            f"skill:{skill_name}",
        ]
        if result.fallback_triggered:
            tags.append("fallback_triggered")
        metadata: dict = {
            "fallback_triggered": result.fallback_triggered,
            "primary_error": result.primary_error,
            "fallback_attempts": result.fallback_attempts,
        }
        # Track B downgrade-audit: only computable when Anthropic served the
        # response (the fallback wrappers return plain text, no token usage
        # or stop_reason).
        if result.vendor == "anthropic" and result.raw_anthropic_response is not None:
            signals = _compute_downgrade_signal(result.raw_anthropic_response, result.model_id)
            if signals is not None:
                metadata["downgrade_signals"] = signals
                if signals["downgrade_candidate"]:
                    tags.append("downgrade_candidate")
        get_client().update_current_trace(tags=tags, metadata=metadata)
    except Exception as exc:  # noqa: BLE001 - best-effort Langfuse tagging; must never break the LLM call
        logger.debug("llm_fallback: Langfuse tagging failed: %s", exc)


def call_anthropic_with_fallback(
    client: Any,
    *,
    model: str,
    max_tokens: int,
    system: Any,
    messages: list[dict],
    effort: str,
    skill_name: str = "unknown",
    temperature: float | None = None,
    output_schema: dict | None = None,
    allow_fallback_for_tool_use: bool = False,
    **anthropic_kwargs: Any,
) -> LLMResult:
    """Try Anthropic; on retriable failure cascade through the configured chain.

    Drop-in replacement for ``client.messages.create(...)`` for the
    text-in-text-out case. Returns an :class:`LLMResult` so callers can
    distinguish "served by Anthropic" from "served by fallback" without
    sniffing the response object.

    Tool calls: if ``anthropic_kwargs`` contains ``tools`` or ``tool_choice``
    and a fallback would be needed, this raises the Anthropic error
    unchanged unless ``allow_fallback_for_tool_use=True`` (which silently
    drops tool definitions on the fallback path - dangerous, opt-in only).

    Model-dependent parameters come from ``claude_models.request_params``:
    ``max_tokens`` is the size of the visible reply and is raised on a model
    that thinks; ``effort`` is required, so every caller decides it, and is
    sent only where the model accepts it; ``temperature`` is sent to Anthropic
    only where it does not raise a 400, and still reaches a fallback vendor.
    ``output_schema`` asks Anthropic for structured output; the fallback path
    has no schema, so the prompt must still ask for the same JSON in words and
    the caller keeps its text parser for that path.

    A response whose ``stop_reason`` is ``refusal`` or ``max_tokens`` raises
    :class:`ClaudeStopped` rather than returning its text.
    """
    has_tools = "tools" in anthropic_kwargs or "tool_choice" in anthropic_kwargs
    primary_error: str | None = None
    raw_response: Any = None
    attempts: list[dict] = []

    # ---- Primary: Anthropic ----
    try:
        raw_response = client.messages.create(
            model=model,
            system=system,
            messages=messages,
            **claude_models.request_params(
                model, max_tokens=max_tokens, effort=effort,
                temperature=temperature, output_schema=output_schema,
            ),
            **anthropic_kwargs,
        )
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"
        if not _is_retriable_anthropic_error(exc):
            raise
        if has_tools and not allow_fallback_for_tool_use:
            logging.warning(
                "llm_fallback: anthropic %s but caller uses tools; "
                "re-raising rather than dropping tool definitions on fallback path. "
                "skill=%s err=%s",
                type(exc).__name__, skill_name, primary_error,
            )
            raise
    else:
        stop_reason = getattr(raw_response, "stop_reason", None)
        if stop_reason in ("refusal", "max_tokens"):
            details = getattr(raw_response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            logging.warning(
                "llm_fallback: anthropic stop_reason=%s skill=%s model=%s category=%s",
                stop_reason, skill_name, model, category,
            )
            raise ClaudeStopped(stop_reason, model, category)
        text = "".join(
            b.text for b in raw_response.content if getattr(b, "type", None) == "text"
        ).strip()
        result = LLMResult(
            text=text,
            vendor="anthropic",
            model_id=model,
            fallback_triggered=False,
            raw_anthropic_response=raw_response,
        )
        _tag_langfuse(result, skill_name)
        return result

    # ---- Cascade ----
    # A declared-sensitive session never reaches a second vendor. The prompts
    # this module carries are daemon prompts - sentinel and email-intelligence
    # are named Track A targets in the docstring above - so a cascade ships email
    # bodies and calendar subjects to a second vendor. That the chain exists
    # at all is a deliberate, configured arrangement and stays; what was missing
    # until 2026-08-23 was the case where the operator has already said otherwise.
    #
    # `sensitivity_is_declared()`, NOT `is_sensitive()`. The latter is fail-closed
    # and answers True whenever the variable was never set, which is every daemon
    # host by default - gating on it would silently kill the whole failover chain
    # in production, turning an Anthropic outage into a total outage. A
    # DECLARATION is a different fact: a person typed it, and it knows something
    # no denylist does.
    if sensitivity_is_declared():
        logging.warning(
            "llm_fallback: SENSITIVE_MODE is declared; refusing to cascade "
            "skill=%s err=%s", skill_name, primary_error,
        )
        raise RuntimeError(
            f"anthropic failed ({primary_error}) and SENSITIVE_MODE is declared, "
            f"so this prompt was not sent to another vendor"
        )

    tier = _tier_for_model(model)
    chain = (_load_config().get("tiers", {}).get(tier, {}) or {}).get("chain", [])
    if not chain:
        logging.error(
            "llm_fallback: no fallback chain configured for tier %s; re-raising. "
            "skill=%s err=%s", tier, skill_name, primary_error,
        )
        # Re-raise the original error since we have nothing to fall back to.
        # The except above already captured it; re-raise by reconstructing a generic
        # RuntimeError preserving the message (the original exc is out of scope).
        raise RuntimeError(
            f"anthropic {tier} failed ({primary_error}) and no fallback chain configured"
        )

    flattened_prompt = _flatten_to_prompt(system, messages)
    for step in chain:
        vendor = step.get("vendor")
        model_id = step.get("model")
        try:
            text = _invoke_vendor(vendor, model_id, flattened_prompt, max_tokens,
                                  1.0 if temperature is None else temperature)
            attempts.append({"vendor": vendor, "model": model_id, "ok": True})
            logging.warning(
                "llm_fallback: anthropic->%s for tier=%s skill=%s reason=%s",
                vendor, tier, skill_name, primary_error,
            )
            result = LLMResult(
                text=text,
                vendor=vendor,
                model_id=model_id,
                fallback_triggered=True,
                primary_error=primary_error,
                fallback_attempts=attempts,
            )
            _tag_langfuse(result, skill_name)
            return result
        except Exception as fallback_exc:
            attempts.append({
                "vendor": vendor,
                "model": model_id,
                "ok": False,
                "error": f"{type(fallback_exc).__name__}: {fallback_exc}",
            })
            logging.warning(
                "llm_fallback: fallback %s/%s failed (%s); continuing chain",
                vendor, model_id, fallback_exc,
            )
            continue

    # All fallbacks exhausted. Surface a clear error citing everything tried.
    chain_summary = "; ".join(
        f"{a['vendor']}/{a['model']}={a.get('error', 'ok')}" for a in attempts
    )
    raise RuntimeError(
        f"llm_fallback: anthropic failed ({primary_error}) and all "
        f"{len(chain)} fallbacks exhausted. Attempts: {chain_summary}"
    )


_vendor_fn_cache: dict[str, Any] = {}


def _load_consult_fn(script_relpath: str, fn_name: str) -> Any:
    """Load a function from a kebab-case Python script via importlib.

    The council wrappers ship as ``scripts/kimi-consult.py`` and
    ``scripts/chatgpt-consult.py`` (kebab-case, CLI convention). Python cannot
    ``import`` hyphenated module names, so we register them via
    spec_from_file_location. Cached to avoid repeating the spec dance on
    every fallback. Renaming the wrappers would break the /council SKILL.md
    subprocess invocations and the skill's documented CLI contract.
    """
    cache_key = f"{script_relpath}::{fn_name}"
    if cache_key in _vendor_fn_cache:
        return _vendor_fn_cache[cache_key]
    import importlib.util
    script_path = WORKSPACE_ROOT / script_relpath
    spec = importlib.util.spec_from_file_location(
        script_relpath.replace("/", "_").replace("-", "_").removesuffix(".py"),
        script_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"llm_fallback: cannot load {script_relpath}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, fn_name, None)
    if fn is None:
        raise RuntimeError(f"llm_fallback: {script_relpath} has no {fn_name}()")
    _vendor_fn_cache[cache_key] = fn
    return fn


# vendor -> (wrapper script, the function inside it). A TABLE, not a chain of
# `if`s: the three branches this replaced were the same four lines three times
# over, and the shape charges for itself the moment a vendor is added. It was
# added on 2026-09-11 and the fourth branch did not get written, so `chatgpt`
# was a vendor `config/llm_fallback.yaml` could name and `_invoke_vendor` would
# reject with `unknown fallback vendor` in the middle of a failover, which is the
# one moment the primary is already down.
#
# Every wrapper here takes the same keyword signature and routes through
# `scripts/utils/proxy_transport.call_model`, so the table is complete on its own
# and a new entry needs no code below it.
VENDOR_WRAPPERS = {
    "kimi": ("scripts/kimi-consult.py", "consult_kimi"),
    "chatgpt": ("scripts/chatgpt-consult.py", "consult_chatgpt"),
}

# The vendors a fallback prompt may reach, and the only ones. The prompts are
# ceo-only (email bodies, calendar subjects), and the operator's named remote
# trust list admits Moonshot Kimi and OpenAI ChatGPT Astra and no other remote
# model. Until 2026-10-12 the haiku chain was Gemini then Grok and nothing in
# code refused them; the yaml was the only thing deciding where a prompt went.
# This set is checked before every vendor call, separately from
# VENDOR_WRAPPERS, so re-adding a wrapper or a yaml step is not enough to send
# anything: the refusal is logged and the chain moves on.
TRUSTED_FALLBACK_VENDORS = frozenset({"kimi", "chatgpt"})


class UntrustedVendor(ValueError):
    """A fallback step named a vendor outside TRUSTED_FALLBACK_VENDORS."""


def _invoke_vendor(vendor: str, model: str, prompt: str, max_tokens: int, temperature: float) -> str:
    """Dispatch to the matching council wrapper (library-mode, no subprocess).

    Refuses, before anything is loaded or sent, a vendor outside
    TRUSTED_FALLBACK_VENDORS.
    """
    if vendor not in TRUSTED_FALLBACK_VENDORS:
        logger.error(
            "llm_fallback: REFUSED untrusted fallback vendor %r (model %r); "
            "nothing was sent. Trusted: %s",
            vendor, model, ", ".join(sorted(TRUSTED_FALLBACK_VENDORS)),
        )
        raise UntrustedVendor(f"untrusted fallback vendor refused: {vendor!r}")
    wrapper = VENDOR_WRAPPERS.get(vendor)
    if wrapper is None:
        raise ValueError(
            f"unknown fallback vendor: {vendor!r}. "
            f"Known: {', '.join(sorted(VENDOR_WRAPPERS))}"
        )
    consult = _load_consult_fn(*wrapper)
    # `reasoning_effort` is deliberately not passed. A failover step wants an
    # answer at the lowest latency the vendor offers, and the chain is already
    # running because the primary failed; the same reasoning pins the kimi step
    # to `kimi-for-coding` rather than to k3.
    return consult(prompt, model=model, temperature=temperature, max_tokens=max_tokens)
