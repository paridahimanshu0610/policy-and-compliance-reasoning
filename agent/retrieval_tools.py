"""
agent/retrieval_tools.py

Everything that touches the vector database lives here, in two layers:

1. Plain Python functions (clause_search, get_clause, get_children,
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
from typing import List, Dict, Any, Union
import json
from langchain_core.tools import tool

from config.settings import NORMALIZED_CHECKPOINT, ACTIVE_COLLECTION_NAME, ACTIVE_EMBEDDING_MODEL, RETRIEVAL_TOP_K
from ingestion.build_vector_db import search_clauses, get_clause_by_ref, get_children as _get_children, generate_query_embeddings


# ---------------------------------------------------------------------------
# Layer 1: plain functions, used by the deterministic nodes
# ---------------------------------------------------------------------------

def clause_search(
        query_text: str, 
        filter_conditions: dict | None = None, 
        top_k: int | None = None, 
        search_mode: str = "dense",  # "dense" | "sparse"
) -> list[dict]:
    """
    Embed query_text with the active embedding model and search the active
    collection, optionally narrowed by filter_conditions (same shape as
    ingestion.build_vector_db.search_clauses expects).
    """
    if search_mode not in ("dense", "sparse"):
        raise ValueError(f"search_mode must be 'dense' or 'sparse', got: {search_mode!r}")
    
    print(f"Opted for {search_mode} search mode...")
    if search_mode == "dense":
        embedding = generate_query_embeddings(query_text, ACTIVE_EMBEDDING_MODEL)
        print("Generated embedding for the query text, now searching the closest clause...")
        return search_clauses(
            query_embedding=embedding,
            search_mode = "dense",
            top_k=top_k or RETRIEVAL_TOP_K,
            filter_conditions=filter_conditions,
            collection_name=ACTIVE_COLLECTION_NAME,
        )
    else:
        print("Searching the BM25 Index...")
        return search_clauses(
            query_text=query_text,
            search_mode = "sparse",
            top_k=top_k or RETRIEVAL_TOP_K,
            filter_conditions=filter_conditions,
            collection_name=ACTIVE_COLLECTION_NAME,
        )

def filter_hits(
    hits: list[dict],
    filter_conditions: dict | None = None,
) -> list[dict]:
    """
    Apply the same filtering logic used in search_clauses() to an
    existing list of Qdrant search results.

    Semantics:
    - bool filter -> exact match
    - list filter -> MatchAny (intersection is non-empty)
    - scalar filter -> exact match
    """

    if not filter_conditions:
        return hits

    def matches(payload: dict, field: str, filter_value) -> bool:
        payload_value = payload.get(field)

        # MatchValue for booleans
        if isinstance(filter_value, bool):
            return payload_value == filter_value

        # MatchAny
        elif isinstance(filter_value, list):
            if payload_value is None:
                return False

            # Qdrant MatchAny is typically used against payload arrays,
            # but handle scalar payload values defensively.
            if isinstance(payload_value, list):
                return any(v in payload_value for v in filter_value)
            else:
                return payload_value in filter_value

        # MatchValue for scalars
        else:
            return payload_value == filter_value

    filtered_hits = []

    for hit in hits:
        payload = hit.get("payload", {})

        if all(
            matches(payload, field, value)
            for field, value in filter_conditions.items()
        ):
            filtered_hits.append(hit)

    return filtered_hits

def get_clause(clause_ref: Union[str, list[str]],) -> dict | None:
    """Fetch one clause's full payload by its exact clause_ref."""
    return get_clause_by_ref(clause_ref, collection_name=ACTIVE_COLLECTION_NAME)

def _load_jsonl(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

def _tokenize(clause_ref: str):
    """
    Split a clause_ref into (root, tokens) where tokens is the ordered list
    of hierarchical segments, e.g.:
        "FINRA-3110(b)(6)(C)(ii)a.1." ->
        ("FINRA-3110", ["(b)", "(6)", "(C)", "(ii)", "a.", "1."])
    """
    token_pattern = re.compile(r'\([^()]+\)|[A-Za-z0-9]+\.')
    tokens = token_pattern.findall(clause_ref)

    # root = everything before the first matched token
    first_match = token_pattern.search(clause_ref)
    root = clause_ref[:first_match.start()] if first_match else clause_ref

    # sanity check: reconstructing root+tokens should give back the original string
    # (helps catch malformed clause_refs)
    reconstructed = root + "".join(tokens)
    if reconstructed != clause_ref:
        raise ValueError(f"Could not fully tokenize clause_ref: {clause_ref!r}")

    return root, tokens


def get_children_clause_ref(
    clause_ref: str,
    include_grandchildren: bool = False,
    include_all_keys: bool = False
) -> List[Dict[str, Any]]:
    """
    Return all dicts in `data` whose clause_ref is a descendant of `clause_ref`.

    - If include_grandchildren is False (default): only immediate children.
    - If True: all descendants at any depth (children, grandchildren, ...).

    Raises ValueError if clause_ref itself isn't found in data (optional safety check
    -- remove if you don't want this).
    """
    try:
        parent_root, parent_tokens = _tokenize(clause_ref)
    except ValueError:
        raise ValueError(f"Could not parse the given clause_ref: {clause_ref!r}")

    data = _load_jsonl(NORMALIZED_CHECKPOINT)
    key = "clause_ref"

    parent_depth = len(parent_tokens)
    results = []

    for item in data:
        candidate = item.get(key)
        if candidate is None or candidate == clause_ref:
            continue
        try:
            cand_root, cand_tokens = _tokenize(candidate)
        except ValueError:
            # skip unparsable entries rather than crashing the whole search
            continue

        if cand_root != parent_root:
            continue
        if len(cand_tokens) <= parent_depth:
            continue
        if cand_tokens[:parent_depth] != parent_tokens:
            continue

        if include_grandchildren:
            results.append(item)
        else:
            if len(cand_tokens) == parent_depth + 1:
                results.append(item)

    if not include_all_keys:
        return [clause[key] for clause in results]
        
    return results

def get_children(clause_ref: str, include_grandchildren: bool = False) -> list[dict]:
    """Fetch every clause whose parent_clause points at clause_ref."""
    children_clause_refs = get_children_clause_ref(clause_ref, include_grandchildren = include_grandchildren, include_all_keys = False)
    return get_clause(children_clause_refs)


def get_parent(clause_ref: str, include_all_ancestors: bool = False) -> list[dict]:
    """
    Walk UP the hierarchy from clause_ref: parent, grandparent, and so on,
    until there's no parent_clause left (or max_depth is hit as a safety
    stop against a bad/circular parent_clause value in the data).
    Returns the chain ordered from immediate parent to most distant ancestor.
    """
    chain = []
    current = get_clause(clause_ref)
    seen = {clause_ref}
    
    max_depth = 10 if include_all_ancestors else 1

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


# ---------------------------------------------------------------------------
# Layer 2: the same functions, exposed as tools the reasoner agent can call
# ---------------------------------------------------------------------------

@tool
def search_clauses_tool(
    query: str,
    search_mode: str = "dense",  # "dense" | "sparse"
    activity_type: list[str] | None = None,
    applies_to_firm_type: list[str] | None = None,
    documentation_required: bool | None = None,
    frequency: list[str] | None = None,
    has_financial_threshold: bool | None = None,
    involves_customer: bool | None = None,
    involves_third_party: bool | None = None,
    obligated_actor: list[str] | None = None,
    regulated_subject: list[str] | None = None,
    reporting_recipient: list[str] | None = None,
    rule_id: str | None = None,
) -> list[dict]:
    """Search the FINRA clause database for clauses related to a topic.

    Two search modes are available:
    - "dense": semantic/embedding-based search. Best for conceptual or
      paraphrased queries where the exact wording may not match the clause
      text (default).
    - "sparse": BM25 keyword-based search. Best when you need to match
      specific terms, phrases, or exact language likely to appear
      verbatim in the clause text (e.g. defined terms, rule citations).

    Optionally narrow results with any of the metadata filters below.
    Only set a filter if you are confident it applies — do not guess.
    Leaving a filter as None means it is not applied; setting it
    incorrectly can wrongly exclude relevant clauses.
    """
    kwargs = {
        "activity_type": activity_type,
        "applies_to_firm_type": applies_to_firm_type,
        "documentation_required": documentation_required,
        "frequency": frequency,
        "has_financial_threshold": has_financial_threshold,
        "involves_customer": involves_customer,
        "involves_third_party": involves_third_party,
        "obligated_actor": obligated_actor,
        "regulated_subject": regulated_subject,
        "reporting_recipient": reporting_recipient,
        "rule_id": rule_id,
    }
    filters = {k: v for k, v in kwargs.items() if v is not None and v != []}
    return clause_search(query, filter_conditions=filters or None, search_mode=search_mode)


@tool
def get_clause_tool(clause_ref: Union[str, list[str]]) -> dict | None:
    """Fetch the full text and metadata of one clause or a list of clauses, by their 
    exact clause_ref. Use this when you know precisely which clause(s) you want."""
    return get_clause(clause_ref)


@tool
def get_children_tool(clause_ref: str) -> list[dict]:
    """Fetch all sub-clauses (children) of a given clause_ref. Use this to
    check whether a clause has more specific sub-provisions worth reading."""
    return get_children(clause_ref)


@tool
def get_parent_tool(clause_ref: str) -> list[dict]:
    """Fetch the parent of a given clause_ref.
    Use this to see the broader obligation a specific clause sits under."""
    return get_parent(clause_ref)

# If any tool (including the tool name) is updated/added here, update the prompt: REASONER_TOOL_INSTRUCTIONS
REASONER_TOOLS = [
    search_clauses_tool,
    get_clause_tool,
    get_children_tool,
    get_parent_tool,
]
