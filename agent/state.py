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
    # The exact text the user just typed THIS TURN ONLY, AFTER PII masking
    # (see pii_map below). After a clarifying question, this might just be
    # "yes" or "$500" -- it is NOT a restatement of the whole situation, so
    # don't use it alone for retrieval.

    situation_summary: str
    # A running plain-language description of the FULL situation as
    # understood so far, rewritten (not just appended to) every turn by
    # intake_node. This is what retrieval should search on -- it stays
    # coherent even when the latest message is a one-word answer to a
    # clarifying question, because it already has the earlier context baked
    # in. Contains masked PII tokens, same as raw_query -- only unmasked
    # right before something is shown to the user or emailed out.

    # --- accumulated understanding of the situation ---
    known_fields: dict[str, Any]
    # Facts we've extracted so far, in the SAME vocabulary as the clause
    # payload fields in Qdrant (activity_type, obligated_actor,
    # applies_to_firm_type, involves_customer, has_financial_threshold, ...).
    # This dict is what gets handed straight to search_clauses() as
    # filter_conditions. Keeping it in this shape (instead of free-text
    # notes) is what lets "understand the user" and "filter the database"
    # stay in sync automatically.

    uncertain_fields: list[str]

    # --- input guardrails ---
    pii_map: dict[str, str]
    # Reversible mapping of mask token -> original PII value, e.g.
    # {"[[EMAIL_1]]": "jane@example.com"}. Populated by mask_input_node on
    # every turn (accumulates across the whole conversation, keyed by
    # thread_id via the checkpointer) and consulted by run_turn() to unmask
    # any token that ends up in something we show back to the user, and by
    # the human handoff node to unmask the situation_summary before it's
    # emailed to a real person.

    # --- scope gating ---
    in_scope: bool
    # False if the user's latest message isn't a FINRA-compliance question
    # at all (weather, booking a flight, etc.) -- routes straight to a
    # polite redirect instead of entering the main reasoning flow.

    wants_human_agent: bool
    # True if the user directly asked to be connected with a human/agent/
    # representative, independent of scope or any cap being hit.

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
    # config.settings.MAX_CLARIFICATION_TURNS -- once exceeded, we offer a
    # human handoff instead of asking yet another question.

    # --- reasoning outcome ---
    conflicts: list[dict]
    # [{"clause_refs": [...], "description": ..., "resolution": str | None}, ...]

    needs_more_search: str | None
    # Set by the reasoner when the current clause set isn't sufficient yet;
    # holds a short description of what to search for next. None means the
    # reasoner is satisfied and we can move on to writing the final answer.

    reasoning_cycles: int
    # How many retrieve -> reason loops have run this turn. Capped by
    # config.settings.MAX_REASONING_CYCLES -- once exceeded, synthesize still
    # runs (best-effort answer with caveats), but we additionally offer a
    # human handoff afterward.

    out_of_scope: bool
    scope_note: str | None
    # Situation 9: nothing in Rules 2000/3000/4000 actually covers this.
    # (Distinct from in_scope/False above: that's a pre-filter for
    # completely unrelated requests like "what's the weather"; this is the
    # reasoner deciding, after actually investigating, that no FINRA clause
    # applies to an otherwise on-topic compliance situation.)

    # --- human-in-the-loop / compliance-agent handoff ---
    escalation_reason: str | None
    # Why we're routing to human_handoff_node this turn: "user_requested",
    # "clarification_cap", or "reason_cap". None means we're not
    # escalating.

    handoff_name: str | None
    handoff_email: str | None
    handoff_note: str | None
    handoff_sent: bool
    # Filled in by human_handoff_node once the user has gone through the
    # consent -> name -> email -> note sequence and the summary email has
    # (or hasn't) been sent successfully.

    # --- output ---
    final_answer: str | None

    reasoner_call_log: Annotated[list[dict], add]
        # Instrumentation only -- not read by any existing node. One entry
        # appended (via the `add` reducer, same pattern as `messages`) every
        # time reason_node runs:
        #   {
        #       "cycle": int,                # matches reasoning_cycles after this call
        #       "duration_seconds": float,
        #       "tool_call_count": int,
        #       "tool_calls_by_name": dict[str, int],
        #   }
        # Read by the eval harness after a turn completes to compute reasoner
        # loop counts, tool-call counts, and per-cycle timing without needing
        # to touch reason_node's actual control flow or return contract.