# Lethal-Trifecta Control - Outbound Send Is Always Human-Gated

Last Updated: 2026-10-29
Last Verified: 2026-08-20

Always-active security rule. An agent that holds private data (leg 1), reads untrusted content (leg 2) and can send externally (leg 3) can be steered into exfiltrating the first through the third, so leg 3 stays permanently human-gated: anything that can send to the outside world is ALWAYS gated behind an explicit human approval and NEVER sent autonomously. One bounded exception follows: self-notification.

Threat model, and why legs 1 and 2 cannot be removed: `SECURITY-MODEL.md` § 1 under registry name `spec-docs`. Mechanical enforcement (the `send_capable -> gated` floor, the unclassified-type fail-safe, their tests): `.claude/rules/tiered-risk.md` and `scripts/utils/tool_risk.py`.

## The control (non-negotiable)

Outside the one exception, every outbound send is drafted, never auto-sent:

- The Action Queue routes any send-capable card to the `gated` tier. The send is SYNCHRONOUS and terminal-native: the CEO's typed `scripts/action-queue.py approve <id>` IS the explicit human approve click, and the send happens in that same command; there is no autonomous background send. `send_card` still refuses anything that does not resolve `gated`.
- A skill or daemon that produces an outbound message produces a **draft** for review; it never calls the send transport itself as an autonomous step.
- New automation that gains a send capability inherits this control: add any new outbound-capable `action_type` to `send_capable` in `config/tool-risk.json` so it floors at `gated`. Forgetting fails safe: an unclassified type resolves `gated`.

## The first exception: self-notification

A notification to the operator's OWN sink is not a leg-3 send (leg 3 is reach to a third party), so the six timer-driven notifiers and the checkpoint hook fire without a click.

`scripts/utils/telegram_notify.py` resolves an allowlist of the operator's own sinks from the ENVIRONMENT (pinned by `HEADING_OS_SELF_TELEGRAM_TARGET`, else the per-feature `*_TELEGRAM_TARGET` set) and REFUSES any other recipient with a logged `REFUSED` line and no send. Absent, blank or unrecognised configuration refuses; it never falls back to sending somewhere.

That closes a recipient a caller can produce WITHOUT touching the environment, the shape a prompt-injected instruction reaches for: a literal in a caller, a value derived from fetched content, an argument handed in by a skill. It does NOT close a value the running process assigned to one of those names: the read is `os.environ`, not the `.env` FILE, so such a value is indistinguishable from one the operator typed (measured 2026-09-01). Before changing how that allowlist is resolved, read § What the self-notification boundary does NOT close in `reference/lethal-trifecta-detail.md`; do not "harden" it to a file read without settling what it names.

Nothing may widen that transport to carry a recipient that arrived through the running process rather than from the operator's own configuration: a rule for whoever edits this next, not a property the code checks. No other send path inherits this exception.

Advisory layers (e.g. the R5b pre-approval critique, `scripts/utils/draft_critique.py`) may annotate a queued draft but never approve, dismiss or send it; the mandatory human click is the only path from draft to sent.
