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
from typing import Optional

from pydantic import BaseModel, Field
from langchain_core.messages import SystemMessage, HumanMessage

from deepagents import create_deep_agent

from agent.state import AgentState
from agent.llm import get_chat_model
from agent.retrieval_tools import REASONER_TOOLS
from config import prompts 
from config.settings import MAX_REASONING_CYCLES


# ---------------------------------------------------------------------------
# Structured output the reasoner must produce
# ---------------------------------------------------------------------------

ALLOWED_ROLES = {
    "rule", "definition", "exception", "condition", "safe_harbor", "override",
    "procedural", "calculation", "record_keeping", "disclosure",
    "cross_reference", "table_row",
}


class ReasonedClause(BaseModel):
    clause_ref: str
    relevance_role: str = Field(description=f"One of: {', '.join(sorted(ALLOWED_ROLES))}")
    reasoning: str = Field(description="2-4 sentences: which fact triggered this, what it contributes.")


class ReasonedConflict(BaseModel):
    clause_refs: list[str]
    description: str
    resolution: Optional[str] = None


class ReasonerOutput(BaseModel):
    sufficient: bool = Field(description="True if this clause set fully answers the situation.")
    needs: Optional[str] = Field(default=None, description="If not sufficient, what to search for next.")
    out_of_scope: bool = False
    scope_note: Optional[str] = None
    clauses: list[ReasonedClause] = []
    conflicts: list[ReasonedConflict] = []


# Build once, reused across calls -- the deep agent object itself is stateless
# per-invocation (conversation state is whatever we pass into .invoke()).
_reasoner_agent = None


def _get_reasoner_agent():
    global _reasoner_agent
    if _reasoner_agent is None:
        _reasoner_agent = create_deep_agent(
            model=get_chat_model("reasoner"),
            tools=REASONER_TOOLS,
            system_prompt=prompts.REASONER_SYSTEM_PROMPT,
            response_format=ReasonerOutput,
        )
    return _reasoner_agent


def reason_node(state: AgentState) -> dict:
    """Hand the working clause_graph to the deep agent, let it investigate
    (chase cross-references, pull more context, etc.) and return its
    structured verdict. Merge that verdict back into clause_graph."""
    agent = _get_reasoner_agent()

    clause_summaries = [
        {"clause_ref": c["clause_ref"], "text": c["payload"].get("merged_clause") or c["payload"].get("original_clause"),
         "rule_id": c["payload"].get("rule_id"), "provenance": c.get("provenance")}
        for c in state.get("clause_graph", [])
    ]
    task = (
        f"Known facts about the situation: {state.get('known_fields', {})}\n\n"
        f"User's question: {state['raw_query']}\n\n"
        f"Candidate clauses gathered so far:\n{json.dumps(clause_summaries, indent=2)}"
    )

    result = agent.invoke({"messages": [{"role": "user", "content": task}]})
    output: ReasonerOutput = result["structured_response"]

    graph = {c["clause_ref"]: c for c in state.get("clause_graph", [])}
    for reasoned in output.clauses:
        if reasoned.clause_ref in graph:
            graph[reasoned.clause_ref]["relevance_role"] = reasoned.relevance_role
            graph[reasoned.clause_ref]["reasoning"] = reasoned.reasoning
        else:
            # The reasoner pulled in a clause via a tool call (e.g. a cross
            # reference) that wasn't in our pre-expanded set -- keep it.
            graph[reasoned.clause_ref] = {
                "clause_ref": reasoned.clause_ref,
                "payload": {},  # not fetched here; synthesis only needs clause_ref + reasoning
                "relevance_role": reasoned.relevance_role,
                "reasoning": reasoned.reasoning,
                "provenance": "reasoner_tool_call",
            }

    cycles = state.get("reasoning_cycles", 0)
    hit_cap = cycles >= MAX_REASONING_CYCLES

    return {
        "clause_graph": list(graph.values()),
        "conflicts": [c.model_dump() for c in output.conflicts],
        "out_of_scope": output.out_of_scope,
        "scope_note": output.scope_note,
        # Stop looping once we hit the cap even if the reasoner still wants more --
        # better to answer with caveats than loop forever.
        "needs_more_search": None if (output.sufficient or hit_cap) else output.needs,
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
        return {"final_answer": note}

    relevant = [c for c in state.get("clause_graph", []) if c.get("relevance_role")]
    if not relevant:
        return {"final_answer": "I wasn't able to find a clause that clearly applies to this situation. Could you share a bit more detail?"}

    llm = get_chat_model("reasoner")
    context = {
        "known_facts": state.get("known_fields", {}),
        "question": state["raw_query"],
        "reasoned_clauses": [
            {"clause_ref": c["clause_ref"], "role": c["relevance_role"], "reasoning": c["reasoning"]}
            for c in relevant
        ],
        "conflicts": state.get("conflicts", []),
    }
    response = llm.invoke([
        SystemMessage(content=prompts.SYNTHESIS_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(context, indent=2)),
    ])

    return {"final_answer": response.content}
