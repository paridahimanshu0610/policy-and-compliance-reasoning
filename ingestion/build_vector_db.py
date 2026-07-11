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
from typing import Any
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
)

import os
from dotenv import load_dotenv
import voyageai

load_dotenv()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


# Voyage-law-2: Embedding dimension = 1024 | Collection name = "voyage_embedded_clauses"
# Euler-Legal-Embedding-V1: Embedding dimension = 4096 | Collection name = "euler_embedded_clauses"
# text-embedding-3-small: Embedding dimension = 1536 | Collection name = "text_embedded_clauses"
# Octen-Embedding-8B: Embedding dimension = 4096 | Collection name = "octen_embedded_clauses"
# Qwen3-Embedding-8B: Embedding dimension = 4096 | Collection name = "qwen_embedded_clauses"
COLLECTION_NAME = "voyage_embedded_clauses"  # Qdrant collection name for FINRA clauses
VECTOR_DIM = 1024

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
        vectors_config=VectorParams(
            size=VECTOR_DIM,
            distance=Distance.COSINE,  # Best for normalized text embeddings
        ),
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


# --------------------------------------------------------------------------
# Point construction
# --------------------------------------------------------------------------

def build_point(clause: dict) -> PointStruct:
    """Convert a raw clause dict into a Qdrant PointStruct."""
    return PointStruct(
        id=clause_ref_to_uuid(clause["clause_ref"]),
        vector=clause["embedding"],
        payload={
            # String fields
            "clause_ref":   clause["clause_ref"],
            "rule_id":      clause["rule_id"],
            "rule_name":    clause["rule_name"],
            "rule_category": clause["rule_category"],
            "rule_url":     clause["rule_url"],
            "parent_clause": to_str(clause.get("parent_clause")),
            "original_clause": to_str(clause.get("original_clause")),
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
    query_embedding: list[float],
    top_k: int = 20,
    filter_conditions: dict = None,
    collection_name: str = COLLECTION_NAME,
) -> list[dict]:
    """
    Search for clauses using vector similarity with optional metadata filters.
 
    filter_conditions example:
    {
        "involves_customer": True,
        "activity_type": ["pay_to_play", "solicitation"],
        "applies_to_firm_type": ["broker_dealer"],
    }
    """
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
        query=query_embedding,
        query_filter=search_filter,
        limit=top_k,
        with_payload=True,
    )
 
    return [
        {
            "clause_ref": r.payload["clause_ref"],
            "score": r.score,
            "payload": r.payload,
        }
        for r in response.points
    ]

def get_clause_by_ref(
    clause_ref: str,
    collection_name: str = COLLECTION_NAME,
) -> dict | None:
    """
    Fetch one clause's full payload directly by its clause_ref -- no vector
    search involved. Use this when you already know exactly which clause you
    want, e.g. while walking up a parent chain, or fetching a clause that a
    cross-reference lookup identified by name.
 
    Returns None if no clause with that clause_ref exists in the collection.
    """
    point_id = clause_ref_to_uuid(clause_ref)
    results = client.retrieve(
        collection_name=collection_name,
        ids=[point_id],
        with_payload=True,
    )
    if not results:
        return None
    return results[0].payload
 
 
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
# Embedding Evaluation
# --------------------------------------------------------------------------
import time
from collections import defaultdict


# -----------------------------------------------------------------------
# STEP 1: Run retrieval once per query, capture ranks of ALL relevant clauses
# -----------------------------------------------------------------------
def run_retrieval(eval_data, top_k=20, sleep_seconds=0, model_name="Mira190/Euler-Legal-Embedding-V1"):
    """
    Runs retrieval for every query in eval_data and records the rank
    (1-indexed) at which each relevant clause was found within top_k.
    If a relevant clause was not found in top_k, its rank is recorded as None.

    Returns a list of dicts, one per query:
        {
            "query_idx": int,
            "situation_summary": str,
            "relevant_clauses": [clause_ref, ...],
            "ranks": [rank_or_None, ...]   # same order as relevant_clauses
        }
    """
    records = []

    for idx in range(len(eval_data)):
        query = eval_data[idx]["expected_situation_summary"]
        relevant_clauses = [
            gt["clause_ref"] for gt in eval_data[idx]["ground_truth_clauses"]
        ]

        query_vector = generate_query_embeddings(query, model_name)
        results = search_clauses(query_embedding=query_vector, top_k=top_k)
        retrieved_refs = [r["clause_ref"] for r in results]

        ranks = []
        for clause_ref in relevant_clauses:
            if clause_ref in retrieved_refs:
                rank = retrieved_refs.index(clause_ref) + 1  # 1-indexed
            else:
                rank = None
            ranks.append(rank)

        records.append({
            "query_idx": idx,
            "situation_summary": query,
            "relevant_clauses": relevant_clauses,
            "ranks": ranks,
        })

        print(f"Query {idx + 1}/{len(eval_data)}: "
              f"{len(relevant_clauses)} relevant clause(s), ranks found = {ranks}")

        if sleep_seconds:
            time.sleep(sleep_seconds)  # avoid overwhelming the embedding/search server

    return records


# -----------------------------------------------------------------------
# STEP 2: Individual metric functions (operate on a single query's ranks)
# -----------------------------------------------------------------------
def average_precision(ranks):
    """
    Computes Average Precision (AP) for a single query.
    ranks: list of ranks (1-indexed) at which each relevant clause was found,
           or None if not found within top_k.
    """
    R = len(ranks)
    if R == 0:
        return None  # no relevant clauses defined for this query — skip

    found_ranks = sorted([r for r in ranks if r is not None])
    if not found_ranks:
        return 0.0  # none of the relevant clauses were retrieved at all

    ap_sum = 0.0
    for i, rank in enumerate(found_ranks):
        precision_at_rank = (i + 1) / rank  # (# relevant found so far) / rank
        ap_sum += precision_at_rank

    return ap_sum / R


def r_precision(ranks):
    """
    Computes R-Precision for a single query:
    (# relevant clauses found within the top-R results) / R
    where R = number of relevant clauses for this query.
    """
    R = len(ranks)
    if R == 0:
        return None

    found_within_R = sum(1 for r in ranks if r is not None and r <= R)
    return found_within_R / R


def recall_at_k(ranks, k):
    """
    Computes Recall@k for a single query:
    (# relevant clauses found within top-k) / (total # relevant clauses)
    """
    R = len(ranks)
    if R == 0:
        return None

    found_within_k = sum(1 for r in ranks if r is not None and r <= k)
    return found_within_k / R


# -----------------------------------------------------------------------
# STEP 3: Aggregate everything into a single results dictionary
# -----------------------------------------------------------------------
def aggregate_metrics(records, recall_ks=(10, 20)):
    """
    Takes the per-query records from run_retrieval() and computes:
      1. MAP (mean of per-query AP)
      2. Mean R-Precision
      3. Recall@k for each k in recall_ks
      4. MAP broken down by clause-count subgroup (1, 2, 3, 4+ relevant clauses)

    Returns a dictionary with all results.
    """
    per_query_ap = []
    per_query_rprec = []
    per_query_recall = {k: [] for k in recall_ks}

    # For clause-count subgroup breakdown
    subgroup_aps = defaultdict(list)

    for rec in records:
        ranks = rec["ranks"]
        num_relevant = len(ranks)

        ap = average_precision(ranks)
        rprec = r_precision(ranks)

        if ap is not None:
            per_query_ap.append(ap)
            subgroup_aps[num_relevant].append(ap)
        if rprec is not None:
            per_query_rprec.append(rprec)

        for k in recall_ks:
            rk = recall_at_k(ranks, k)
            if rk is not None:
                per_query_recall[k].append(rk)

    results = {
        "MAP": sum(per_query_ap) / len(per_query_ap) if per_query_ap else None,
        "mean_R_precision": (
            sum(per_query_rprec) / len(per_query_rprec) if per_query_rprec else None
        ),
        "recall_at_k": {
            k: (sum(vals) / len(vals) if vals else None)
            for k, vals in per_query_recall.items()
        },
        "MAP_by_clause_count": {
            count: {
                "MAP": sum(aps) / len(aps),
                "num_queries": len(aps),
            }
            for count, aps in sorted(subgroup_aps.items())
        },
        "num_queries_evaluated": len(records),
    }

    return results


def retrieval_eval(model_name="voyage-law-2"):

    with open("/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/data/evals/situation3/sit3_eval_cases.jsonl", "r") as f:
        sit_eval_cases = [json.loads(line) for line in f]

    collection_mapper = {
        "voyage-law-2": ("voyage_embedded_clauses", 1024),
        "text-embedding-3-small": ("text_embedded_clauses", 1536),
        "Mira190/Euler-Legal-Embedding-V1": ("euler_embedded_clauses", 4096),
        "Octen/Octen-Embedding-8B": ("octen_embedded_clauses", 4096),
        "Qwen/Qwen3-Embedding-8B": ("qwen_embedded_clauses", 4096),
    }

    # set module-level COLLECTION_NAME
    global COLLECTION_NAME, VECTOR_DIM
    COLLECTION_NAME, VECTOR_DIM = collection_mapper.get(model_name, ("euler_embedded_clauses", 4096))
    print(f"Using collection '{COLLECTION_NAME}' with vector dimension {VECTOR_DIM} for model '{model_name}'.")

    records = run_retrieval(sit_eval_cases, top_k=20, sleep_seconds=0, model_name=model_name)
    metrics = aggregate_metrics(records, recall_ks=(10, 20))

    print(json.dumps(metrics, indent=2, default=str))

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    """
    Entry point: ensures the collection + indexes exist (without wiping
    existing data), then is ready for upsert_clauses / search_clauses calls.
    """
    # setup_database(COLLECTION_NAME)

    # # Usage example (uncomment and provide real data/embeddings):
    
    # with open("/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/data/embedded_clauses/finra_clauses_embedded__Octen__Octen-Embedding-8B.jsonl", "r") as f:
    #     new_clauses = [json.loads(line) for line in f]
    # upsert_clauses(new_clauses)
    
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
    # main()
    retrieval_eval(model_name="voyage-law-2")