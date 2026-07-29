# --------------------------------------------------------------------------
# Embedding Evaluation
# --------------------------------------------------------------------------
import time
import json
from collections import defaultdict

from ingestion.build_vector_db import generate_query_embeddings, search_clauses 

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

    records = run_retrieval(sit_eval_cases, top_k=20, sleep_seconds=0, model_name=model_name)
    metrics = aggregate_metrics(records, recall_ks=(10, 20))

    print(json.dumps(metrics, indent=2, default=str))