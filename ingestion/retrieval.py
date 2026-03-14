"""
retrieval.py
============
Step 6 — ChromaDB retrieval layer.

Takes the structured intent JSON produced by the intent pipeline and
returns the most relevant FINRA clause documents from ChromaDB.

The retrieval strategy uses a two-stage approach:
    Stage 1 — Metadata pre-filter: narrows the candidate set using
              the structured fields from the intent JSON. Only fields
              whose values are known with high confidence are used as
              hard filters. This avoids silently excluding relevant
              clauses due to imperfect intent extraction.

    Stage 2 — Semantic search: within the filtered candidate set,
              ChromaDB ranks documents by cosine similarity against a
              natural language query string built from the intent fields.
              The merged clause text (used at embedding time) makes this
              search semantically rich even for fragment clauses.

Usage:
    from retrieval import load_collection, retrieve_clauses

    collection = load_collection()

    intent = {
        "activity_type":        "supervision",
        "category":             "supervision",
        "obligated_actor":      "member",
        "involves_customer":    False,
        "involves_third_party": False,
        ...
    }

    results = retrieve_clauses(intent, collection)
    for r in results:
        print(r["clause_ref"], r["activity_type"])
        print(r["document"])
        print()
"""

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# ── Configuration ─────────────────────────────────────────────────────────────

CHROMA_PATH      = "data/chromadb"
COLLECTION_NAME  = "finra_clauses"
EMBEDDING_MODEL  = "all-MiniLM-L6-v2"
DEFAULT_TOP_K    = 10

# Fields that carry enough discriminating power to use as hard filters.
# Fields not in this list are used only for query construction, not filtering.
# The guiding principle: a wrong filter silently excludes correct clauses.
# A missing filter only slightly widens the candidate set.
# When in doubt, leave a field out of the filter and let semantic search
# rank it appropriately.
FILTERABLE_STRING_FIELDS = {
    "category",
    "activity_type",
    "obligated_actor",
    "regulated_subject",
}

FILTERABLE_BOOL_FIELDS = {
    "involves_customer",
    "involves_third_party",
    "has_financial_threshold",
    "documentation_required",
}


# ── Collection Loader ─────────────────────────────────────────────────────────

def load_collection(
    chroma_path:     str = CHROMA_PATH,
    collection_name: str = COLLECTION_NAME,
    embedding_model: str = EMBEDDING_MODEL,
) -> chromadb.Collection:
    """
    Opens the persistent ChromaDB client and returns the clause collection.

    Raises FileNotFoundError if the ChromaDB path does not exist, which
    means the knowledge base has not been built yet.

    Parameters
    ----------
    chroma_path     : directory where ChromaDB stores its files
    collection_name : name of the clause collection
    embedding_model : sentence-transformer model name for embeddings

    Returns
    -------
    A chromadb.Collection instance ready for querying.
    """
    from pathlib import Path

    if not Path(chroma_path).exists():
        raise FileNotFoundError(
            f"ChromaDB path not found: {chroma_path}\n"
            "Run build_knowledge_base.py first to construct the knowledge base."
        )

    client = chromadb.PersistentClient(path=chroma_path)
    ef     = SentenceTransformerEmbeddingFunction(model_name=embedding_model)

    collection = client.get_collection(
        name               = collection_name,
        embedding_function = ef,
    )
    return collection


# ── Filter Builder ────────────────────────────────────────────────────────────

def _build_where_filter(intent: dict) -> dict | None:
    """
    Constructs a ChromaDB `where` filter dict from the intent JSON.

    FILTER STRATEGY
    ───────────────
    String fields (category, activity_type, obligated_actor,
    regulated_subject):
        Include in filter ONLY if the value is a non-empty string.
        A null or empty string means the intent pipeline was not
        confident — widening the search is safer than excluding.

    Boolean fields (involves_customer, involves_third_party,
    has_financial_threshold, documentation_required):
        Include in filter ONLY if the value is True.
        Reasoning: if the intent says involves_customer=True, we know
        the situation involves a customer and want clauses that do too.
        If involves_customer=False, we want all clauses regardless —
        a purely internal query is still governed by many clauses that
        happen to also cover customer situations.

    Multiple conditions are joined with $and. If only one condition
    exists, it is returned without wrapping in $and (ChromaDB requires
    $and to contain at least 2 conditions).

    Returns None if no filterable fields are present, which causes the
    caller to run an unfiltered semantic search over the full collection.

    Parameters
    ----------
    intent : structured intent dict from extract_structured_intent

    Returns
    -------
    ChromaDB where-filter dict, or None for an unfiltered search.
    """
    conditions: list[dict] = []

    # String fields — filter only when the value is a known, non-empty string
    for field in FILTERABLE_STRING_FIELDS:
        value = intent.get(field)
        if value and isinstance(value, str) and value.strip():
            conditions.append({field: {"$eq": value.strip()}})

    # Boolean fields — filter only when the intent says True
    for field in FILTERABLE_BOOL_FIELDS:
        value = intent.get(field)
        if value is True:
            conditions.append({field: {"$eq": True}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


# ── Query String Builder ──────────────────────────────────────────────────────

def _build_query_string(intent: dict) -> str:
    """
    Constructs a natural language query string for semantic similarity
    search from the intent JSON.

    The query is built from the most semantically expressive fields:
    activity_type, regulated_subject, subject_matter, and the raw
    situation summary if present. This gives the embedding model
    enough signal to rank clause documents meaningfully within the
    filtered candidate set.

    The query is deliberately human-readable so that the same
    sentence-transformer embedding space is used consistently between
    query time and the merged clause text stored at ingestion time.

    Parameters
    ----------
    intent : structured intent dict from extract_structured_intent

    Returns
    -------
    A natural language query string. Never empty — falls back to
    "FINRA compliance obligation" if no fields are usable.
    """
    parts: list[str] = []

    activity = intent.get("activity_type", "")
    if activity:
        # Convert underscore-separated activity names to readable phrases
        parts.append(activity.replace("_", " "))

    subject = intent.get("regulated_subject", "")
    if subject:
        parts.append(subject.replace("_", " "))

    obligated = intent.get("obligated_actor", "")
    if obligated:
        parts.append(f"{obligated.replace('_', ' ')} obligation")

    # subject_matter tags are comma-separated strings stored from ingestion;
    # at query time, intent["subject_matter"] is a list from the LLM output.
    subject_matter = intent.get("subject_matter", [])
    if isinstance(subject_matter, list) and subject_matter:
        parts.extend(tag.replace("_", " ") for tag in subject_matter[:4])
    elif isinstance(subject_matter, str) and subject_matter:
        parts.append(subject_matter.replace("_", " "))

    # The situation summary is the richest semantic signal available.
    # If the caller passes it through in the intent dict (optional), use it.
    summary = intent.get("situation_summary", "")
    if summary:
        parts.append(summary)

    if not parts:
        return "FINRA compliance obligation"

    return ". ".join(parts)


# ── Post-retrieval Helpers ────────────────────────────────────────────────────

def _firm_type_matches(doc_meta: dict, intent_firm_types: list[str]) -> bool:
    """
    Checks whether the document's applies_to_firm_type field overlaps
    with any of the firm types in the intent.

    The field is stored as a comma-joined string (e.g. "broker_dealer").
    This helper splits it and checks for any intersection.

    Used as a soft post-filter — documents that fail this check are
    deprioritised rather than hard-excluded, since "broker_dealer" is
    the default and covers most cases.

    Parameters
    ----------
    doc_meta         : metadata dict for a single retrieved document
    intent_firm_types: list of firm type strings from the intent JSON

    Returns
    -------
    True if there is any overlap, or if no firm type filter is active.
    """
    if not intent_firm_types:
        return True

    stored = doc_meta.get("applies_to_firm_type", "")
    if not stored:
        return True   # no firm type stored — do not exclude

    stored_types = {t.strip() for t in stored.split(",")}
    return bool(stored_types & set(intent_firm_types))


def _format_results(
    query_result: dict,
    intent:       dict,
) -> list[dict]:
    """
    Converts the raw ChromaDB query result into a clean list of
    result dicts, each combining document text, metadata, and distance.

    Applies firm-type soft-filtering: documents whose applies_to_firm_type
    does not overlap with the intent are moved to the end of the list
    rather than removed, preserving retrieval completeness while
    surfacing the most relevant results first.

    Parameters
    ----------
    query_result : raw dict returned by collection.query()
    intent       : structured intent dict (used for firm-type soft-filter)

    Returns
    -------
    List of result dicts, each with keys:
        clause_ref, document, distance, and all metadata fields.
    """
    intent_firm_types = intent.get("applies_to_firm_type", ["broker_dealer"])
    if isinstance(intent_firm_types, str):
        intent_firm_types = [t.strip() for t in intent_firm_types.split(",")]

    primary:   list[dict] = []
    secondary: list[dict] = []

    ids        = query_result["ids"][0]
    documents  = query_result["documents"][0]
    metadatas  = query_result["metadatas"][0]
    distances  = query_result["distances"][0]

    for doc_id, doc_text, meta, dist in zip(ids, documents, metadatas, distances):
        result = {
            "clause_ref": doc_id,
            "document":   doc_text,
            "distance":   round(dist, 4),
            **meta,
        }
        if _firm_type_matches(meta, intent_firm_types):
            primary.append(result)
        else:
            secondary.append(result)

    return primary + secondary


# ── Main Retrieval Function ───────────────────────────────────────────────────

def retrieve_clauses(
    intent:     dict,
    collection: chromadb.Collection,
    top_k:      int = DEFAULT_TOP_K,
) -> list[dict]:
    """
    Retrieves the most relevant FINRA clause documents for a given intent.

    Runs a two-stage retrieval:
        1. Builds a metadata where-filter from the intent's structured
           fields. Asks ChromaDB to retrieve top_k * 2 candidates from
           the filtered set to give semantic search more to work with.
        2. Semantic similarity ranking within that candidate set returns
           the final top_k results.

    If the where-filter would produce zero results (e.g. because the
    activity_type or category has no matches), the function automatically
    falls back to an unfiltered semantic search. This prevents the
    retrieval layer from returning nothing due to overly strict filters.

    Parameters
    ----------
    intent     : structured intent dict from extract_structured_intent.
                 May optionally contain "situation_summary" (str) for
                 richer semantic query construction.
    collection : loaded ChromaDB collection from load_collection()
    top_k      : number of results to return  (default: 10)

    Returns
    -------
    List of result dicts ordered by relevance (primary firm-type match
    first, then secondary). Each dict contains clause_ref, document,
    distance, and all metadata fields.

    Returns an empty list if the collection is empty or all queries fail.
    """
    if collection.count() == 0:
        print("  ✗ Collection is empty. Run build_knowledge_base.py first.")
        return []

    where_filter = _build_where_filter(intent)
    query_string = _build_query_string(intent)

    print(f"\n  Query: \"{query_string[:80]}{'...' if len(query_string) > 80 else ''}\"")
    if where_filter:
        print(f"  Filter: {where_filter}")
    else:
        print("  Filter: none (unfiltered semantic search)")

    # ── Attempt 1: Filtered search ────────────────────────────────────────
    if where_filter:
        try:
            raw = collection.query(
                query_texts  = [query_string],
                where        = where_filter,
                n_results    = min(top_k * 2, collection.count()),
                include      = ["documents", "metadatas", "distances"],
            )

            results = _format_results(raw, intent)

            if results:
                print(f"  ✓ Retrieved {len(results)} candidates "
                      f"(filtered). Returning top {min(top_k, len(results))}.")
                return results[:top_k]
            else:
                print("  ⚠ Filtered search returned no results. "
                      "Falling back to unfiltered search.")

        except Exception as e:
            print(f"  ⚠ Filtered search failed ({e}). "
                  "Falling back to unfiltered search.")

    # ── Attempt 2: Unfiltered semantic search fallback ────────────────────
    try:
        raw = collection.query(
            query_texts = [query_string],
            n_results   = min(top_k, collection.count()),
            include     = ["documents", "metadatas", "distances"],
        )
        results = _format_results(raw, intent)
        print(f"  ✓ Retrieved {len(results)} results (unfiltered fallback).")
        return results

    except Exception as e:
        print(f"  ✗ Unfiltered search also failed: {e}")
        return []


# ── Entry Point (Quick Test) ──────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Quick smoke-test: load the collection and run a sample intent query.
    Replace the sample intent below with any valid intent JSON to test
    retrieval before integrating with the full pipeline.
    """

    print("Loading collection...")
    col = load_collection()
    print(f"  ✓ Collection loaded  ({col.count()} documents)\n")

    sample_intent = {
        "activity_type":        "supervision",
        "category":             "supervision",
        "obligated_actor":      "member",
        "regulated_subject":    "written_procedures",
        "applies_to_firm_type": ["broker_dealer"],
        "involves_customer":    False,
        "involves_third_party": False,
        "has_financial_threshold": False,
        "documentation_required":  True,
        "frequency":            "ongoing",
        "reporting_recipient":  None,
        "subject_matter": [
            "written_procedures",
            "supervisory_system",
            "supervision",
            "establishment_maintenance",
        ],
        # Optional: include the situation summary for richer semantic search
        "situation_summary": (
            "A broker-dealer member is seeking to determine whether it is "
            "required to establish and maintain written supervisory procedures "
            "for its associated persons."
        ),
    }

    print("Running retrieval with sample intent...")
    results = retrieve_clauses(sample_intent, col, top_k=5)

    print(f"\n{'=' * 60}")
    print(f"TOP {len(results)} RESULTS")
    print(f"{'=' * 60}")

    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r['clause_ref']}  (distance: {r['distance']})")
        print(f"     activity_type    : {r.get('activity_type')}")
        print(f"     obligated_actor  : {r.get('obligated_actor')}")
        print(f"     involves_customer: {r.get('involves_customer')}")
        print(f"     document preview : {r['document'][:200]}...")
