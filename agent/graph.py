"""
agent/graph.py

Wires all the nodes from agent/nodes.py and agent/reasoner.py into one
LangGraph StateGraph, and exposes run_turn() -- the single function you call
from anywhere (a REPL loop today, a FastAPI route later) to advance the
conversation by one user message.

Control flow:

    intake -> ambiguity_check --(ambiguous)--> clarify --> END
                    |
              (not ambiguous)
                    v
                retrieve -> gap_analysis --(blocking gap)--> clarify --> END
                                  |
                            (no blocking gap)
                                  v
                              expand -> reason --(needs more)--> retrieve
                                            |
                                      (sufficient)
                                            v
                                       synthesize -> END

Each call to run_turn() is one full graph.invoke(). Conversation memory
(known_fields, clause_graph, gaps, etc.) persists BETWEEN calls because we
use a checkpointer keyed by thread_id -- so when the user answers a
clarifying question, the next run_turn() call picks up right where the
conversation left off, re-running intake on the new message and continuing
from there with everything already known preserved.
"""

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver

from agent.state import AgentState
from agent.nodes import (
    intake_node,
    ambiguity_node,
    retrieve_node,
    gap_analysis_node,
    clarify_node,
    expand_node,
)
from agent.reasoner import reason_node, synthesize_node
from config.settings import MAX_CLARIFICATION_TURNS


def _route_after_ambiguity(state: AgentState) -> str:
    return "clarify" if state.get("is_ambiguous") else "retrieve"


def _route_after_gaps(state: AgentState) -> str:
    blocking = [g for g in state.get("gaps", []) if g["determines_clause_applicability"]]
    if blocking and state.get("clarification_count", 0) < MAX_CLARIFICATION_TURNS:
        return "clarify"
    return "expand"


def _route_after_reason(state: AgentState) -> str:
    return "retrieve" if state.get("needs_more_search") else "synthesize"


def build_graph():
    """Assemble and compile the graph. Call get_graph() instead of this
    directly -- it caches the compiled graph so we don't rebuild it (and
    reconstruct the deep agent) on every single turn."""
    g = StateGraph(AgentState)

    g.add_node("intake", intake_node)
    g.add_node("ambiguity_check", ambiguity_node)
    g.add_node("clarify", clarify_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("gap_analysis", gap_analysis_node)
    g.add_node("expand", expand_node)
    g.add_node("reason", reason_node)
    g.add_node("synthesize", synthesize_node)

    g.set_entry_point("intake")
    g.add_edge("intake", "ambiguity_check")
    g.add_conditional_edges("ambiguity_check", _route_after_ambiguity, {"clarify": "clarify", "retrieve": "retrieve"})
    g.add_edge("clarify", END)  # pause; resumed by the NEXT run_turn() call, not within this one
    g.add_edge("retrieve", "gap_analysis")
    g.add_conditional_edges("gap_analysis", _route_after_gaps, {"clarify": "clarify", "expand": "expand"})
    g.add_edge("expand", "reason")
    g.add_conditional_edges("reason", _route_after_reason, {"retrieve": "retrieve", "synthesize": "synthesize"})
    g.add_edge("synthesize", END)

    return g.compile(checkpointer=InMemorySaver())
    # InMemorySaver is fine for development. Swap for a persistent
    # checkpointer (e.g. a Postgres/SQLite one) when this moves behind
    # FastAPI so conversations survive a server restart.


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


def run_turn(user_message: str, thread_id: str) -> dict:
    """
    Advance a conversation by one user message.

    thread_id identifies a single ongoing conversation -- pass the same
    thread_id on every call for the same user session so the checkpointer
    can restore known_fields/clause_graph/gaps/etc. from where the last call
    left off. A new thread_id starts a brand new conversation.

    This is the function to call directly today, and to wrap in a FastAPI
    route later: a route handler just needs to read thread_id from the
    session/request and user_message from the request body, call this, and
    return the dict as JSON.

    Returns one of:
      {"type": "clarification", "content": "<question to show the user>"}
      {"type": "answer", "content": "<final answer text>", "trace": [...]}
    """
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}

    result = graph.invoke(
        {"raw_query": user_message},
        config=config,
    )

    if result.get("final_answer"):
        return {
            "type": "answer",
            "content": result["final_answer"],
            # The trace is what you'd diff against ground_truth_clauses in
            # your eval set later: clause_ref + role + reasoning for every
            # clause that made it into the answer.
            "trace": [
                {"clause_ref": c["clause_ref"], "relevance_role": c["relevance_role"], "reasoning": c["reasoning"]}
                for c in result.get("clause_graph", [])
                if c.get("relevance_role")
            ],
            "conflicts": result.get("conflicts", []),
        }

    # The graph stopped at "clarify" -- the question we want to show the
    # user is the last message added to the transcript.
    last_message = result["messages"][-1]
    return {"type": "clarification", "content": last_message.content}


if __name__ == "__main__":
    # Minimal manual test loop: `python -m agent.graph`
    import uuid
    thread_id = str(uuid.uuid4())
    print("FINRA compliance assistant (Ctrl+C to quit)\n")
    while True:
        user_message = input("you: ")
        response = run_turn(user_message, thread_id)
        print(f"\nassistant: {response['content']}\n")
