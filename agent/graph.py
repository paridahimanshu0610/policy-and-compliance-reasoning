"""
agent/graph.py

Wires all the nodes from agent/nodes.py, agent/reasoner.py, agent/scope_guard.py
and agent/human_handoff.py into one LangGraph StateGraph, and exposes
run_turn() -- the single function you call from anywhere (a REPL loop today,
a FastAPI route later) to advance the conversation by one user message.

Each call to run_turn() is one full graph.invoke() (or, if the graph is
paused mid-way through the human-handoff Q&A, one graph resume via
Command(resume=...)). Conversation memory (known_fields, clause_graph, gaps,
pii_map, etc.) persists BETWEEN calls because we use a checkpointer keyed by
thread_id.

Turn flow, high level:

    mask_input -> scope_gate -+-> human_handoff -> END          (user asked for a human)
                              +-> out_of_scope -> END            (off-topic message)
                              +-> intake -> retrieve -> clarification_check -+-> clarify -> END
                                                                             +-> clarification_cap -> human_handoff -> END
                                                                             +-> expand -> reason <-> retrieve (loop)
                                                                                             |
                                                                                             v
                                                                                        synthesize -+-> END
                                                                                                     +-> human_handoff -> END   (reasoning cap exceeded)
"""

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.state import AgentState
from agent.nodes import (
    mask_input_node,
    intake_node,
    retrieve_node,
    ambiguity_and_gap_check_node,
    clarification_and_reason_cap_check_node,
    clarify_node,
    expand_node,
    explain_node,
    _needs_clarify
)
from agent.scope_guard import scope_gate_node, out_of_scope_node
from agent.human_handoff import human_handoff_node
from agent.reasoner import reason_node, synthesize_node
from agent.pii import unmask_pii
from config.settings import MAX_CLARIFICATION_TURNS, FINRA_BASE_URL


def _route_after_scope_gate(state: AgentState) -> str:
    if state.get("wants_human_agent"):
        return "human_handoff"
    if state.get("wants_explanation"):
        return "explain"
    if not state.get("in_scope"):
        return "out_of_scope"
    return "intake"


def _route_after_cap_check(state: AgentState) -> str:
    # HITL trigger #1: we'd normally ask yet another clarifying question, but
    # we've already exceeded the budget for this conversation -- offer a
    # human handoff instead of asking a question the user will never see.
    if state.get("escalation_reason", None) in {"clarification_cap", "reason_cap"}:
        return "human_handoff"

    clarification_needed = _needs_clarify(state)

    if clarification_needed:
        return "clarify"
    else:
        return "expand"

def _route_after_reason(state: AgentState) -> str:
    return "retrieve" if state.get("needs_more_search") else "synthesize"

def _route_after_synthesize(state: AgentState) -> str:
    # HITL trigger #2: reasoning_cycles exceeded MAX_REASONING_CYCLES this
    # turn (set on state by reason_node). synthesize_node has already run,
    # so the user still gets the best-effort answer -- this just tacks the
    # handoff offer on afterward instead of replacing the answer with it.
    if state.get("escalation_reason") == "reason_cap":
        return "human_handoff"
    return "end"


def build_graph():
    """Assemble and compile the graph. Call get_graph() instead of this
    directly -- it caches the compiled graph so we don't rebuild it (and
    reconstruct the deep agent) on every single turn."""
    g = StateGraph(AgentState)

    g.add_node("mask_input", mask_input_node)
    g.add_node("scope_gate", scope_gate_node)
    g.add_node("out_of_scope", out_of_scope_node)
    g.add_node("intake", intake_node)
    g.add_node("ambiguity_and_gap_check", ambiguity_and_gap_check_node)   # was: ambiguity_check + gap_analysis
    g.add_node("clarification_and_reason_cap_check", clarification_and_reason_cap_check_node)
    g.add_node("clarify", clarify_node)
    g.add_node("explain", explain_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("expand", expand_node)
    g.add_node("reason", reason_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("human_handoff", human_handoff_node)

    g.set_entry_point("mask_input")
    g.add_edge("mask_input", "scope_gate")
    g.add_conditional_edges(
        "scope_gate",
        _route_after_scope_gate,
        {
            "human_handoff": "human_handoff",
            "out_of_scope": "out_of_scope",
            "explain": "explain",
            "intake": "intake",
        },
    )
    g.add_edge("out_of_scope", END)
    g.add_edge("explain", END)
    g.add_edge("intake", "retrieve")
    g.add_edge("retrieve", "ambiguity_and_gap_check")
    g.add_edge("ambiguity_and_gap_check", "clarification_and_reason_cap_check")
    g.add_conditional_edges(
        "clarification_and_reason_cap_check",
        _route_after_cap_check,
        {"clarify": "clarify", "expand": "expand", "human_handoff": "human_handoff"},
    )
    g.add_edge("clarify", END)
    g.add_edge("expand", "reason")
    g.add_conditional_edges("reason", _route_after_reason, {"retrieve": "retrieve", "synthesize": "synthesize"})
    g.add_edge("synthesize", END)
    g.add_edge("human_handoff", END)

    return g.compile(checkpointer=InMemorySaver())
    # InMemorySaver is fine for development. Swap for a persistent
    # checkpointer (e.g. a Postgres/SQLite one) when this moves behind
    # FastAPI so conversations survive a server restart -- this also becomes
    # required (not just nice-to-have) once human_handoff_node's interrupt()
    # calls need to survive across a server restart mid-Q&A.


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


def run_turn(user_message: str, thread_id: str, callbacks: list | None = None) -> dict:
    """
    Advance a conversation by one user message.
 
    thread_id identifies a single ongoing conversation -- pass the same
    thread_id on every call for the same user session so the checkpointer
    can restore known_fields/clause_graph/gaps/pii_map/etc. from where the
    last call left off. A new thread_id starts a brand new conversation.
 
    This is the function to call directly today, and to wrap in a FastAPI
    route later: a route handler just needs to read thread_id from the
    session/request and user_message from the request body, call this, and
    return the dict as JSON.
 
    Returns one of:
      {"type": "clarification", "content": "<question to show the user>"}
      {"type": "human_handoff_prompt", "content": "<consent/name/email/note question>"}
      {"type": "answer", "content": "<final answer text>", "trace": [...]}
    """
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        config["callbacks"] = callbacks
 
    # If the graph is currently paused inside human_handoff_node's
    # interrupt() sequence, this turn's message is the user's answer to that
    # pending question -- resume, don't restart.
    #
    # NOTE: deliberately checking snapshot.tasks[*].interrupts here, NOT
    # snapshot.next. Once a node has paused on more than one interrupt()
    # call (as human_handoff_node does -- consent, then name, then email,
    # then note), snapshot.next goes back to () even though the task is
    # still genuinely interrupted; snapshot.tasks still correctly reports
    # the pending Interrupt on that task. Using .next here would cause the
    # second-and-later resumes in the same handoff sequence to be treated
    # as brand new conversations, restarting from mask_input instead of
    # continuing the Q&A.
    snapshot = graph.get_state(config)
    is_paused_on_interrupt = any(task.interrupts for task in snapshot.tasks)
    if is_paused_on_interrupt:
        result = graph.invoke(Command(resume=user_message), config=config)
    else:
        result = graph.invoke({"raw_query": user_message}, config=config)
 
    pii_map = result.get("pii_map", {})
 
    if result.get("__interrupt__"):
        payload = result["__interrupt__"][0].value
        question = payload.get("question", str(payload)) if isinstance(payload, dict) else str(payload)
        return {"type": "human_handoff_prompt", "content": unmask_pii(question, pii_map)}

    output_type = result.get("turn_output_type")

    if output_type == "explanation":
        return {
            "type": "explanation",
            "content": unmask_pii(result["messages"][-1].content, pii_map),
        }

    if output_type == "answer" or output_type == "out_of_scope":
        return {
            "type": "answer",
            "content": unmask_pii(result["final_answer"], pii_map),
            "trace": [
                {"clause_ref": c["clause_ref"], "relevance_role": c["relevance_role"], "reasoning": c["reasoning"], "rule_url": c["payload"].get("rule_url", FINRA_BASE_URL)}
                for c in result.get("clause_graph", [])
                if c.get("relevance_role")
            ],
            "conflicts": result.get("conflicts", []),
        }

    if output_type == "clarification":
        last_message = result["messages"][-1]
        return {"type": "clarification", "content": unmask_pii(last_message.content, pii_map)}

    # Fallback -- should not normally be hit; every terminal node now stamps
    # turn_output_type. Kept only as a defensive default.
    last_message = result["messages"][-1]
    return {"type": "clarification", "content": unmask_pii(last_message.content, pii_map)}


if __name__ == "__main__":
    # Minimal manual test loop: `python -m agent.graph`
    import uuid
    thread_id = str(uuid.uuid4())
    print("FINRA compliance assistant (Ctrl+C to quit)\n")
    while True:
        user_message = input("you: ")
        response = run_turn(user_message, thread_id)
        print(f"\nassistant: {response['content']}\n")
