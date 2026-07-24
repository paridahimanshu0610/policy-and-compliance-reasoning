"""
agent/scope_guard.py

Runs first, on every single turn (before intake), and decides one of three
things:
  1. The user is directly asking to be connected with a human/compliance
     agent -- skip the whole reasoning pipeline and go straight to
     human_handoff_node.
  2. The message isn't a FINRA-compliance question at all (weather, booking
     travel, general chit-chat, ...) -- don't enter the main reasoning flow;
     redirect politely instead.
  3. Otherwise, it's in scope -- proceed to intake as normal.

Kept as its own small, cheap LLM call (not folded into intake) so a
completely off-topic message never even reaches the fact-extraction /
retrieval machinery, and so "wants a human" is checked independently of
whatever else is going on in the conversation.
"""

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from agent.state import AgentState
from agent.llm import get_chat_model

SCOPE_GUARD_SYSTEM_PROMPT = """You are the scope gate for a FINRA compliance assistant. \
This assistant exists ONLY to help someone understand which FINRA rule or \
clause applies to a specific situation they're dealing with (e.g. as a \
broker-dealer employee, compliance officer, or registered representative).

Given the ongoing conversation and the user's latest message, decide two \
independent things:

1. in_scope: Is this latest message part of a genuine attempt to describe, \
   ask about, or follow up on a FINRA compliance situation? This includes \
   follow-up answers to clarifying questions (like "yes", "$500", or a firm \
   type), even if the message alone looks unrelated to FINRA -- check it \
   against the ongoing situation summary. Mark False only for messages that \
   are clearly unrelated to any compliance situation: small talk, weather, \
   booking travel, general knowledge questions, requests unrelated to FINRA \
   rules, etc.

2. wants_human_agent: Is the user, in this latest message, directly and \
   explicitly asking to be connected with, transferred to, or contacted by \
   a human being / compliance agent / representative / real person right \
   now? Only mark this True for a clear, direct request -- not for \
   frustration or a rhetorical remark.

A message can be in_scope=True and wants_human_agent=True at the same time \
(e.g. "can I just talk to a real person about this FINRA issue instead").
"""


class ScopeAssessment(BaseModel):
    in_scope: bool = Field(
        description="True if this message is part of a genuine FINRA compliance question/situation."
    )
    wants_human_agent: bool = Field(
        description="True if the user is directly asking to be connected with a human compliance agent right now."
    )


OUT_OF_SCOPE_MESSAGE = (
    "I'm built specifically to help you work through FINRA compliance questions -- "
    "figuring out which rule or clause applies to a situation you're dealing with. "
    "That's outside what I can help with here. If you'd like, tell me more about the "
    "compliance situation you're navigating and I'll help you find the FINRA clause "
    "that applies to it."
)


def scope_gate_node(state: AgentState) -> dict:
    llm = get_chat_model("scope_guard").with_structured_output(ScopeAssessment)

    context = (
        f"Ongoing situation summary so far (empty if this is the first message): "
        f"{state.get('situation_summary') or '(none yet)'}\n\n"
        f"User's latest message: {state['raw_query']}"
    )
    result = llm.invoke([
        SystemMessage(content=SCOPE_GUARD_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    updates: dict = {
        "in_scope": result.in_scope,
        "wants_human_agent": result.wants_human_agent,
    }
    if result.wants_human_agent:
        updates["escalation_reason"] = "user_requested"
    return updates


def out_of_scope_node(state: AgentState) -> dict:
    """Redirect politely -- does NOT touch known_fields/situation_summary/
    clarification_count/etc, since this message wasn't actually part of the
    compliance situation and shouldn't be treated as progress on it."""
    return {
        "final_answer": OUT_OF_SCOPE_MESSAGE,
        "messages": [AIMessage(content=OUT_OF_SCOPE_MESSAGE)],
    }
