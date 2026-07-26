"""
eval/judge.py

Every metric here needs a judgment call an exact-match comparison can't
make (does this reasoning capture the same *point* as the reference, does
this answer actually *convey* a required fact, is this claim about a
clause's text actually *true* to that text). Each judge call is a small,
single-purpose structured-output LLM call -- not one giant "grade
everything" prompt -- so a bad parse or a weak judgment on one dimension
doesn't take the others down with it, and each is independently
inspectable/re-runnable.

All judge calls use the "judge" LLM role (config.settings.ACTIVE_LLM /
LLM_MODELS) via agent.llm.get_chat_model, temperature=0 (get_chat_model's
default), for repeatable scoring.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

from agent.llm import get_chat_model
from config import prompts


# ---------------------------------------------------------------------------
# Structured outputs
# ---------------------------------------------------------------------------

class CoverageVerdict(BaseModel):
    coverage: Literal["full", "partial", "missed"]
    justification: str


class MustMentionVerdict(BaseModel):
    covered: bool
    justification: str


class GroundednessVerdict(BaseModel):
    groundedness: Literal["grounded", "minor_issue", "fabricated"]
    justification: str
    unsupported_claims: list[str] = Field(default_factory=list)


class NonGoldRelevanceVerdict(BaseModel):
    relevance: Literal["relevant", "tangential", "noise"]
    justification: str


class QualityVerdict(BaseModel):
    responsiveness_score: int = Field(ge=1, le=5)
    responsiveness_justification: str
    structural_clarity_score: int = Field(ge=1, le=5)
    structural_clarity_justification: str


def _judge_llm():
    return get_chat_model("judge")


# ---------------------------------------------------------------------------
# 1. Coverage -- does system reasoning for a matched clause match the
#    reference contribution_reasoning?
# ---------------------------------------------------------------------------

def judge_coverage(
    situation_summary: str,
    clause_ref: str,
    clause_text: str,
    reference_reasoning: str,
    system_reasoning: str,
) -> CoverageVerdict:
    llm = _judge_llm().with_structured_output(CoverageVerdict)
    context = (
        f"Clause: {clause_ref}\n"
        f"Clause text: {clause_text or '(text not available)'}\n\n"
        f"User's situation: {situation_summary}\n\n"
        f"Reference (expert) explanation: {reference_reasoning}\n\n"
        f"System's reasoning: {system_reasoning or '(no reasoning recorded)'}"
    )
    return llm.invoke([
        SystemMessage(content=prompts.JUDGE_COVERAGE_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])


# ---------------------------------------------------------------------------
# 2. must_mention coverage -- does the final answer convey a required point?
# ---------------------------------------------------------------------------

def judge_must_mention(final_answer: str, required_point: str) -> MustMentionVerdict:
    llm = _judge_llm().with_structured_output(MustMentionVerdict)
    context = f"Final answer:\n{final_answer}\n\nRequired point: {required_point}"
    return llm.invoke([
        SystemMessage(content=prompts.JUDGE_MUST_MENTION_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])


# ---------------------------------------------------------------------------
# 3. Groundedness -- is a clause_graph entry's reasoning faithful to that
#    clause's actual text?
# ---------------------------------------------------------------------------

def judge_groundedness(
    situation_summary: str,
    clause_ref: str,
    clause_text: str,
    system_reasoning: str,
) -> GroundednessVerdict:
    llm = _judge_llm().with_structured_output(GroundednessVerdict)
    context = (
        f"User's situation: {situation_summary}\n\n"
        f"Clause {clause_ref} actual text: {clause_text or '(text not available -- treat any specific claim about clause content as unverifiable and flag it)'}\n\n"
        f"System's reasoning about this clause: {system_reasoning or '(no reasoning recorded)'}"
    )
    return llm.invoke([
        SystemMessage(content=prompts.JUDGE_GROUNDEDNESS_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])


# ---------------------------------------------------------------------------
# 4. Non-gold clause relevance
# ---------------------------------------------------------------------------

def judge_non_gold_relevance(
    situation_summary: str,
    clause_ref: str,
    system_reasoning: str,
) -> NonGoldRelevanceVerdict:
    llm = _judge_llm().with_structured_output(NonGoldRelevanceVerdict)
    context = (
        f"User's situation: {situation_summary}\n\n"
        f"Extra clause pulled in: {clause_ref}\n"
        f"System's stated reasoning for including it: {system_reasoning or '(no reasoning recorded)'}"
    )
    return llm.invoke([
        SystemMessage(content=prompts.JUDGE_NON_GOLD_RELEVANCE_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])


# ---------------------------------------------------------------------------
# 5. Answer quality -- responsiveness + structural clarity
# ---------------------------------------------------------------------------

def judge_quality(
    situation_summary: str,
    final_answer: str,
    expected_answer_structure: Optional[str],
) -> QualityVerdict:
    llm = _judge_llm().with_structured_output(QualityVerdict)
    context = (
        f"User's situation: {situation_summary}\n\n"
        f"Final answer:\n{final_answer}\n\n"
        f"Expected answer structure (guidance, not a rigid template): "
        f"{expected_answer_structure or '(none provided)'}"
    )
    return llm.invoke([
        SystemMessage(content=prompts.JUDGE_QUALITY_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])
