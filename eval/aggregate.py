"""
eval/aggregate.py

Rolls a list of per-question records (produced by eval/run_eval.py) up into
a per-situation-folder summary and an overall summary. Kept separate from
run_eval.py's orchestration logic so the aggregation rules can be
re-run/tweaked against an existing results JSONL without re-running the
(expensive) system + judge calls.
"""

from collections import defaultdict
from statistics import mean
from typing import Any, Optional


def _safe_mean(values: list[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(mean(clean), 4) if clean else None


def _rate(bools: list[Optional[bool]]) -> Optional[float]:
    clean = [b for b in bools if b is not None]
    return round(sum(1 for b in clean if b) / len(clean), 4) if clean else None


def summarize(records: list[dict]) -> dict:
    """One summary dict for a homogeneous group of records (either all
    records for one situation folder, or all records overall)."""
    n = len(records)
    answered = [r for r in records if r["terminated_via"] == "answer"]

    coverage_full_flags = []
    groundedness_bad_flags = []  # True if "fabricated" (the failure case we care about)
    non_gold_noise_flags = []
    for r in records:
        reasoning = r.get("reasoning") or {}
        for c in reasoning.get("coverage_per_clause", []):
            coverage_full_flags.append(c["coverage"] == "full")
        halluc = r.get("hallucination") or {}
        for g in halluc.get("groundedness_per_clause", []):
            groundedness_bad_flags.append(g["groundedness"] == "fabricated")
        for ng in halluc.get("non_gold_relevance", []):
            non_gold_noise_flags.append(ng["relevance"] == "noise")

    must_mention_flags = []
    for r in records:
        for m in (r.get("reasoning") or {}).get("must_mention", []):
            must_mention_flags.append(m["covered"])

    return {
        "n_questions": n,
        "terminated_via_breakdown": {
            kind: sum(1 for r in records if r["terminated_via"] == kind)
            for kind in {r["terminated_via"] for r in records}
        },
        "retrieval": {
            "recall_must_gate_pass_rate": _rate(
                [r["retrieval"]["recall_must"]["recall"] == 1.0
                 if r["retrieval"]["recall_must"]["recall"] is not None else None
                 for r in records]
            ),
            "avg_recall_must": _safe_mean([r["retrieval"]["recall_must"]["recall"] for r in records]),
            "avg_overall_recall": _safe_mean([r["retrieval"]["overall_recall"]["recall"] for r in records]),
        },
        "reasoning": {
            "avg_coverage_full_rate": _rate(coverage_full_flags),
            "avg_must_mention_coverage_rate": _rate(must_mention_flags),
        },
        "hallucination": {
            "clause_ref_grounding_pass_rate": _rate(
                [r["hallucination"]["clause_ref_grounding"]["passed"] for r in records]
            ),
            "groundedness_fabricated_rate": _rate(groundedness_bad_flags),
            "non_gold_noise_rate": _rate(non_gold_noise_flags),
        },
        "quality": {
            "avg_responsiveness_score": _safe_mean(
                [(r.get("quality") or {}).get("responsiveness_score") for r in answered]
            ),
            "avg_structural_clarity_score": _safe_mean(
                [(r.get("quality") or {}).get("structural_clarity_score") for r in answered]
            ),
        },
        "agentic": {
            "avg_clarification_count": _safe_mean([r["agentic"]["clarification_count"] for r in records]),
            "avg_reasoning_cycles": _safe_mean([r["agentic"]["reasoning_cycles"] for r in records]),
            "avg_total_tool_calls": _safe_mean([r["agentic"]["total_tool_calls"] for r in records]),
            "avg_reasoner_duration_seconds": _safe_mean(
                [r["agentic"]["avg_reasoner_duration_seconds"] for r in records]
            ),
            "avg_total_graph_steps": _safe_mean([r["agentic"]["total_graph_steps"] for r in records]),
            "handoff_rate": _rate([r["agentic"]["handoff_triggered"] for r in records]),
            "avg_simulated_user_turns": _safe_mean([r["agentic"]["simulated_user_turns"] for r in records]),
        },
        "tokens": {
            "avg_total_tokens": _safe_mean([r["token_usage"]["total_tokens"] for r in records]),
            "sum_total_tokens": sum(r["token_usage"]["total_tokens"] for r in records),
        },
    }


def build_report(records: list[dict]) -> dict:
    by_situation_folder: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_situation_folder[r["situation_folder"]].append(r)

    return {
        "overall": summarize(records),
        "by_situation_folder": {
            folder: summarize(items) for folder, items in sorted(by_situation_folder.items())
        },
    }
