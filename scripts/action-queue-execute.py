#!/usr/bin/env python3
"""Action-Queue send executor: ``send_card``, the one function that can send.

``scripts/action-queue.py approve`` imports ``send_card`` and calls it for the
one card the CEO approved; that synchronous terminal approve is the sole send
path. ``send_card`` sends through ``scripts/send-email.py`` (which loads the
Exchange creds from ``.env`` in that child process and auto-logs the send to
CRM) and returns a result dict. It never writes ``queue.json``; the caller
applies the status change under the queue lock.

Failure classification. ``send-email.py`` exits 1 for both connection blips and
permanent config / recipient errors with no distinct codes, so:

- pre-send empty recipient / empty body (detected before spawning) -> permanent.
- ``TimeoutExpired`` and a residual non-zero exit -> transient.

The approve caller stamps its own classification (None, no dead-letter entry)
and the CEO retries by hand.

Not a CLI: there is no batch entry point. The batch executor and its retry
backoff were removed with the bridge daemon that spawned them.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import recipient, tool_risk
from scripts.utils.venv_guard import venv_python

SEND_TIMEOUT_S = 120


def send_card(engine_root: Path, card: dict) -> dict:
    """Send ONE card via send-email.py and return a result dict, for the
    synchronous terminal approve (scripts/action-queue.py).

    ``engine_root`` is the ENGINE workspace root - it locates
    ``scripts/send-email.py`` (an engine path) and is the subprocess cwd. This
    function NEVER touches queue.json; the caller resolves the queue store
    separately (under the DATA root) and applies the status transition.

    Result shapes:
      {result: "sent",     classification: "sent",      attempt}
      {result: "send_failed", classification: transient|permanent, error, attempt}
      {result: "refused",  classification: "none", error}  (not gated - never send)
      {result: "skipped",  classification: "none", error}  (not a send type)
    The caller stamps its own classification on the queue (M2: None, no
    auto-DLQ).
    """
    aid = card.get("id")
    action_type = card.get("action_type")
    # Derived up here, not further down, because the first branch that returns
    # a send_failed result is the telegram one below.
    attempt = card.get("attempt") or 0
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        attempt = 0
    # THE GATE, and it is UNCONDITIONAL: it runs for every card that reaches
    # this function, above every branch on `action_type`. This function is the
    # only code in the tree that can send anything, so nothing below can send
    # without `tier_for` having answered `gated` for the card in hand.
    #
    # It used to sit BELOW the two type branches, inside the region keyed on the
    # literal `email_send`. A send-capable type registered in the ledger but
    # missing from those branches returned `skipped` before ever reaching it:
    # the guard was skipped, not failed. The exposure was latent (no executor
    # exists for any other type) and would have opened the day one was added,
    # because a new branch goes where the others are - above the old check.
    #
    # An emptied or tampered `send_capable` cannot switch this off. `tier_for`
    # is total over action_types and answers `gated` for one it does not know,
    # so the condition below is evaluated for every card whatever the ledger
    # says. `send_capable_types()` is consulted only to tell the two refusals
    # apart - never to decide whether to check.
    tier = tool_risk.tier_for(action_type)
    if tier != tool_risk.GATED:
        if action_type in tool_risk.send_capable_types():
            # The ledger calls it a sender and the resolver disagrees. Only a
            # tampered resolver can produce this; refuse loudly.
            return {"action_id": aid, "result": "refused",
                    "error": f"{action_type} does not resolve gated - refusing to send",
                    "classification": "none"}
        # Positively classified as a non-send type (autonomous / notify).
        return {"action_id": aid, "result": "skipped",
                "error": f"not a send type ({action_type})", "classification": "none"}
    # telegram_send is reserved-and-gated but has no executor yet (F-L6): explicit
    # 501, never a silent skip. A gated send that cannot send.
    if action_type == "telegram_send":
        # `attempt` too: every other send_failed path carries it, and the
        # docstring's result shape promises it, so a caller persisting
        # res["attempt"] uniformly hit KeyError on this one branch.
        return {"action_id": aid, "result": "send_failed",
                "error": "telegram executor not implemented (501)",
                "classification": "permanent", "attempt": attempt}
    if action_type != "email_send":
        # Gated, but no executor here knows how to send it - an unclassified
        # type, or a send type registered before its executor landed.
        return {"action_id": aid, "result": "skipped",
                "error": f"no executor for send type ({action_type})",
                "classification": "none"}

    to = (card.get("to") or "").strip()
    subject = card.get("subject") or ""
    body = card.get("draft_body") or ""
    if not to or not body:
        return {"action_id": aid, "result": "send_failed",
                "error": "draft not written (fill the body with `action-queue.py edit --body-file`)",
                "classification": "permanent", "attempt": attempt}
    # THE RECIPIENT GATE: a card may be deposited before a real recipient is
    # known, and this refuses a placeholder address (added 2026-09-02).
    # It sits HERE, in the one function that can send. `refused` and not
    # `send_failed`: nothing was attempted, so the caller puts
    # its claim back and the card stays approvable once the address is fixed.
    # Recording a send_failure would offer `retry`, which would repeat a send
    # that never ran and cannot succeed.
    refusal = recipient.refusal_reason(to)
    if refusal:
        return {"action_id": aid, "result": "refused",
                "error": (f"refusing to send: {refusal}. Correct it with "
                          f"`action-queue.py edit {aid} --to <address>`"),
                "classification": "none"}
    send_script = engine_root / "scripts" / "send-email.py"
    # The body goes on STDIN, never in argv. Two reasons, both measured
    # 2026-08-23. An argv element is readable by any local account via `ps` for
    # the up-to-120-second life of the send, and outbound CRM content is the
    # most commercially sensitive text here. And Linux caps one argument at
    # MAX_ARG_STRLEN = 131072 bytes: a 100,000-byte body spawned, 131,072 raised
    # OSError [Errno 7]. A test held the stdin route until the 2026-09-21
    # test-suite split; nothing does now, so moving the body back into argv
    # would go unnoticed by everything except a reader of this comment.
    cmd = [str(venv_python()), str(send_script), "--to", to, "--subject", subject,
           "--body-stdin"]
    try:
        p = subprocess.run(cmd, cwd=str(engine_root), capture_output=True,
                           text=True, timeout=SEND_TIMEOUT_S, input=body)
    except subprocess.TimeoutExpired:
        return _transient_result(aid, "send timed out", attempt)
    except OSError as exc:
        # A spawn can still fail for reasons this code does not control:
        # ENOMEM, EMFILE, a missing interpreter. Returning a result keeps the
        # caller on the recorded path; raising made it a raw traceback out of
        # `action-queue.py approve`, the CEO's own send command.
        return _transient_result(aid, f"could not start the sender: {exc}", attempt)
    if p.returncode == 0:
        return {"action_id": aid, "result": "sent", "classification": "sent", "attempt": attempt}
    error = (p.stderr or p.stdout or "send failed")[-300:].strip()
    return _transient_result(aid, error, attempt)


def _transient_result(aid, error: str, attempt: int) -> dict:
    """Build a send_failed result for a default-transient failure."""
    return {"action_id": aid, "result": "send_failed", "error": error,
            "classification": "transient", "attempt": attempt + 1}
