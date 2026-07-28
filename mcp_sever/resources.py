"""
mcp_server/resources.py

Exact-key read resources -- "give me this known thing", not a search.
Both are backed directly by the flat data files (see data_access.py), not
Qdrant: an exact clause_ref/rule_id lookup doesn't need semantic search, so
these resources stay cheap and dependency-free even if the vector DB is
down.
"""

from .instance import mcp
from . import data_access


@mcp.resource("finra-clause://{clause_ref}")
def clause_resource(clause_ref: str) -> dict:
    """A single normalized clause: its text (original, and merged with any
    sub-clause rollups), rule metadata, and the structured fields used for
    filtering (obligated_actor, involves_customer, activity_type, ...).

    Use this when you already have a clause_ref (e.g. from search_clauses,
    or cited in another clause via resolve_cross_references) and want its
    full record. Use search_clauses instead if you don't know the exact ref.
    """
    clause = data_access.get_normalized_clause(clause_ref)
    if clause is None:
        raise ValueError(f"No clause found with clause_ref={clause_ref!r}")
    return clause


@mcp.resource("finra-rule://{rule_id}")
def rule_resource(rule_id: str) -> dict:
    """An entire FINRA rule: its meta (name, category, source URL) and
    every clause under it, both as originally parsed and after sub-clause
    merging. rule_id is the bare rule number, e.g. '2010' or '3110' -- not
    a clause_ref.

    Use this to pull a whole rule into context at once (e.g. before
    answering several questions about it), rather than fetching its
    clauses one at a time via finra-clause://.
    """
    rule = data_access.get_rule(rule_id)
    if rule is None:
        raise ValueError(f"No rule found with rule_id={rule_id!r}")
    return rule
