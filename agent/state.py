"""
agent/state.py

Defines the one shared "state" object that flows through every node in the
graph. Every node reads some of these fields and returns updates to some of
them. Nothing is passed between nodes except through this object -- that's
what makes the whole run inspectable/loggable (you can print the state after
any node and see exactly what the agent knows at that point).

Kept as plain TypedDicts (not classes) because that's what LangGraph expects
for a graph's state schema.
"""

from typing import Annotated, Any, TypedDict
from operator import add


class AgentState(TypedDict, total=False):
    # --- conversation ---
    messages: Annotated[list, add]
    # Running chat transcript (user + assistant turns). Annotated with `add`
    # so LangGraph appends new messages instead of overwriting the list.

    raw_query: str
    # The exact text the user just typed this turn (not normalized).

    # --- accumulated understanding of the situation ---
    known_fields: dict[str, Any]
    # Facts we've extracted so far, in the SAME vocabulary as the clause
    # payload fields in Qdrant (activity_type, obligated_actor,
    # applies_to_firm_type, involves_customer, has_financial_threshold, ...).
    # This dict is what gets handed straight to search_clauses() as
    # filter_conditions. Keeping it in this shape (instead of free-text
    # notes) is what lets "understand the user" and "filter the database"
    # stay in sync automatically.

    # --- ambiguity handling ---
    is_ambiguous: bool
    ambiguity_question: str | None
    # If the raw query itself could mean more than one thing (Situation 11),
    # we set is_ambiguous=True and ambiguity_question holds the question to
    # show the user, before we even attempt retrieval.

    # --- retrieval + graph expansion ---
    candidate_clauses: list[dict]
    # Raw hits straight out of vector search this cycle:
    # [{"clause_ref": ..., "score": ..., "payload": {...}}, ...]

    clause_graph: list[dict]
    # The working set of clauses after expansion (parents/children/cross-refs)
    # and, later, after the reasoner assigns roles:
    # [{"clause_ref": ..., "payload": {...}, "relevance_role": ..., "reasoning": ...}, ...]
    # Accumulates across retrieve<->reason cycles within a turn (nodes merge
    # into this rather than replacing it), so nothing already-justified gets
    # dropped if the reasoner asks for one more search.

    # --- clarification loop ---
    gaps: list[dict]
    # Missing load-bearing facts found by comparing candidate clauses'
    # payload fields against known_fields:
    # [{"detail": ..., "why_it_matters": ..., "determines_clause_applicability": bool, "field": ...}, ...]

    clarification_count: int
    # How many clarifying questions we've asked this conversation. Capped by
    # config.settings.MAX_CLARIFICATION_TURNS so the agent never loops forever.

    # --- reasoning outcome ---
    conflicts: list[dict]
    # [{"clause_refs": [...], "description": ..., "resolution": str | None}, ...]

    needs_more_search: str | None
    # Set by the reasoner when the current clause set isn't sufficient yet;
    # holds a short description of what to search for next. None means the
    # reasoner is satisfied and we can move on to writing the final answer.

    reasoning_cycles: int
    # How many retrieve -> reason loops have run this turn. Capped by
    # config.settings.MAX_REASONING_CYCLES.

    out_of_scope: bool
    scope_note: str | None
    # Situation 9: nothing in Rules 2000/3000/4000 actually covers this.

    # --- output ---
    final_answer: str | None
