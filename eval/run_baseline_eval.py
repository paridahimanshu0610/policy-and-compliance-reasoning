"""
eval/run_baseline_retrieval_eval.py

Entry point:  python -m eval.run_baseline_retrieval_eval [--situation situation3] [--limit 5]

A retrieval-only baseline eval, separate from run_agent_eval.py. Where run_agent_eval.py
drives the full agent (reasoner + tool calls + judge LLM calls) and measures
end-to-end quality, this script isolates JUST the retrieval step and asks:
"if we skipped the agent entirely and threw the query straight at
search_clauses, how good would retrieval be?" No graph, no reasoner, no
judge calls -- just eval.metrics' deterministic recall_must / overall_recall
against four baseline retrieval configurations per eval item:

    (retrieval mode)  x  (input text)
    dense  / sparse   x  raw_query / expected_summary

  dense_raw_query          -- dense search on item.question["query"]["raw"]
  dense_expected_summary   -- dense search on item.eval_case["expected_situation_summary"]
  sparse_raw_query         -- BM25 search on item.question["query"]["raw"]
  sparse_expected_summary  -- BM25 search on item.eval_case["expected_situation_summary"]

"expected_situation_summary" is the analyst-written, already-clarified
restatement of the situation (see finra_qdrant_setup.py's retrieval_eval /
run_retrieval, which uses the same field). Comparing raw_query vs.
expected_summary performance is what tells you how much of any retrieval
gap is "the query was underspecified" vs. "the retriever itself is weak".

IMPORTANT CAVEAT ON RECALL vs. top_k:
recall_must / overall_recall (eval/metrics.py) score whether ground-truth
clause_refs appear ANYWHERE in the retrieved set. That retrieved set is
itself capped at top_k, so every number produced here is really
"Recall@top_k", not unconditional recall. top_k defaults to
search_clauses' own default (20); pass --top-k to change it, and always
report the top_k value alongside these numbers -- a bigger top_k
mechanically inflates recall regardless of retrieval quality.

Writes one JSON record per question to
{EVAL_OUTPUT_DIR}/baseline_retrieval/{run_id}/records.jsonl, and a
per-situation-folder + overall summary (one block per of the 4 combos above)
to {EVAL_OUTPUT_DIR}/baseline_retrieval/{run_id}/summary.json.
"""

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config.settings import ACTIVE_EMBEDDING_MODEL, EVAL_OUTPUT_DIR, RETRIEVAL_TOP_K
from ingestion.build_vector_db import generate_query_embeddings, search_clauses

from eval import metrics as det
from eval.aggregate import _rate, _safe_mean
from eval.loader import EvalItem, load_all_eval_items, load_situation

# ---------------------------------------------------------------------------
# The four baseline combos this script scores. Order here is the order
# they'll appear in records.jsonl / summary.json.
# ---------------------------------------------------------------------------

INPUT_SOURCES = {
    "raw_query": lambda item: item.question["query"]["raw"],
    "expected_summary": lambda item: item.eval_case.get("expected_situation_summary", ""),
}

RETRIEVAL_MODES = ["dense", "sparse"]

COMBOS = [f"{mode}_{source}" for mode in RETRIEVAL_MODES for source in INPUT_SOURCES]


# ---------------------------------------------------------------------------
# Retrieval for one (mode, text) pair
# ---------------------------------------------------------------------------

def _retrieve(text: str, mode: str, top_k: int, embedding_model: str) -> list[dict]:
    """Runs search_clauses in the given mode and returns results shaped like
    clause_graph entries (just clause_ref is all recall_must/overall_recall
    need -- score is kept alongside for debugging/inspection only)."""
    if not text or not text.strip():
        # Nothing to search on (e.g. expected_situation_summary missing for
        # this eval case) -- empty retrieval, not an error. Let it flow
        # through as a legitimate zero-recall result rather than crashing
        # the whole run on one bad eval case.
        return []

    if mode == "dense":
        query_embedding = generate_query_embeddings(text, embedding_model)
        results = search_clauses(query_embedding=query_embedding, search_mode="dense", top_k=top_k)
    elif mode == "sparse":
        results = search_clauses(query_text=text, search_mode="sparse", top_k=top_k)
    else:
        raise ValueError(f"Unknown retrieval mode: {mode!r}")

    return [{"clause_ref": r["clause_ref"], "score": r["score"]} for r in results]


def compute_baseline_record(item: EvalItem, top_k: int, embedding_model: str) -> dict:
    ground_truth_clauses = item.eval_case.get("ground_truth_clauses", [])

    combo_results = {}
    for mode in RETRIEVAL_MODES:
        for source_name, source_fn in INPUT_SOURCES.items():
            combo = f"{mode}_{source_name}"
            text = source_fn(item)
            retrieved = _retrieve(text, mode, top_k, embedding_model)

            combo_results[combo] = {
                "input_text": text,
                "retrieved": retrieved,
                "recall_must": det.recall_must(retrieved, ground_truth_clauses),
                "overall_recall": det.overall_recall(retrieved, ground_truth_clauses),
            }

    return {
        "item_id": item.item_id,
        "situation_id": item.situation_id,
        "situation_folder": item.situation_folder,
        "difficulty": item.difficulty,
        "query_raw": item.question["query"]["raw"],
        "expected_situation_summary": item.eval_case.get("expected_situation_summary", ""),
        "ground_truth_refs": sorted({c["clause_ref"] for c in ground_truth_clauses if c.get("clause_ref")}),
        **combo_results,
    }


# ---------------------------------------------------------------------------
# Aggregation -- reuses eval.aggregate's _rate / _safe_mean so the pass/fail
# and averaging semantics stay identical to the main eval's retrieval block.
# ---------------------------------------------------------------------------

def summarize_combo(records: list[dict], combo: str) -> dict:
    return {
        "n_questions": len(records),
        "recall_must_gate_pass_rate": _rate(
            [
                r[combo]["recall_must"]["recall"] == 1.0
                if r[combo]["recall_must"]["recall"] is not None else None
                for r in records
            ]
        ),
        "avg_recall_must": _safe_mean([r[combo]["recall_must"]["recall"] for r in records]),
        "avg_overall_recall": _safe_mean([r[combo]["overall_recall"]["recall"] for r in records]),
    }


def build_baseline_report(records: list[dict], top_k: int, embedding_model: str) -> dict:
    by_situation_folder: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_situation_folder[r["situation_folder"]].append(r)

    return {
        "overall": {combo: summarize_combo(records, combo) for combo in COMBOS},
        "by_situation_folder": {
            folder: {combo: summarize_combo(items, combo) for combo in COMBOS}
            for folder, items in sorted(by_situation_folder.items())
        },
        "config": {
            "top_k": top_k,
            "embedding_model": embedding_model,
            "combos": COMBOS,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(items: list[EvalItem], run_dir: Path, top_k: int, embedding_model: str) -> list[dict]:
    records_path = run_dir / "records.jsonl"
    records = []
    with open(records_path, "w", encoding="utf-8") as f:
        for i, item in enumerate(items, 1):
            print(f"[{i}/{len(items)}] {item.item_id} ...", flush=True)
            t0 = time.perf_counter()
            try:
                record = compute_baseline_record(item, top_k=top_k, embedding_model=embedding_model)
            except Exception as exc:  # noqa: BLE001 -- one bad item shouldn't kill the run
                record = {
                    "item_id": item.item_id,
                    "situation_id": item.situation_id,
                    "situation_folder": item.situation_folder,
                    "difficulty": item.difficulty,
                    "error": repr(exc),
                }
            record["wall_clock_seconds"] = round(time.perf_counter() - t0, 2)
            f.write(json.dumps(record) + "\n")
            f.flush()
            records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description="Baseline (agent-free) retrieval eval: dense/sparse x raw_query/expected_summary.")
    parser.add_argument("--situation", type=str, default=None,
                         help="Only run one situation folder, e.g. situation3.")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only run the first N items (after any --situation filter).")
    parser.add_argument("--run-id", type=str, default=None,
                         help="Name for this run's output folder. Defaults to a UTC timestamp.")
    parser.add_argument("--top-k", type=int, default=RETRIEVAL_TOP_K,
                         help="top_k passed to search_clauses for every combo (default: 20, matching search_clauses' own default). "
                              "Recall numbers are Recall@top_k -- larger top_k mechanically inflates recall.")
    parser.add_argument("--embedding-model", type=str, default=ACTIVE_EMBEDDING_MODEL,
                         help="Model passed to generate_query_embeddings for dense combos. "
                              "Defaults to config.settings.ACTIVE_EMBEDDING_MODEL -- must match "
                              "whatever model embedded the documents in the target Qdrant collection, "
                              "or dense search will silently return meaningless results.")
    args = parser.parse_args()

    items = load_situation(args.situation) if args.situation else load_all_eval_items()
    if args.limit:
        items = items[:args.limit]

    if not items:
        print("No eval items found -- check EVAL_DATA_DIR / --situation.")
        return

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = EVAL_OUTPUT_DIR / "baseline_retrieval" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running {len(items)} eval items x {len(COMBOS)} combos {COMBOS} "
          f"(top_k={args.top_k}, embedding_model={args.embedding_model}) -> {run_dir}")
    records = run(items, run_dir, top_k=args.top_k, embedding_model=args.embedding_model)

    # Only well-formed records (no top-level "error") feed the aggregate --
    # errored items stay visible in records.jsonl for debugging.
    clean_records = [r for r in records if "error" not in r]
    report = build_baseline_report(clean_records, top_k=args.top_k, embedding_model=args.embedding_model)
    report["run_metadata"] = {
        "run_id": run_id,
        "n_items_total": len(records),
        "n_items_errored": len(records) - len(clean_records),
    }

    summary_path = run_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\nDone. Records: {run_dir / 'records.jsonl'}\nSummary: {summary_path}")


if __name__ == "__main__":
    main()