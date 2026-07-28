"""
eval/metrics.py

Every metric here is computed by plain comparison -- no LLM call, no
ambiguity in scoring. Kept separate from eval/judge.py (which holds the
metrics that genuinely need an LLM's judgment) so it's obvious at a glance
which numbers in the final report are "hard facts" vs. "judged".
"""

import re
from typing import Any


def _refs(clauses: list[dict], key: str = "clause_ref") -> set[str]:
    return {c[key] for c in clauses if c.get(key)}


# ---------------------------------------------------------------------------
# Retrieval metrics
# ---------------------------------------------------------------------------

def recall_must(clause_graph: list[dict], ground_truth_clauses: list[dict]) -> dict:
    """Gate metric. Of the ground-truth clauses marked retrieval_priority ==
    'must_retrieve', what fraction made it into clause_graph? Must be 1.0
    for the answer to be trustworthy at all."""
    must = [c for c in ground_truth_clauses if c.get("retrieval_priority") == "must_retrieve"]
    must_refs = _refs(must)
    found_refs = _refs(clause_graph)

    if not must_refs:
        return {"recall": None, "must_refs": [], "found": [], "missing": [], "n_must": 0}

    found = must_refs & found_refs
    missing = must_refs - found_refs
    return {
        "recall": len(found) / len(must_refs),
        "must_refs": sorted(must_refs),
        "found": sorted(found),
        "missing": sorted(missing),
        "n_must": len(must_refs),
    }


def overall_recall(clause_graph: list[dict], ground_truth_clauses: list[dict]) -> dict:
    """Of ALL ground-truth clauses for this situation (regardless of
    retrieval_priority), what fraction made it into clause_graph?"""
    gt_refs = _refs(ground_truth_clauses)
    found_refs = _refs(clause_graph)

    if not gt_refs:
        return {"recall": None, "gt_refs": [], "found": [], "missing": [], "n_gt": 0}

    found = gt_refs & found_refs
    missing = gt_refs - found_refs
    return {
        "recall": len(found) / len(gt_refs),
        "gt_refs": sorted(gt_refs),
        "found": sorted(found),
        "missing": sorted(missing),
        "n_gt": len(gt_refs),
    }


def matched_and_extra_clauses(
    clause_graph: list[dict], ground_truth_clauses: list[dict]
) -> dict:
    """Split clause_graph into the subset that overlaps ground truth
    (matched -- feeds coverage/groundedness judging) and the subset that
    doesn't (extra -- feeds non-gold relevance judging)."""
    gt_by_ref = {c["clause_ref"]: c for c in ground_truth_clauses}
    matched, extra = [], []
    for c in clause_graph:
        if c.get("clause_ref") in gt_by_ref:
            matched.append(c)
        else:
            extra.append(c)
    return {"matched": matched, "extra": extra, "gt_by_ref": gt_by_ref}


# ---------------------------------------------------------------------------
# Hallucination metric: clause-ref grounding (hard fail, zero-tolerance)
# ---------------------------------------------------------------------------

# Best-effort extraction of clause references from prose. FINRA clause_refs
# look like "FINRA-3110", "FINRA-4210(e)(2)(H)(i)e.2.A.", "Rule 2210", etc.
# This intentionally over-matches slightly (better to flag a false positive
# for manual review than silently miss a real citation).
# _CLAUSE_REF_PATTERN = re.compile(
#     r"\bFINRA-\d+(?:\([^\s()]{1,6}\)|[A-Za-z0-9]{1,3}\.)*"
# )
_CLAUSE_REF_PATTERN = re.compile(
    r"\bFINRA-\d+"
    r"(?:\([^\s()]{1,6}\)|[A-Za-z][A-Za-z0-9]{0,2}\.)"      # required first clause segment
    r"(?:\([^\s()]{1,6}\)|[A-Za-z0-9]{1,3}\.?)*"            # optional further segments
)


def extract_cited_clause_refs(final_answer: str) -> list[str]:
    if not final_answer:
        return []
    return sorted(set(_CLAUSE_REF_PATTERN.findall(final_answer)))


def clause_ref_grounding(final_answer: str, clause_graph: list[dict]) -> dict:
    """Every clause_ref-looking token cited in the final answer must exist
    in clause_graph. Any that don't is a hard-fail hallucinated citation."""
    cited = extract_cited_clause_refs(final_answer)
    known = _refs(clause_graph)
    ungrounded = sorted(set(cited) - known)
    return {
        "cited_refs": cited,
        "ungrounded_refs": ungrounded,
        "passed": len(ungrounded) == 0,
    }
