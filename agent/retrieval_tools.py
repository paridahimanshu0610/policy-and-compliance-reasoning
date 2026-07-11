"""
agent/retrieval_tools.py

Everything that touches the vector database lives here, in two layers:

1. Plain Python functions (vector_search, get_clause, get_children,
   lookup_cross_reference) -- used directly by the deterministic graph nodes
   in agent/nodes.py.

2. The same functions wrapped with @tool -- used by the reasoner deep agent
   in agent/reasoner.py, so it can call them itself mid-reasoning (e.g. "this
   clause mentions Rule 4512, let me go look that up") instead of us having
   to predict every lookup it'll need in advance.

Both layers call the same underlying code, so there's only one place that
actually knows how to talk to Qdrant.
"""

import re

from langchain_core.tools import tool

from config.settings import ACTIVE_COLLECTION_NAME, ACTIVE_EMBEDDING_MODEL, RETRIEVAL_TOP_K
from ingestion.build_vector_db import search_clauses, get_clause_by_ref, get_children as _get_children
from ingestion.build_vector_db import generate_query_embeddings


# ---------------------------------------------------------------------------
# Layer 1: plain functions, used by the deterministic nodes
# ---------------------------------------------------------------------------

def vector_search(query_text: str, filter_conditions: dict | None = None, top_k: int | None = None) -> list[dict]:
    """
    Embed query_text with the active embedding model and search the active
    collection, optionally narrowed by filter_conditions (same shape as
    ingestion.build_vector_db.search_clauses expects).
    """
    embedding = generate_query_embeddings(query_text, ACTIVE_EMBEDDING_MODEL)
    return search_clauses(
        query_embedding=embedding,
        top_k=top_k or RETRIEVAL_TOP_K,
        filter_conditions=filter_conditions,
        collection_name=ACTIVE_COLLECTION_NAME,
    )


def get_clause(clause_ref: str) -> dict | None:
    """Fetch one clause's full payload by its exact clause_ref."""
    return get_clause_by_ref(clause_ref, collection_name=ACTIVE_COLLECTION_NAME)


def get_children(clause_ref: str) -> list[dict]:
    """Fetch every clause whose parent_clause points at clause_ref."""
    return _get_children(clause_ref, collection_name=ACTIVE_COLLECTION_NAME)


def get_parent_chain(clause_ref: str, max_depth: int = 10) -> list[dict]:
    """
    Walk UP the hierarchy from clause_ref: parent, grandparent, and so on,
    until there's no parent_clause left (or max_depth is hit as a safety
    stop against a bad/circular parent_clause value in the data).
    Returns the chain ordered from immediate parent to most distant ancestor.
    """
    chain = []
    current = get_clause(clause_ref)
    seen = {clause_ref}

    for _ in range(max_depth):
        if not current:
            break
        parent_ref = current.get("parent_clause")
        if not parent_ref or parent_ref in seen:
            break
        parent = get_clause(parent_ref)
        if not parent:
            break
        chain.append(parent)
        seen.add(parent_ref)
        current = parent

    return chain


# A loose pattern for FINRA-style clause references appearing inside clause
# text, e.g. "Rule 4512", "2111.03", "3110(b)(2)". This is intentionally
# permissive -- false positives just mean lookup_cross_reference() tries a
# get_clause() that returns None and falls back to semantic search anyway.
_CLAUSE_REF_PATTERN = re.compile(r"\b\d{4}(?:\.\d{1,3})?(?:\([a-zA-Z0-9]+\))*\b")


def lookup_cross_reference(reference_text: str) -> list[dict]:
    """
    Given a snippet of clause text that appears to reference another clause
    or rule (e.g. "...subject to the requirements of Rule 4512...", or just
    "4512"), try to resolve it to an actual clause.

    Strategy:
      1. Pull out anything that looks like a clause number and try an exact
         get_clause() lookup for each candidate -- cheap and precise when it
         hits.
      2. If none of those match a real clause_ref, fall back to a semantic
         search using the reference_text itself, so the reasoner still gets
         candidates to look at even when the reference is phrased loosely
         ("the definition of 'institutional account' above") rather than as
         a bare number.

    Returns a list of clause payload dicts (exact hits first, semantic
    fallback hits after, deduplicated by clause_ref).
    """
    exact_hits = []
    seen_refs = set()

    for candidate in _CLAUSE_REF_PATTERN.findall(reference_text):
        if candidate in seen_refs:
            continue
        clause = get_clause(candidate)
        if clause:
            exact_hits.append(clause)
            seen_refs.add(candidate)

    if exact_hits:
        return exact_hits

    # No exact clause number found (or none of them exist) -- fall back to
    # semantic search over the reference text so we still surface *something*
    # for the reasoner to evaluate, rather than silently returning nothing.
    semantic_hits = vector_search(reference_text, top_k=5)
    return [hit["payload"] for hit in semantic_hits if hit["clause_ref"] not in seen_refs]


# ---------------------------------------------------------------------------
# Layer 2: the same functions, exposed as tools the reasoner agent can call
# ---------------------------------------------------------------------------

@tool
def search_clauses_tool(query: str, activity_type: list[str] | None = None,
                         applies_to_firm_type: list[str] | None = None) -> list[dict]:
    """Semantic search over the FINRA clause database. Use this to find
    clauses related to a topic. Optionally narrow results with
    activity_type and/or applies_to_firm_type if you already know them."""
    filters = {}
    if activity_type:
        filters["activity_type"] = activity_type
    if applies_to_firm_type:
        filters["applies_to_firm_type"] = applies_to_firm_type
    return vector_search(query, filter_conditions=filters or None)


@tool
def get_clause_tool(clause_ref: str) -> dict | None:
    """Fetch the full text and metadata of one specific clause, by its exact
    clause_ref. Use this when you know precisely which clause you want."""
    return get_clause(clause_ref)


@tool
def get_children_tool(clause_ref: str) -> list[dict]:
    """Fetch all sub-clauses (children) of a given clause_ref. Use this to
    check whether a clause has more specific sub-provisions worth reading."""
    return get_children(clause_ref)


@tool
def get_parent_chain_tool(clause_ref: str) -> list[dict]:
    """Fetch the parent, grandparent, etc. of a given clause_ref, in order.
    Use this to see the broader obligation a specific clause sits under."""
    return get_parent_chain(clause_ref)


@tool
def lookup_cross_reference_tool(reference_text: str) -> list[dict]:
    """Resolve a lateral reference found INSIDE a clause's text (e.g. a
    clause that says 'see Rule 4512' or 'as defined above') to the actual
    clause(s) it points to. Pass the exact snippet of text containing the
    reference."""
    return lookup_cross_reference(reference_text)


REASONER_TOOLS = [
    search_clauses_tool,
    get_clause_tool,
    get_children_tool,
    get_parent_chain_tool,
    lookup_cross_reference_tool,
]
