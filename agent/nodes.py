"""
agent/nodes.py

The deterministic nodes of the graph -- the ones that either need only a
small, focused LLM call, or no LLM call at all. The one node that genuinely
needs open-ended, multi-step reasoning (the deep agent) lives in
agent/reasoner.py instead.

Every node function has the shape LangGraph expects:
    def node(state: AgentState) -> dict
It reads whatever it needs from `state` and returns a dict of the fields it
wants to update. Fields it doesn't return are left untouched.
"""
import json
from typing import Any, Optional, List

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

from agent.state import AgentState
from agent.llm import get_chat_model
from config import prompts 
from agent.retrieval_tools import vector_search, filter_hits, get_parent, get_children
from config.settings import MAX_CLARIFICATION_TURNS
from ingestion.build_vector_db import KEYWORD_FIELDS, BOOL_FIELDS


# ---------------------------------------------------------------------------
# 1. Intake -- turn plain language into normalized, filterable facts
# ---------------------------------------------------------------------------

class ExtractedFacts(BaseModel):
    """Only fields the message actually supports get filled in -- everything
    else stays None so we don't overwrite something we already knew with a
    guess. uncertain_fields is the exception: it's always a complete list,
    recomputed fresh each turn."""
    obligated_actor: Optional[list[str]] = None
    regulated_subject: Optional[list[str]] = None
    activity_type: Optional[list[str]] = None
    applies_to_firm_type: Optional[list[str]] = None
    involves_customer: Optional[bool] = None
    involves_third_party: Optional[bool] = None
    has_financial_threshold: Optional[bool] = None
    documentation_required: Optional[bool] = None
    frequency: Optional[list[str]] = None
    reporting_recipient: Optional[list[str]] = None
    numeric_value: Optional[str] = Field(
        default=None,
        description="Any specific dollar amount, percentage, or count mentioned, as plain text (e.g. '$500', '10%').",
    )
    uncertain_fields: List[str] = Field(
        default_factory=list,
        description=(
            "Complete current list of field names (matching the other field names in this "
            "schema, e.g. 'numeric_value', 'obligated_actor') that the AI has explicitly asked "
            "about and the user clearly indicated they don't know or can't find out. Recomputed "
            "fresh from the full situation each turn -- drop a field from this list as soon as "
            "the user provides any real answer for it, even a partial or ambiguous one."
        ),
    )
    situation_summary: str = Field(
        description="Updated 2-4 sentence plain-language summary of the FULL situation so far, not just this message.",
    )


def intake_node(state: AgentState) -> dict:
    """Extract normalized facts from this turn's message and merge them into
    what we already know. List fields are replaced (not unioned) since the
    model re-derives each field's complete current value from the running
    summary + latest turn; scalar fields are only overwritten when the new
    message actually states them. uncertain_fields is always fully replaced,
    then reconciled so a field can't be both known and uncertain at once."""
    llm = get_chat_model("intake").with_structured_output(ExtractedFacts)

    previous_summary = state.get("situation_summary") or "(none yet -- this is the first message)"
    known_so_far = state.get("known_fields", {})  # still used downstream, just not shown to this LLM call

    latest_user_query = state.get("raw_query")
    latest_ai_message = state.get("messages", [])[-1] if state.get("messages") else None

    previous_gaps = json.dumps(
        [
            {
                "field": field_dict["field"],
                "detail": field_dict["detail"],
            }
            for field_dict in state.get("gaps", [])
        ],
        indent=2,
    )

    if latest_ai_message and isinstance(latest_ai_message, AIMessage):
        context = (
            f"Situation summary so far: {previous_summary}\n\n"
            f"Facts extracted last turn (continuity aid only -- see rule 6 under CRITICAL RULES): {known_so_far}\n\n"
            f"Field(s) the AI's last question targeted (empty if no clarifying question has been asked yet):\n{previous_gaps}\n\n"
            f"Latest interaction between system and user:\nAI: {latest_ai_message.content}\nUser: {latest_user_query}\n"
        )
    else:
        context = (
            f"Situation summary so far: {previous_summary}\n\n"
            f"Facts extracted last turn (continuity aid only -- see rule 6 under CRITICAL RULES): {known_so_far}\n\n"
            f"Field(s) the AI's last question targeted (empty if no clarifying question has been asked yet):\n{previous_gaps}\n\n"
            f"Latest interaction: {latest_user_query}"
        )

    extracted = llm.invoke([
        SystemMessage(content=prompts.INTAKE_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    extracted_fields = extracted.model_dump()
    new_summary = extracted_fields.pop("situation_summary")
    uncertain_fields = extracted_fields.pop("uncertain_fields") or []

    updated = dict(known_so_far)
    for field, value in extracted_fields.items():
        if value is None:
            continue  # still can't be determined -- keep whatever we had
        if isinstance(value, list):
            updated[field] = sorted(set(value))  # REPLACE, not union -- allows corrections
        else:
            updated[field] = value

    # Safety net: a field that now has a real value this turn (or already
    # had one from a prior turn) can't simultaneously be "uncertain" --
    # don't rely on the model to self-reconcile this perfectly.
    uncertain_fields = [f for f in uncertain_fields if f not in updated]

    return {
        "known_fields": updated,
        "situation_summary": new_summary,
        "uncertain_fields": uncertain_fields,
        "messages": [HumanMessage(content=state["raw_query"])],
    }

# ---------------------------------------------------------------------------
# 2. Retrieve -- hybrid metadata-filtered vector search
# ---------------------------------------------------------------------------

_FILTERABLE_FIELDS = set(KEYWORD_FIELDS) | set(BOOL_FIELDS)

def retrieve_node(state: AgentState) -> dict:
    """Build a Qdrant filter out of whatever known_fields overlap with the
    clause payload's actual filterable fields, then search. If the reasoner
    asked for a specific follow-up search (needs_more_search), that text is
    used as the query instead of the original question."""
    known_fields = state.get("known_fields", {})
    filter_conditions = {
        field: value for field, value in known_fields.items()
        if field in _FILTERABLE_FIELDS and value not in (None, [], "", False)
    }

    # Priority: a specific follow-up the reasoner asked for > the running
    # situation summary > the raw message, as a last-resort fallback only
    # (raw_query alone is unreliable once the conversation is past turn 1 --
    # it might just be "yes" or "$500").
    query_text = state.get("needs_more_search") or state.get("situation_summary") or state["raw_query"]
    # filtered_hits = vector_search(query_text, filter_conditions=filter_conditions or None)
    hits = vector_search(query_text, filter_conditions=None)

    # Filter the hits:
    filtered_hits = []
    if filter_conditions:
        filtered_hits = filter_hits(hits, filter_conditions=filter_conditions)

    # If a strict filter starved the results, retry without it rather than
    # returning an empty/weak set -- better to over-fetch and let the
    # reasoner discard irrelevant ones than to miss the right clause because
    # of an over-confident filter.
    # if len(filtered_hits) < 3 and filter_conditions:
    #     hits = vector_search(query_text, filter_conditions=None)
    if len(filtered_hits) >= 4:
        hits = filtered_hits

    return {
        "candidate_clauses": hits,
        "needs_more_search": None,  # consumed
    }

# ---------------------------------------------------------------------------
# 3. Ambiguity and Gap check
# ---------------------------------------------------------------------------

class Gap(BaseModel):
    detail: str
    why_it_matters: str
    determines_clause_applicability: bool
    field: Optional[str] = Field(
        default=None,
        description="The known_fields key this gap would resolve, if it maps to one (e.g. 'applies_to_firm_type', 'numeric_value'). Null if it doesn't map cleanly to a single field.",
    )


class ClarificationAssessment(BaseModel):
    is_ambiguous: bool = Field(
        description="True only if the user's question itself could mean genuinely different things, each pointing at a different set of clauses."
    )
    ambiguity_question: Optional[str] = Field(
        default=None, description="If ambiguous, one short question listing the interpretations in plain language."
    )
    gaps: list[Gap] = Field(
        default=[],
        description="Missing facts that would change WHICH clause(s) apply. Do not list a gap just because candidates differ on some field -- only when that difference would change the outcome for this specific situation.",
    )


# Fields worth showing the LLM alongside clause text, since they're the ones
# that most often distinguish "this clause applies to you" from "it doesn't" --
# NOT used for any Python-side clustering/diffing anymore, just as reading aids.
_CONTEXT_FIELDS = [
    "rule_id", "obligated_actor", "applies_to_firm_type", "involves_customer",
    "involves_third_party", "has_financial_threshold", "frequency",
]


def _format_candidate(c: dict) -> str:
    payload = c["payload"]
    tags = ", ".join(f"{f}={payload.get(f)}" for f in _CONTEXT_FIELDS if payload.get(f) not in (None, [], ""))
    text = payload.get("merged_clause") or payload.get("original_clause") or ""
    return f"[{c['clause_ref']}] ({tags})\n{text}"


def clarification_check_node(state: AgentState) -> dict:
    """One LLM call, reading the actual candidate clause text, that decides
    two things at once:
      1. Is the user's question itself ambiguous (Situation 11)?
      2. Is there a missing fact that would change WHICH clause(s) apply
         (drives clarification for Situations 10, 12, and general
         incompleteness)?

    Deliberately NOT done by diffing payload field values in Python: two
    clauses having different values for a field (e.g. a rule and its own
    exception having different involves_third_party values) is completely
    normal when they're playing different roles in the SAME answer -- that
    is not ambiguity and not a gap. Only an LLM reading the actual clause
    text can tell "these are alternatives" apart from "these are
    complementary parts of one answer", so both checks are folded into a
    single call over the same context instead of two separate heuristics
    that can't see each other's reasoning.
    """
    candidates = state.get("candidate_clauses", [])[:8] # strongest hits only; keeps the prompt focused
    known_fields = state.get("known_fields", {})

    # A field the user later actually answers should stop being "uncertain" --
    # known_fields wins if both are somehow set for the same field.
    uncertain_fields = [f for f in state.get("uncertain_fields", []) if f not in known_fields]

    if not candidates:
        # No candidates at all isn't ambiguity -- it's a signal for
        # out-of-scope (Situation 9), which reason_node already handles.
        # Just let it flow through to expand/reason rather than faking a
        # clarifying question that doesn't match anything.
        return {"is_ambiguous": False, "gaps": []}

    llm = get_chat_model("ambiguity").with_structured_output(ClarificationAssessment)
    candidates_block = "\n\n".join(_format_candidate(c) for c in candidates)
    context = (
        f"Situation so far: {state.get('situation_summary') or state['raw_query']}\n"
        f"Facts already known: {known_fields}\n"
        f"Fields the user has already said they don't know -- never generate a gap for "
        f"any of these, even if a candidate clause depends on it: {uncertain_fields}\n\n"
        f"Candidate clauses:\n{candidates_block}"
    )
    result = llm.invoke([
        SystemMessage(content=prompts.CLARIFICATION_SYSTEM_PROMPT),
        HumanMessage(content=context),
    ])

    gaps = [
        g.model_dump() for g in result.gaps
        if not (g.field and g.field in known_fields and known_fields[g.field] not in (None, [], ""))
        and not (g.field and g.field in uncertain_fields)
    ]

    return {
        "is_ambiguous": result.is_ambiguous,
        "ambiguity_question": result.ambiguity_question,
        "gaps": gaps,
        "uncertain_fields": uncertain_fields,  # carried forward, cleaned of anything now resolved
    }


# ---------------------------------------------------------------------------
# 5. Clarify -- ask the single highest-priority open question
# ---------------------------------------------------------------------------

def clarify_node(state: AgentState) -> dict:
    """Turn all open gaps (or the ambiguity question, if that's why we're
    here) into a single combined question and add it to the transcript."""
    if state.get("is_ambiguous") and state.get("ambiguity_question"):
        question = state["ambiguity_question"]
    else:
        gaps = state.get("gaps", [])
        blocking = [g for g in gaps if g["determines_clause_applicability"]] or gaps

        query_text = state.get("situation_summary") or state["raw_query"]
        gaps_text = "\n".join(
            f"- Missing detail: {g['detail']}\n  Why it matters: {g['why_it_matters']}"
            for g in blocking
        )

        llm = get_chat_model("clarify")
        response = llm.invoke([
            SystemMessage(content=prompts.CLARIFY_SYSTEM_PROMPT),
            HumanMessage(content=(
                f"User's situation so far: {query_text}\n\n"
                f"Missing details:\n{gaps_text}"
            )),
        ])
        question = response.content

    return {
        "messages": [AIMessage(content=question)],
        "clarification_count": state.get("clarification_count", 0) + 1,
    }


# ---------------------------------------------------------------------------
# 6. Expand -- walk the clause hierarchy around the retrieved candidates
# ---------------------------------------------------------------------------

def expand_node(state: AgentState) -> dict:
    """For every candidate clause, pull in its full parent chain and its
    direct children, and merge everything into the working clause_graph.
    Cross-references found INSIDE clause text are handled later by the
    reasoner (it has a tool for that) since deciding whether a mentioned
    reference is actually relevant needs judgment, not just structure."""
    existing = {c["clause_ref"]: c for c in state.get("clause_graph", [])}

    for candidate in state.get("candidate_clauses", []):
        _add_to_graph(existing, candidate["clause_ref"], candidate["payload"], "retrieved")

        # for parent in get_parent(candidate["clause_ref"]):
        #     _add_to_graph(existing, parent["clause_ref"], parent, "parent")

        # for child in get_children(candidate["clause_ref"]):
        #     _add_to_graph(existing, child["clause_ref"], child, "child")

    return {"clause_graph": list(existing.values())}


def _add_to_graph(graph: dict, clause_ref: str, payload: dict, provenance: str) -> None:
    """Add a clause to the working graph if it isn't already there. Doesn't
    overwrite an entry that's already been reasoned over (has a
    relevance_role) just because it shows up again as a parent/child of a
    different seed clause."""
    if clause_ref in graph:
        return
    graph[clause_ref] = {
        "clause_ref": clause_ref,
        "payload": payload,
        "relevance_role": None,
        "reasoning": None,
        "provenance": provenance,
    }