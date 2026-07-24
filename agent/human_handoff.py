"""
agent/human_handoff.py

The human-in-the-loop node. Reached three ways (see agent/graph.py):
  - the user directly asked for a human agent (escalation_reason="user_requested")
  - clarification_count exceeded MAX_CLARIFICATION_TURNS (escalation_reason="clarification_cap")
  - reasoning_cycles exceeded MAX_REASONING_CYCLES (escalation_reason="reason_cap")

In all three cases we: ask permission -> if yes, collect name/email/note ->
email config.settings.EMAIL_ID a summary -> confirm to the user. If the user
declines, we say so and stop there.

Uses LangGraph's interrupt() rather than manual state-machine fields for the
multi-step Q&A: each interrupt() call pauses graph execution and hands
control back to run_turn(), which returns the question to the caller. On the
next call, run_turn() resumes the graph with Command(resume=<user's answer>);
LangGraph replays this node from the top, but every interrupt() call that
already has an answer returns it immediately instead of pausing again, so
execution picks up exactly where it left off. This keeps the whole multi-turn
exchange as ordinary top-to-bottom code instead of several separate nodes
threading state manually.
"""

import logging
import smtplib
from email.message import EmailMessage

from langgraph.types import interrupt
from langchain_core.messages import AIMessage

from agent.state import AgentState
from agent.pii import unmask_pii
from config.settings import (
    EMAIL_ID,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_PASSWORD,
    SMTP_USE_TLS,
    SMTP_FROM_ADDRESS,
)

logger = logging.getLogger(__name__)

# NOTE: same caveat as scope_guard.py -- these live here rather than in
# config/prompts.py only because that file wasn't available to edit.
CONSENT_PROMPTS = {
    "user_requested": (
        "Of course -- I can connect you with a compliance agent. "
        "Would you like me to go ahead and pass this along? (yes/no)"
    ),
    "clarification_cap": (
        "I've asked a few clarifying questions but I'm still missing details I'd need "
        "to give you a confident answer. Would you like me to connect you with a "
        "compliance agent who can help further? (yes/no)"
    ),
    "reason_cap": (
        "I've done as much analysis as I can here, and there's still more nuance to work "
        "through. Would you like me to pass this along to a compliance agent for a "
        "closer look? (yes/no)"
    ),
}
_DEFAULT_CONSENT_PROMPT = CONSENT_PROMPTS["user_requested"]

_YES_WORDS = {"y", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "please do", "go ahead", "affirmative"}
_NO_WORDS = {"n", "no", "nope", "nah", "not now", "no thanks", "negative", "don't", "do not"}


def _interpret_yes_no(text: str) -> bool | None:
    normalized = text.strip().lower().rstrip(".!")
    if normalized in _YES_WORDS or any(normalized.startswith(w) for w in _YES_WORDS):
        return True
    if normalized in _NO_WORDS or any(normalized.startswith(w) for w in _NO_WORDS):
        return False
    return None


def send_handoff_email(situation_summary: str, name: str, email: str, note: str | None) -> bool:
    """Best-effort send; returns False (and logs) instead of raising, so a
    misconfigured mail server doesn't crash the whole graph turn -- the node
    still tells the user honestly whether it worked."""
    if not SMTP_HOST:
        logger.error("Cannot send handoff email: SMTP_HOST is not configured.")
        return False

    msg = EmailMessage()
    msg["Subject"] = f"FINRA Compliance Assistant -- handoff request from {name}"
    msg["From"] = SMTP_FROM_ADDRESS or SMTP_USERNAME or "no-reply@localhost"
    msg["To"] = EMAIL_ID
    msg["Reply-To"] = email
    msg.set_content(
        "A user has requested to speak with a compliance agent.\n\n"
        f"Name: {name}\n"
        f"Email: {email}\n\n"
        f"Situation summary:\n{situation_summary}\n\n"
        f"Additional note:\n{note or '(none provided)'}\n"
    )

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USERNAME and SMTP_PASSWORD:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception:
        logger.exception("Failed to send compliance handoff email.")
        return False


def human_handoff_node(state: AgentState) -> dict:
    reason = state.get("escalation_reason") or "user_requested"
    consent_prompt = CONSENT_PROMPTS.get(reason, _DEFAULT_CONSENT_PROMPT)

    consent: bool | None = None
    for attempt in range(3):
        question = consent_prompt if attempt == 0 else (
            "Sorry, just to confirm with a yes or no -- would you like me to "
            "connect you with a compliance agent?"
        )
        answer = interrupt({"stage": "consent", "question": question})
        consent = _interpret_yes_no(answer)
        if consent is not None:
            break

    if not consent:
        decline_message = (
            "No problem -- I'll keep helping here. Let me know if you'd like to "
            "add more detail, or if you change your mind about reaching an agent."
        )
        updates: dict = {
            "final_answer": decline_message,
            "messages": [AIMessage(content=decline_message)],
            "escalation_reason": None,
        }
        # Give the agent a fresh budget rather than re-offering the handoff on
        # every subsequent turn -- only reset whichever counter actually
        # caused this escalation (a "user_requested" handoff wasn't
        # cap-triggered, so neither counter should be touched).
        if reason == "clarification_cap":
            updates["clarification_count"] = 0
        elif reason == "reason_cap":
            updates["reasoning_cycles"] = 0
        return updates

    name = interrupt({"stage": "name", "question": "Great -- could you share your name?"})
    email = interrupt({
        "stage": "email",
        "question": "And what email address should the compliance agent use to reach you?",
    })
    note_raw = interrupt({
        "stage": "note",
        "question": "Is there anything else you'd like to add for the agent? (Say 'no' or 'none' to skip.)",
    })

    note = None if note_raw.strip().lower() in {"", "no", "none", "n/a", "na", "skip"} else note_raw.strip()

    # The summary was built from masked text (it never held raw PII to begin
    # with) -- unmask it here so the actual compliance agent receiving the
    # email sees the real situation, not placeholder tokens.
    summary_for_email = unmask_pii(
        state.get("situation_summary") or state.get("raw_query") or "",
        state.get("pii_map", {}),
    )

    sent_ok = send_handoff_email(
        situation_summary=summary_for_email,
        name=name.strip(),
        email=email.strip(),
        note=note,
    )

    if sent_ok:
        confirmation = (
            f"Thanks, {name.strip()} -- I've sent your situation summary to our compliance "
            f"team and they'll follow up at {email.strip()} shortly."
        )
    else:
        confirmation = (
            "I tried to send this to our compliance team but ran into an issue on my end "
            "sending the email. Please reach out to them directly in the meantime, and "
            "apologies for the inconvenience."
        )

    return {
        "final_answer": confirmation,
        "messages": [AIMessage(content=confirmation)],
        "handoff_name": name.strip(),
        "handoff_email": email.strip(),
        "handoff_note": note,
        "handoff_sent": sent_ok,
    }