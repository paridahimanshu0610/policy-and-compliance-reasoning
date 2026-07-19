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
from agent.retrieval_tools import vector_search, get_parent_chain, get_children
from config.settings import MAX_CLARIFICATION_TURNS
from ingestion.build_vector_db import KEYWORD_FIELDS, BOOL_FIELDS


# ---------------------------------------------------------------------------
# 1. Intake -- turn plain language into normalized, filterable facts
# ---------------------------------------------------------------------------

class ExtractedFacts(BaseModel):
    """Only fields the message actually supports get filled in -- everything
    else stays None so we don't overwrite something we already knew with a
    guess."""
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
    situation_summary: str = Field(
        description="Updated 2-4 sentence plain-language summary of the FULL situation so far, not just this message.",
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
            f"Field(s) the AI's last question targeted (empty if no clarifying question has been asked yet):\n{previous_gaps}\n\n"
            f"Latest interaction between system and user:\nAI: {latest_ai_message.content}\nUser: {latest_user_query}\n"
        )
    else:
        context = (
            f"Situation summary so far: {previous_summary}\n\n"
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
# 2. Ambiguity check -- is the query itself open to >1 reading?
# ---------------------------------------------------------------------------

class AmbiguityCheck(BaseModel):
    is_ambiguous: bool
    question: Optional[str] = Field(
        default=None, description="If ambiguous, the one clarifying question to ask."
    )


def ambiguity_node(state: AgentState) -> dict:
    """Run a quick, unfiltered search and see whether the top results split
    into more than one distinct topic. If they do, ask the LLM to phrase a
    disambiguating question; if they don't, move straight to retrieval."""
    query_text = state.get("situation_summary") or state["raw_query"]
    hits = vector_search(query_text, top_k=10)
    if not hits:
        return {"is_ambiguous": False}

    top_score = hits[0]["score"]
    # Only look at hits that are genuinely competitive with the top hit --
    # a big score gap means the rest aren't real alternative interpretations.
    close_hits = [h for h in hits if top_score - h["score"] < 0.08]
    topic_clusters = {tuple(h["payload"].get("activity_type", [])) for h in close_hits}

    if len(topic_clusters) < 2:
        return {"is_ambiguous": False}

    llm = get_chat_model("ambiguity").with_structured_output(AmbiguityCheck)
    summary_of_clusters = "\n".join(
        f"- {h['payload'].get('rule_name', h['clause_ref'])}: {h['payload'].get('activity_type')}"
        for h in close_hits
    )
    result = llm.invoke([
        SystemMessage(content=prompts.AMBIGUITY_SYSTEM_PROMPT),
        HumanMessage(content=f"Situation so far: {query_text}\n\nCandidate topics found:\n{summary_of_clusters}"),
    ])

    return {"is_ambiguous": result.is_ambiguous, "ambiguity_question": result.question}


# ---------------------------------------------------------------------------
# 3. Retrieve -- hybrid metadata-filtered vector search
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
    hits = vector_search(query_text, filter_conditions=filter_conditions or None)

    # If a strict filter starved the results, retry without it rather than
    # returning an empty/weak set -- better to over-fetch and let the
    # reasoner discard irrelevant ones than to miss the right clause because
    # of an over-confident filter.
    if len(hits) < 3 and filter_conditions:
        hits = vector_search(query_text, filter_conditions=None)

    return {
        "candidate_clauses": hits,
        "needs_more_search": None,  # consumed
        "reasoning_cycles": state.get("reasoning_cycles", 0) + 1,
    }


# ---------------------------------------------------------------------------
# 4. Gap analysis -- do the candidates need a fact we don't have yet?
# ---------------------------------------------------------------------------

# For each of these payload fields, if the top candidates disagree on its
# value (some clauses apply to one value, others to a different one) and we
# don't yet know the user's actual value, that's a gap worth asking about.
_DISCRIMINATING_FIELDS = {
    "applies_to_firm_type": "What type of firm or role you're asking about (e.g. broker-dealer vs. investor) changes which rule applies.",
    "involves_customer": "Whether a customer is involved changes which rule applies.",
    "involves_third_party": "Whether a third party outside your firm is involved changes which rule applies.",
    "frequency": "How often this happens changes which rule applies.",
}


def gap_analysis_node(state: AgentState) -> dict:
    """Rule-based diff: for each discriminating field, check whether the
    current candidates disagree on it and whether we already know the
    user's value. Also flags a numeric gap when a top candidate depends on
    a dollar/percentage threshold we haven't been given (Situation 10)."""
    known_fields = state.get("known_fields", {})
    candidates = state.get("candidate_clauses", [])[:8]  # only look at the strongest hits
    uncertain_fields = state.get("uncertain_fields", []) 
    gaps = []

    for field, why in _DISCRIMINATING_FIELDS.items():
        if (known_fields.get(field, None) is not None) or (field in uncertain_fields):
            continue  # already known, nothing to ask
        values_seen = {tuple(c["payload"].get(field, [])) if isinstance(c["payload"].get(field), list)
                        else c["payload"].get(field) for c in candidates}
        if len(values_seen) > 1:
            gaps.append({
                "detail": f"whether {field.replace('_', ' ')} applies to your situation",
                "why_it_matters": why,
                "determines_clause_applicability": True,
                "field": field,
            })

    # if any(c["payload"].get("has_financial_threshold") for c in candidates) and not known_fields.get("numeric_value"):
    if known_fields.get("has_financial_threshold", None) and known_fields.get("numeric_value", None) is None:
        gaps.append({
            "detail": "the specific dollar amount, percentage, or count involved",
            "why_it_matters": "This rule's requirement depends on a specific number, and different amounts can trigger different obligations.",
            "determines_clause_applicability": True,
            "field": "numeric_value",
        })

    return {"gaps": gaps}


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

        # for parent in get_parent_chain(candidate["clause_ref"]):
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