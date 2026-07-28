"""
agent/reasoner.py

The one node in the graph that's a deepagents deep agent instead of a single
LLM call. Everything upstream (intake, retrieve, expand) is deterministic-ish
and hands this node a working set of candidate clauses; this node is where
actual judgment happens: which clauses matter, what role each plays, whether
any reference each other, whether they conflict, whether the set is even
complete yet.

Also contains synthesize_node, the final "write the answer" step -- it's a
plain LLM call (not a deep agent), since by the time we get there the hard
reasoning is already done and it's a writing task, not an investigation.
"""

import json
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain.agents.middleware import AgentMiddleware
from langchain.agents import create_agent
from deepagents import create_deep_agent

from agent.state import AgentState
from agent.llm import get_chat_model
from agent.retrieval_tools import REASONER_TOOLS
from config import prompts
from config.settings import MAX_REASONING_CYCLES
from ingestion.build_vector_db import get_clause_by_ref

# ---------------------------------------------------------------------------
# Structured output the reasoner must produce
# ---------------------------------------------------------------------------

RelevanceRole = Literal[
    "rule", "definition", "exception", "condition", "safe_harbor", "override",
    "procedural", "calculation", "record_keeping", "disclosure",
    "cross_reference", "table_row",
]


class ReasonedClause(BaseModel):
    clause_ref: str
    relevance_role: RelevanceRole
    reasoning: str


class ReasonedConflict(BaseModel):
    clause_refs: list[str]
    description: str
    resolution: Optional[str] = None


class ReasonerOutput(BaseModel):
    sufficient: bool
    needs: Optional[str] = None
    out_of_scope: bool = False
    scope_note: Optional[str] = None
    clauses: list[ReasonedClause] = []
    conflicts: list[ReasonedConflict] = []


# Build once, reused across calls -- the deep agent object itself is stateless
# per-invocation (conversation state is whatever we pass into .invoke()).
_reasoner_agent = None

def _normalize_args(args: dict) -> str:
    """Canonical string form of tool args so equivalent calls compare equal
    regardless of key order, list order in clause_ref lists, etc."""
    def _norm(v):
        if isinstance(v, list):
            return sorted(_norm(x) for x in v)
        if isinstance(v, str):
            return v.strip().lower()
        return v
    normalized = {k: _norm(v) for k, v in (args or {}).items()}
    return json.dumps(normalized, sort_keys=True, default=str)

class DedupToolCallMiddleware(AgentMiddleware):
    """Short-circuits a tool call if the exact same tool + args has already
    been called earlier in this run, returning the cached result instead of
    re-invoking the tool."""

    def wrap_tool_call(self, request, handler):
        tool_name = request.tool_call["name"]
        key = (tool_name, _normalize_args(request.tool_call.get("args", {})))

        messages = request.state.get("messages", [])

        seen: dict[tuple, str] = {}
        for msg in messages:
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    seen_key = (tc["name"], _normalize_args(tc.get("args", {})))
                    seen.setdefault(seen_key, tc["id"])

        prior_call_id = seen.get(key)
        if prior_call_id and prior_call_id != request.tool_call["id"]:
            for msg in messages:
                if isinstance(msg, ToolMessage) and msg.tool_call_id == prior_call_id:
                    return ToolMessage(
                        content=(
                            f"[deduped] {tool_name} was already called with these "
                            f"exact arguments earlier in this task. Reusing that "
                            f"result rather than calling the tool again:\n\n{msg.content}"
                        ),
                        tool_call_id=request.tool_call["id"],
                        name=tool_name,
                    )

        return handler(request)
    
class SanitizeNoneContentMiddleware(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        for msg in request.messages:
            if getattr(msg, "content", None) is None:
                msg.content = ""
        return handler(request)

def _get_reasoner_agent(use_tools: bool = False):
    global _reasoner_agent
    if _reasoner_agent is None:
        system_prompt = prompts.REASONER_SYSTEM_PROMPT
        if use_tools:
            system_prompt += prompts.REASONER_TOOL_INSTRUCTIONS

        _reasoner_agent = create_agent(
            model=get_chat_model("reasoner"),
            tools=REASONER_TOOLS if use_tools else None,
            system_prompt=system_prompt,
            response_format=ReasonerOutput,
            middleware=[SanitizeNoneContentMiddleware(),  DedupToolCallMiddleware()],
        )
    return _reasoner_agent

def reason_node(state: AgentState) -> dict:
    """Hand the working clause_graph to the deep agent, let it investigate
    (chase cross-references, pull more context, etc.) and return its
    structured verdict. Merge that verdict back into clause_graph."""
    agent = _get_reasoner_agent(use_tools=True)

    clause_summaries = [
        {"clause_ref": c["clause_ref"], "text": c["payload"].get("merged_clause") or c["payload"].get("original_clause"),
         "rule_id": c["payload"].get("rule_id"), "provenance": c.get("provenance")}
        for c in state.get("clause_graph", [])
    ]
    task = (
        f"Full situation: {state.get('situation_summary') or state['raw_query']}\n\n"
        f"Candidate clauses gathered so far:\n{json.dumps(clause_summaries, indent=2)}"
    )

    # --- NEW: instrumentation start ---
    _start = time.perf_counter()
    # --- end new ---

    result = agent.invoke({"messages": [{"role": "user", "content": task}]})
    output: ReasonerOutput = result["structured_response"]

    # --- NEW: instrumentation end + tool-call tally ---
    _duration = time.perf_counter() - _start
    _tool_calls_by_name: dict[str, int] = {}
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                name = tc.get("name", "unknown")
                _tool_calls_by_name[name] = _tool_calls_by_name.get(name, 0) + 1
    _tool_call_count = sum(_tool_calls_by_name.values())
    # --- end new ---

    initial_graph = {c["clause_ref"]: c for c in state.get("clause_graph", [])}
    new_graph = {}

    tool_retrieved_clause_refs = [reasoned.clause_ref for reasoned in output.clauses if reasoned.clause_ref not in initial_graph]
    tool_retrieved_clauses = get_clause_by_ref(tool_retrieved_clause_refs)
    tool_retrieved_clauses = {clause["clause_ref"]:clause for clause in tool_retrieved_clauses}

    for reasoned in output.clauses:
        if reasoned.clause_ref in initial_graph:
            new_graph[reasoned.clause_ref] = initial_graph[reasoned.clause_ref]
            new_graph[reasoned.clause_ref]["relevance_role"] = reasoned.relevance_role
            new_graph[reasoned.clause_ref]["reasoning"] = reasoned.reasoning
        else:
            new_graph[reasoned.clause_ref] = {
                "clause_ref": reasoned.clause_ref,
                "payload": tool_retrieved_clauses.get(reasoned.clause_ref, {}),
                "relevance_role": reasoned.relevance_role,
                "reasoning": reasoned.reasoning,
                "provenance": "reasoner_tool_call",
            }

    cycles = state.get("reasoning_cycles", 0) + 1
    hit_cap = cycles >= MAX_REASONING_CYCLES

    return {
        "clause_graph": list(new_graph.values()),
        "conflicts": [c.model_dump() for c in output.conflicts],
        "out_of_scope": output.out_of_scope,
        "scope_note": output.scope_note,
        "needs_more_search": None if (output.sufficient or hit_cap) else output.needs,
        "reasoning_cycles": cycles,
        "reasoner_call_log": [{
            "cycle": cycles,
            "duration_seconds": round(_duration, 3),
            "tool_call_count": _tool_call_count,
            "tool_calls_by_name": _tool_calls_by_name,
        }],
    }


# ---------------------------------------------------------------------------
# Synthesis -- write the final answer from the reasoned clause set
# ---------------------------------------------------------------------------

def synthesize_node(state: AgentState) -> dict:
    """Write the final answer strictly from the reasoned clause_graph (only
    clauses with a relevance_role are "in" the answer -- clauses that were
    fetched but the reasoner decided weren't actually relevant just have
    relevance_role=None and get left out)."""
    if state.get("out_of_scope"):
        note = state.get("scope_note") or (
            "This situation doesn't appear to be covered by FINRA Rule series "
            "2000, 3000, or 4000. It may fall under a different rule series, "
            "or outside FINRA's rules entirely -- worth checking with a "
            "compliance professional."
        )
        return {"final_answer": note, "turn_output_type": "answer"}

    relevant = [c for c in state.get("clause_graph", []) if c.get("relevance_role")]
    if not relevant:
        return {
            "final_answer": "I wasn't able to find a clause that clearly applies to this situation. Could you share a bit more detail?",
            "turn_output_type": "answer",
        }

    llm = get_chat_model("reasoner")
    context = {
        "situation": state.get("situation_summary") or state["raw_query"],
        # "known_facts": state.get("known_fields", {}),
        "reasoned_clauses": [
            {"clause_ref": c["clause_ref"], "role": c["relevance_role"], "reasoning": c["reasoning"]} # "clause_actual_text": c["payload"]["merged_clause"],
            for c in relevant
        ],
        "conflicts": state.get("conflicts", []),
    }
    response = llm.invoke([
        SystemMessage(content=prompts.SYNTHESIS_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, indent=2)),
    ])

    return {"final_answer": response.content, "turn_output_type": "answer"}