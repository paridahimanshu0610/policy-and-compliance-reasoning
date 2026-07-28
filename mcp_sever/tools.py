"""
mcp_server/tools.py

MCP tools, two kinds, deliberately kept distinct:

  - Retrieval tools (search_clauses, get_clause_children, get_clause_parent,
    resolve_cross_references, list_rules) -- thin wrappers around the same
    plain functions agent/retrieval_tools.py already exposes to the internal
    reasoner deep agent. Read-only, side-effect-free, safe to call
    repeatedly -- annotated as such so a well-behaved MCP client/host can
    treat them accordingly (e.g. auto-approve without a confirmation
    prompt).

  - ask_finra_compliance_agent -- wraps agent.graph.run_turn(), i.e. the
    whole orchestrated pipeline (scope gate, clarification loop, retrieval,
    reasoning, synthesis, and -- only if the user explicitly consents
    mid-conversation -- a human handoff that sends a real email). NOT
    annotated read-only, since a full conversation through this tool can
    end in that side effect.
"""

from __future__ import annotations

import re
import uuid

from mcp.types import ToolAnnotations

from .instance import mcp
from . import data_access
from agent import retrieval_tools
from agent.graph import run_turn

# Fully qualified clause_ref citations embedded mid-paragraph in a clause's
# own text, e.g. "...as described in FINRA-3110(b)(6)(C)(ii)a.1...".
# Same pattern used elsewhere in the project for parsing clause_ref strings.
_CLAUSE_REF_PATTERN = re.compile(
    r"\bFINRA-\d+"
    r"(?:\([^\s()]{1,6}\)|[A-Za-z][A-Za-z0-9]{0,2}\.)"      # required first clause segment
    r"(?:\([^\s()]{1,6}\)|[A-Za-z0-9]{1,3}\.?)*"            # optional further segments
)

# The "Cross References" footer that appears at the end of many clauses
# (see aggregate_normalized_clauses.jsonl) doesn't cite full clause_refs --
# it cites bare rule numbers against a title, one per line, e.g.:
#   "2111, Suitability"
#   "IM-12000, Failure to Act Under Provisions of Code of Arbitration..."
# These two patterns are intentionally separate from _CLAUSE_REF_PATTERN
# above: they point at a different citation shape (a whole rule, not a
# specific sub-clause) and need to be resolved differently (see
# data_access.intro_clause_for_rule).
_CROSS_REF_HEADER = re.compile(r"Cross References\s*[-\u2013\u2014]?\s*\n?", re.IGNORECASE)
_BARE_REF_LINE = re.compile(r"(?m)^\s*((?:IM-)?\d{3,6}(?:\.\d{2})?)\s*,\s*(.+?)\s*$")

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)


@mcp.tool(annotations=READ_ONLY)
def search_clauses(
    query: str,
    search_mode: str = "dense",
    rule_id: str | None = None,
    obligated_actor: list[str] | None = None,
    involves_customer: bool | None = None,
    activity_type: list[str] | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Search the FINRA clause knowledge base by topic.

    search_mode: "dense" for semantic/paraphrased queries, "sparse" (BM25)
    for exact terms or rule citations likely to appear verbatim in the
    clause text. Only pass a filter if you're confident it applies -- an
    incorrect filter silently excludes relevant clauses rather than
    erroring.
    """
    filters = {
        k: v
        for k, v in {
            "rule_id": rule_id,
            "obligated_actor": obligated_actor,
            "involves_customer": involves_customer,
            "activity_type": activity_type,
        }.items()
        if v is not None and v != []
    }
    return retrieval_tools.clause_search(
        query, filter_conditions=filters or None, top_k=top_k, search_mode=search_mode
    )


@mcp.tool(annotations=READ_ONLY)
def get_clause_children(clause_ref: str, include_grandchildren: bool = False) -> list[dict]:
    """Fetch every clause that sits directly under clause_ref (or, with
    include_grandchildren=True, every descendant at any depth). Use this to
    check whether a clause has more specific sub-provisions worth reading."""
    return retrieval_tools.get_children(clause_ref, include_grandchildren=include_grandchildren)


@mcp.tool(annotations=READ_ONLY)
def get_clause_parent(clause_ref: str, include_all_ancestors: bool = False) -> list[dict]:
    """Fetch the parent of clause_ref (or, with include_all_ancestors=True,
    the full ancestor chain up to the rule's top-level clause). Use this to
    see the broader obligation a specific sub-clause sits under."""
    return retrieval_tools.get_parent(clause_ref, include_all_ancestors=include_all_ancestors)


@mcp.tool(annotations=READ_ONLY)
def list_rules(category: str | None = None) -> list[dict]:
    """List every FINRA rule's metadata (rule_id, name, category, source
    URL), optionally filtered to one rule_category (e.g.
    'duties_and_conflicts'). Use this to browse when you don't yet know a
    specific rule_id or clause_ref -- then follow up with the finra-rule://
    resource or search_clauses."""
    return data_access.list_rule_metas(category)


@mcp.tool(annotations=READ_ONLY)
def resolve_cross_references(clause_ref: str) -> dict:
    """Resolve everything a clause cites into actual clause/rule records
    instead of leaving them as unresolved prose.

    A FINRA clause's text can cite other provisions in two different
    shapes:
      1. A "Cross References" footer listing bare rule numbers against a
         title (e.g. "2111, Suitability") -- each points at another
         rule's top-level clause, not one specific sub-clause.
      2. An inline citation of a fully qualified clause_ref mid-paragraph
         (e.g. "...as described in FINRA-3110(b)(6)(C)(ii)a.1...") --
         points at one exact sub-clause.

    Returns both kinds resolved against the local data, plus anything
    cited that couldn't be resolved (clause text does sometimes cite rules
    outside the 2000/3000/4000 series this project covers).
    """
    clause = data_access.get_normalized_clause(clause_ref)
    if clause is None:
        raise ValueError(f"No clause found with clause_ref={clause_ref!r}")

    text = clause.get("merged_clause") or clause.get("original_clause") or ""

    cited_rules: list[dict] = []
    unresolved_rules: list[dict] = []
    header_match = _CROSS_REF_HEADER.search(text)
    if header_match:
        block = text[header_match.end():]
        for m in _BARE_REF_LINE.finditer(block):
            ref_rule_id, title = m.group(1), m.group(2)
            intro = data_access.intro_clause_for_rule(ref_rule_id)
            if intro is not None:
                cited_rules.append({"rule_id": ref_rule_id, "title": title, "clause": intro})
            else:
                unresolved_rules.append({"rule_id": ref_rule_id, "title": title})

    cited_clauses: list[dict] = []
    unresolved_clauses: list[str] = []
    seen_refs: set[str] = set()
    for m in _CLAUSE_REF_PATTERN.finditer(text):
        ref = m.group()
        if ref == clause_ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        target = data_access.get_normalized_clause(ref)
        if target is not None:
            cited_clauses.append(target)
        else:
            unresolved_clauses.append(ref)

    return {
        "clause_ref": clause_ref,
        "cited_rules": cited_rules,
        "cited_clauses": cited_clauses,
        "unresolved": {"rule_ids": unresolved_rules, "clause_refs": unresolved_clauses},
    }


@mcp.tool()
def ask_finra_compliance_agent(query: str, thread_id: str | None = None) -> dict:
    """Run one turn of the full FINRA compliance reasoning agent -- the same
    pipeline used by the project's own chat interface: scope-gating, PII
    masking, clarification questions when the situation is ambiguous or
    under-specified, iterative clause retrieval + reasoning, and a final
    synthesized answer with a clause-by-clause trace.

    Unlike search_clauses (raw hits for you to interpret), this returns a
    reasoned answer: which clauses actually apply, why, and any conflicts
    between them.

    thread_id identifies one ongoing conversation. Omit it to start a new
    one -- the thread_id in the response must be passed on every
    subsequent call for that same conversation, since the agent may need
    to ask a clarifying question (or, only with the caller's explicit
    consent partway through, hand off to a human compliance agent -- which
    sends a real email) before it can give a final answer.

    Returns {"thread_id", "type", "content", ...} where type is one of
    "clarification", "human_handoff_prompt", or "answer".
    """
    resolved_thread_id = thread_id or str(uuid.uuid4())
    result = run_turn(query, resolved_thread_id)
    return {"thread_id": resolved_thread_id, **result}
