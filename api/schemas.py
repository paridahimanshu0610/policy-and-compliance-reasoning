"""
api/schemas.py

Pydantic models for the FastAPI layer. These are deliberately separate from
agent/state.py -- the agent's internal state shape is free to evolve without
breaking the API contract, and vice versa.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="The user's message for this turn.")
    thread_id: Optional[str] = Field(
        None,
        description="Conversation id. Omit on the first message of a new "
        "conversation -- the server will generate one and return it.",
    )


class ClauseCitation(BaseModel):
    """One entry from run_turn()'s `trace` list, reshaped for display.

    Only clause_ref + rule_url are surfaced to the frontend (relevance_role /
    reasoning still flow through run_turn()'s trace but aren't shown in the
    UI). NOTE: `rule_url` is passed through from the clause dict if present
    (clause.get("rule_url")). graph.py's own trace list comprehension does
    not currently surface this field -- confirm the key name against
    agent/reasoner.py / agent/state.py's clause_graph schema. If the key
    differs (e.g. "source_url", "url"), update `_build_trace` in
    api/main.py accordingly.
    """

    clause_ref: str
    rule_url: Optional[str] = None


class ChatResponse(BaseModel):
    type: Literal["answer", "clarification", "explanation", "human_handoff_prompt"]
    thread_id: str
    content: str
    trace: Optional[list[ClauseCitation]] = None
    conflicts: Optional[list] = None


class NewSessionResponse(BaseModel):
    thread_id: str


class GuidelineItem(BaseModel):
    title: str
    body: str


class UIConfigResponse(BaseModel):
    """Everything the frontend needs from api/content.py, in one call."""

    greeting_message: str
    thinking_words: list[str]
    thinking_word_interval_ms: int


class GuidelinesResponse(BaseModel):
    guidelines: list[GuidelineItem]
