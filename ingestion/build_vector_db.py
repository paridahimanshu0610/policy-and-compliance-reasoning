"""
FINRA clauses Qdrant setup and search utilities.

Run as a script:
    python finra_qdrant_setup.py

Import as a module:
    from finra_qdrant_setup import client, search_clauses, upsert_clauses, main
"""

import json
import time
import uuid
from typing import Any, Union
from .embedders import get_embedder

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    HnswConfigDiff,
    OptimizersConfigDiff,
    PayloadSchemaType,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    SparseVectorParams,
    SparseVector,
    Modifier,
)
from fastembed import SparseTextEmbedding

import os
from dotenv import load_dotenv
import voyageai
from config.settings import DATA_DIR

load_dotenv()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


# Voyage-law-2: Embedding dimension = 1024 | Collection name = "voyage_embedded_clauses"
# Euler-Legal-Embedding-V1: Embedding dimension = 4096 | Collection name = "euler_embedded_clauses"
# text-embedding-3-small: Embedding dimension = 1536 | Collection name = "text_embedded_clauses"
# Octen-Embedding-8B: Embedding dimension = 4096 | Collection name = "octen_embedded_clauses"
# Qwen3-Embedding-8B: Embedding dimension = 4096 | Collection name = "qwen_embedded_clauses"
COLLECTION_NAME = "voyage_embedded_clauses_new"  # Qdrant collection name for FINRA clauses
VECTOR_DIM = 1024

# Name of the dense vector on each point (now required since we're adding a
# second, named sparse vector alongside it — Qdrant doesn't allow mixing an
# unnamed default vector with named vectors on the same point).
DENSE_VECTOR_NAME = "dense"

# Name of the BM25 sparse vector computed over `original_clause`.
BM25_VECTOR_NAME = "bm25"
BM25_MODEL_NAME = "Qdrant/bm25"

# Loaded once at import time; reused across all build_point calls.
_bm25_model = SparseTextEmbedding(model_name=BM25_MODEL_NAME)

KEYWORD_FIELDS = [
    "clause_ref",
    "rule_id",
    "rule_category",
    "parent_clause",
    "obligated_actor",       # Qdrant handles keyword[] with the same index type
    "regulated_subject",
    "activity_type",
    "frequency",
    "reporting_recipient",
    "applies_to_firm_type",
]

BOOL_FIELDS = [
    "involves_customer",
    "involves_third_party",
    "has_financial_threshold",
    "documentation_required",
]

# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

client = QdrantClient(host="localhost", port=6333)


# --------------------------------------------------------------------------
# Collection / index setup (idempotent)
# --------------------------------------------------------------------------

def collection_exists(collection_name: str = COLLECTION_NAME) -> bool:
    """Check whether the collection already exists."""
    return client.collection_exists(collection_name=collection_name)


def create_collection(collection_name: str = COLLECTION_NAME) -> None:
    """
    Create the collection only if it doesn't already exist.
    Does NOT recreate/drop an existing collection, so existing data is preserved.
    """
    if collection_exists(collection_name):
        print(f"Collection '{collection_name}' already exists. Skipping creation.")
        return

    client.create_collection(
        collection_name=collection_name,
        vectors_config={
            DENSE_VECTOR_NAME: VectorParams(
                size=VECTOR_DIM,
                distance=Distance.COSINE,  # Best for normalized text embeddings
            ),
        },
        sparse_vectors_config={
            BM25_VECTOR_NAME: SparseVectorParams(
                modifier=Modifier.IDF,  # Enables BM25's IDF term
            ),
        },
        hnsw_config=HnswConfigDiff(
            m=16,                      # Fine for now; bump to 32 at 50k if recall dips
            ef_construct=200,          # Higher than default for better index quality
            full_scan_threshold=10000  # Below 10k vectors, use exact search (100% recall)
                                       # Your 1300 clauses will always exact-search
        ),
        optimizers_config=OptimizersConfigDiff(
            indexing_threshold=20000  # Don't build HNSW until you exceed 20k vectors
                                       # Below this, exact search is faster anyway
        ),
    )
    print(f"Collection '{collection_name}' created.")


def create_payload_indexes(collection_name: str = COLLECTION_NAME) -> None:
    """
    Create keyword and boolean payload indexes.
    Safe to call repeatedly: creating an index that already exists is a no-op
    in Qdrant (it will just confirm the existing schema), so no existence
    check is required here.
    """
    for field in KEYWORD_FIELDS:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
        )

    for field in BOOL_FIELDS:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field,
            field_schema=PayloadSchemaType.BOOL,
        )

    print("Payload indexes created.")


def setup_database(collection_name: str = COLLECTION_NAME) -> None:
    """
    Ensure the collection and its payload indexes exist.
    Only creates the collection if it doesn't already exist; never drops
    or overwrites existing data.
    """
    was_new = not collection_exists(collection_name)
    create_collection(collection_name)
    if was_new:
        # Only need to (re)create indexes when the collection is freshly made.
        # If it already existed, indexes were presumably created previously,
        # but create_payload_index is idempotent, so calling it again is safe too.
        create_payload_indexes(collection_name)
    else:
        create_payload_indexes(collection_name)


# --------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------

def to_list(value: Any) -> list:
    """Normalize a field that can be str, list, or None to always be a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]  # single string -> wrap in list


def to_str(value: Any) -> str:
    """Normalize a field that can be str or None to always be a string."""
    if value is None:
        return ""
    return value


def clause_ref_to_uuid(clause_ref: str) -> str:
    """Derive a stable UUID from clause_ref using UUID5 (SHA-1 namespace hash)."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, clause_ref))


def compute_bm25_sparse_vector(text: str) -> SparseVector:
    """Compute a BM25 sparse vector for the given text using fastembed."""
    embedding = list(_bm25_model.embed([text]))[0]
    return SparseVector(
        indices=embedding.indices.tolist(),
        values=embedding.values.tolist(),
    )


# --------------------------------------------------------------------------
# Point construction
# --------------------------------------------------------------------------

def build_point(clause: dict) -> PointStruct:
    """Convert a raw clause dict into a Qdrant PointStruct."""
    original_clause = to_str(clause.get("original_clause"))
    return PointStruct(
        id=clause_ref_to_uuid(clause["clause_ref"]),
        vector={
            DENSE_VECTOR_NAME: clause["embedding"],
            BM25_VECTOR_NAME: compute_bm25_sparse_vector(original_clause),
        },
        payload={
            # String fields
            "clause_ref":   clause["clause_ref"],
            "rule_id":      clause["rule_id"],
            "rule_name":    clause["rule_name"],
            "rule_category": clause["rule_category"],
            "rule_url":     clause["rule_url"],
            "parent_clause": to_str(clause.get("parent_clause")),
            "original_clause": original_clause,
            "merged_clause":   to_str(clause.get("merged_clause")),
            # List fields — always normalized
            "obligated_actor":      to_list(clause.get("obligated_actor")),
            "regulated_subject":    to_list(clause.get("regulated_subject")),
            "activity_type":        to_list(clause.get("activity_type")),
            "frequency":            to_list(clause.get("frequency")),
            "reporting_recipient":  to_list(clause.get("reporting_recipient")),
            "applies_to_firm_type": to_list(clause.get("applies_to_firm_type")),
            # Boolean fields
            "involves_customer":        bool(clause["involves_customer"]),
            "involves_third_party":     bool(clause["involves_third_party"]),
            "has_financial_threshold":  bool(clause["has_financial_threshold"]),
            "documentation_required":   bool(clause["documentation_required"]),
        }
    )


# --------------------------------------------------------------------------
# Upsert (additive: inserts new clauses, updates existing ones by clause_ref)
# --------------------------------------------------------------------------

def upsert_clauses(
    clauses: list[dict],
    collection_name: str = COLLECTION_NAME,
    batch_size: int = 100,
) -> None:
    """
    Upsert clauses in batches.

    This is additive, not destructive: Qdrant's upsert inserts any point
    whose id is not yet present, and only overwrites a point when its id
    already exists. Since each point's id is derived deterministically from
    `clause_ref` (via UUID5), calling this repeatedly with new clauses adds
    them to the existing collection rather than wiping it out. Passing a
    clause with a `clause_ref` that already exists will update that specific
    point in place, leaving all other points untouched.
    """
    for i in range(0, len(clauses), batch_size):
        batch = clauses[i : i + batch_size]
        points = [build_point(c) for c in batch]
        client.upsert(
            collection_name=collection_name,
            points=points,
            wait=True,  # Confirm write before returning
        )
        print(f"Upserted batch {i // batch_size + 1} ({len(points)} points)")


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def search_clauses(
    query_embedding: list[float] | None = None,
    query_text: str | None = None,
    search_mode: str = "dense",  # "dense" | "sparse"
    top_k: int = 20,
    filter_conditions: dict = None,
    collection_name: str = COLLECTION_NAME,
) -> list[dict]:
    """
    Search for clauses using either dense (semantic) or sparse (BM25 keyword)
    similarity, with optional metadata filters.

    search_mode:
        "dense"  -> requires query_embedding, searches the DENSE_VECTOR_NAME vector.
        "sparse" -> requires query_text, computes a BM25 sparse vector and
                    searches the BM25_VECTOR_NAME vector.

    filter_conditions example:
    {
        "involves_customer": True,
        "activity_type": ["pay_to_play", "solicitation"],
        "applies_to_firm_type": ["broker_dealer"],
    }
    """
    if search_mode not in ("dense", "sparse"):
        raise ValueError(f"search_mode must be 'dense' or 'sparse', got: {search_mode!r}")

    print("search_mode: ", search_mode)
    if search_mode == "dense":
        if query_embedding is None:
            raise ValueError("query_embedding is required when search_mode='dense'")
        query = query_embedding
        using = DENSE_VECTOR_NAME
    else:
        if query_text is None:
            raise ValueError("query_text is required when search_mode='sparse'")
        query = compute_bm25_sparse_vector(query_text)
        using = BM25_VECTOR_NAME

    must_conditions = []

    if filter_conditions:
        for field, value in filter_conditions.items():
            if isinstance(value, bool):
                must_conditions.append(
                    FieldCondition(key=field, match=MatchValue(value=value))
                )
            elif isinstance(value, list):
                must_conditions.append(
                    FieldCondition(key=field, match=MatchAny(any=value))
                )
            else:
                must_conditions.append(
                    FieldCondition(key=field, match=MatchValue(value=value))
                )

    search_filter = Filter(must=must_conditions) if must_conditions else None

    response = client.query_points(
        collection_name=collection_name,
        query=query,
        using=using,
        query_filter=search_filter,
        limit=top_k,
        with_payload=True,
    )

    return [
        {
            "clause_ref": r.payload["clause_ref"],
            "score": r.score,
            "payload": {k: v for k, v in r.payload.items() if k != "original_clause"},
        }
        for r in response.points
    ]

def get_clause_by_ref(
    clause_ref: Union[str, list[str]],
    collection_name: str = COLLECTION_NAME,
) -> Union[dict, list[dict | None], None]:
    """
    Fetch one or more clauses' full payloads directly by clause_ref -- no
    vector search involved. Use this when you already know exactly which
    clause(s) you want, e.g. while walking up a parent chain, fetching
    children identified by a prior lookup, or resolving a batch of
    cross-references at once.

    - If `clause_ref` is a single string: returns that clause's payload dict,
      or None if it doesn't exist in the collection.
    - If `clause_ref` is a list of strings: returns a list of payload dicts
      in the same order as the input, with None in place of any clause_ref
      that wasn't found.
    """
    is_single = isinstance(clause_ref, str)
    refs = [clause_ref] if is_single else clause_ref

    if not refs:
        return [] if not is_single else None

    id_to_ref = {clause_ref_to_uuid(ref): ref for ref in refs}
    point_ids = list(id_to_ref.keys())

    results = client.retrieve(
        collection_name=collection_name,
        ids=point_ids,
        with_payload=True,
    )

    # Map found results back to their clause_ref, since Qdrant may return
    # fewer points than requested (missing ids are simply omitted) and
    # doesn't guarantee input order.
    found_by_id = {str(point.id): point.payload for point in results}

    payloads = [
        found_by_id.get(str(pid))
        for pid, ref in id_to_ref.items()
    ]
    # Reorder to match original `refs` order (dict preserves insertion order
    # from `refs`, so this is already aligned, but being explicit below).
    ref_to_payload = dict(zip(id_to_ref.values(), payloads))
    ordered_payloads = [ref_to_payload[ref] for ref in refs]

    # Removing "original_clause" key
    for clause in ordered_payloads:
        if isinstance(clause, dict) and "original_clause" in clause: 
            del clause["original_clause"]

    if is_single:
        return ordered_payloads[0]
    return ordered_payloads
 
 
def get_children(
    parent_clause_ref: str,
    collection_name: str = COLLECTION_NAME,
    limit: int = 100,
) -> list[dict]:
    """
    Find every clause whose `parent_clause` field points at parent_clause_ref.
 
    Only the child -> parent link is stored on each clause (parent_clause),
    there's no reverse "children" list stored anywhere. So walking DOWN the
    hierarchy (parent -> children) means filtering: "give me every clause
    whose parent_clause equals this ref". That's what this does.
    """
    results, _next_offset = client.scroll(
        collection_name=collection_name,
        scroll_filter=Filter(
            must=[FieldCondition(key="parent_clause", match=MatchValue(value=parent_clause_ref))]
        ),
        with_payload=True,
        limit=limit,
    )
    return [r.payload for r in results]


def generate_query_embeddings(
    query: str | list[str],
    model_name: str,
) -> list[float] | list[list[float]]:
    """
    Generate query embedding(s) using the specified model.

    Accepts a single query string or a list of query strings.
    Returns a single embedding (list[float]) for a single string input,
    or a list of embeddings (list[list[float]]) for a list input.

    Always uses input_type="query" — several models (Voyage, Qwen3-Embedding)
    apply different transformations for queries vs documents (e.g. an
    instruction prefix). Never use "document" here, or retrieval quality will
    silently degrade even though nothing errors.

    Args:
        query: A single query string or a list of query strings.
        model_name: Which embedding model to use — MUST match the model used
            to embed the corresponding documents in Qdrant. Embeddings from
            different models live in different vector spaces and are not
            comparable, so querying a "voyage-law-2" collection with a
            "text-embedding-3-small" query vector will silently return
            meaningless results rather than erroring.

    Returns:
        A single embedding if input was a string.
        A list of embeddings if input was a list.

    Usage:
        # Single query → pass directly to search_clauses
        vector = generate_query_embeddings(
            "suitability obligations for broker dealers", "voyage-law-2"
        )
        results = search_clauses(query_embedding=vector, top_k=10)

        # Batch queries → each embedding passed separately to search_clauses
        vectors = generate_query_embeddings(
            ["suitability obligations for broker dealers", "front running restrictions"],
            "voyage-law-2",
        )
        for vector in vectors:
            results = search_clauses(query_embedding=vector, top_k=10)
    """
    is_single = isinstance(query, str)
    texts = [query] if is_single else query

    if not texts:
        raise ValueError("Query input must not be empty.")

    if any(not t.strip() for t in texts):
        raise ValueError("Query input must not contain empty or whitespace-only strings.")

    embedder = get_embedder(model_name)
    embeddings = embedder.embed(texts, input_type="query")

    return embeddings[0] if is_single else embeddings

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    """
    Entry point: ensures the collection + indexes exist (without wiping
    existing data), then is ready for upsert_clauses / search_clauses calls.
    """
    setup_database(COLLECTION_NAME)

    # Usage example (uncomment and provide real data/embeddings):
    
    with open(DATA_DIR / "embedded_clauses" / "finra_clauses_embedded__voyage-law-2.jsonl", "r") as f:
        new_clauses = [json.loads(line) for line in f]
    upsert_clauses(new_clauses)
    
    # with open("/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/data/evals/situation1/2000_sit1_eval_cases.jsonl", "r") as f:
    #     sit2000 = [json.loads(line) for line in f]


    # rank_in_top_k = []
    # for idx in range(len(sit2000)):
    #     query = sit2000[idx]["expected_situation_summary"]
    #     expected_clause = sit2000[idx]['ground_truth_clauses'][0]['clause_ref']

    #     my_query_vector = generate_query_embeddings(query, model_name="Octen/Octen-Embedding-8B")
    #     results = search_clauses(
    #         query_embedding=my_query_vector,
    #         top_k=10,
    #     )
    #     rank = next((i + 1 for i, r in enumerate(results) if r['clause_ref'] == expected_clause), None)
    #     print(f"Query {idx + 1}: Expected clause '{expected_clause}' found at rank {rank} in top 10 results.")
    #     rank_in_top_k.append(rank)
    #     time.sleep(25)  # Optional: small delay to avoid overwhelming the server
    
    # print(f"Rank of expected clause in top 10 results for each query: {rank_in_top_k}")

    # idx = 3
    # query = sit2000[idx]["expected_situation_summary"]
    # expected_clause = sit2000[idx]['ground_truth_clauses'][0]['clause_ref']

    # my_query_vector = generate_query_embeddings(query, model_name="Octen/Octen-Embedding-8B")
    # results = search_clauses(
    #     query_embedding=my_query_vector,
    #     top_k=5,
    #     # filter_conditions={
    #     #     "involves_customer": True,
    #     #     "applies_to_firm_type": ["broker_dealer"],
    #     #     "activity_type": ["pay_to_play"],
    #     # }
    # )
    # print(f"Expected clause: {expected_clause}")
    # print(results)


if __name__ == "__main__":
    main()