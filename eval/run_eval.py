"""
eval/run_eval.py

Entry point:  python -m eval.run_eval [--situation situation3] [--limit 5]

For every (question, eval_case) pair discovered by eval/loader.py:
  1. Start a fresh thread_id and drive the conversation with the LLM
     user-simulator (eval/user_simulator.py) until the graph produces a
     final answer, exhausts the simulated-turn cap, or is routed to
     human_handoff -- reaching human_handoff STOPS the conversation right
     there (terminated_via="human_handoff_prompt"); the simulated user
     never answers the consent/name/email/note interrupt sequence, since
     that's a real judgment call (and potential PII fabrication) we don't
     want a synthetic user making. See agent/graph.py for the
     run_turn() callbacks patch this depends on.
  2. Pull the full final AgentState via graph.get_state(...) (clause_graph,
     situation_summary, clarification_count, reasoning_cycles,
     reasoner_call_log) and the total graph-step count via
     graph.get_state_history(...).
  3. Compute retrieval + hard-fail hallucination metrics deterministically
     (eval/metrics.py), and coverage / must_mention / groundedness /
     non-gold-relevance / quality via judge calls (eval/judge.py).
  4. Write one JSON record per question to
     {EVAL_OUTPUT_DIR}/{run_id}/records.jsonl, and a rolled-up summary to
     {EVAL_OUTPUT_DIR}/{run_id}/summary.json (eval/aggregate.py).
"""

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.graph import get_graph, run_turn
from agent.retrieval_tools import get_clause
from config.settings import EVAL_OUTPUT_DIR, MAX_SIMULATED_USER_TURNS

from eval.loader import EvalItem, load_all_eval_items, load_situation
from eval.user_simulator import simulate_user_turn
from eval.instrumentation import track_usage, total_tokens
from eval import metrics as det
from eval import judge
from eval.aggregate import build_report


# ---------------------------------------------------------------------------
# Conversation driver
# ---------------------------------------------------------------------------

def _clause_text(clause_entry: dict) -> str:
    """clause_graph entries the reasoner pulled in purely via a tool call
    carry an empty payload (see agent/reasoner.py's comment on this) -- fall
    back to a direct DB lookup so judging isn't working blind for those."""
    payload = clause_entry.get("payload") or {}
    text = payload.get("merged_clause") or payload.get("original_clause")
    if text:
        return text
    fetched = get_clause(clause_entry["clause_ref"])
    if fetched:
        return fetched.get("merged_clause") or fetched.get("original_clause") or ""
    return ""


def drive_conversation(item: EvalItem) -> dict:
    """Runs the full simulated conversation for one eval item. Returns a
    dict with everything downstream metric computation needs."""
    thread_id = str(uuid.uuid4())
    transcript: list[dict] = []
    handoff_triggered = False
    simulated_user_turns = 0
    explanation_events = []          # NEW: records each aside, for metrics/regression tracking
    last_clarifying_question = None  # NEW: tracks the real pending question, distinct from any explanation content
    terminated_via = None
    final_answer = None

    with track_usage() as (callbacks, get_usage):
        response = run_turn(item.question["query"]["raw"], thread_id, callbacks=callbacks)
        transcript.append({"role": "user", "content": item.question["query"]["raw"]})
        transcript.append({"role": "assistant", "content": response["content"], "type": response["type"]})

        while True:
            if response["type"] == "answer":
                final_answer = response["content"]
                terminated_via = "answer"
                break

            if response["type"] == "clarification":
                last_clarifying_question = response["content"]
                if simulated_user_turns >= MAX_SIMULATED_USER_TURNS:
                    terminated_via = "clarification_loop_exhausted"
                    break
                user_reply = simulate_user_turn(
                    full_situation=item.eval_case.get("full_situation", ""),
                    assistant_message=response["content"],
                )
                simulated_user_turns += 1
                transcript.append({"role": "user", "content": user_reply})
                response = run_turn(user_reply, thread_id, callbacks=callbacks)
                transcript.append({"role": "assistant", "content": response["content"], "type": response["type"]})
                continue

            if response["type"] == "explanation":
                # explain_node doesn't advance known_fields/gaps/clarification_count,
                # so the conversation is exactly where it was before this fired.
                # Flag it as a potential FALSE TRIGGER if it happened on turn 1 --
                # there's nothing in the conversation yet for a meta-question to
                # point backward at, so this is the specific failure mode we
                # patched scope_gate for.
                explanation_events.append({
                    "turn_index": len(transcript),
                    "content": response["content"],
                    "likely_false_trigger": last_clarifying_question is None and simulated_user_turns == 0,
                })
                if len(explanation_events) >= MAX_SIMULATED_USER_TURNS:
                    terminated_via = "explanation_loop_exhausted"
                    break
                if last_clarifying_question is None:
                    # Nothing to re-answer -- this was the very first turn.
                    # Can't meaningfully continue as a synthetic user; stop here
                    # and let the flag above surface it in metrics.
                    terminated_via = "unexpected_explanation_on_first_turn"
                    break
                user_reply = simulate_user_turn(
                    full_situation=item.eval_case.get("full_situation", ""),
                    assistant_message=last_clarifying_question,
                )
                transcript.append({"role": "user", "content": user_reply})
                response = run_turn(user_reply, thread_id, callbacks=callbacks)
                transcript.append({"role": "assistant", "content": response["content"], "type": response["type"]})
                continue

            if response["type"] == "human_handoff_prompt":
                # Stop the conversation here, deliberately -- do NOT answer
                # the consent prompt (or any of it) on the simulated user's
                # behalf. Reaching this state is itself the outcome we want
                # to measure (the system decided it needed a human, whether
                # because the user asked directly, or because a clarification/
                # reasoning cap was hit) -- record it as such and don't touch
                # agent/human_handoff.py's interrupt() sequence at all. The
                # graph is left paused mid-interrupt for this thread_id.
                handoff_triggered = True
                terminated_via = "human_handoff_prompt"
                break

            # Defensive fallback -- shouldn't happen given graph.py's return contract.
            terminated_via = f"unexpected_type:{response['type']}"
            break

        usage_by_model = get_usage()

    config = {"configurable": {"thread_id": thread_id}}
    graph = get_graph()
    snapshot = graph.get_state(config)
    final_state = dict(snapshot.values)
    total_graph_steps = len(list(graph.get_state_history(config)))

    return {
        "thread_id": thread_id,
        "transcript": transcript,
        "terminated_via": terminated_via,
        "final_answer": final_answer,
        "final_state": final_state,
        "handoff_triggered": handoff_triggered,
        "simulated_user_turns": simulated_user_turns,
        "explanation_events": explanation_events,
        "total_graph_steps": total_graph_steps,
        "usage_by_model": usage_by_model,
    }


# ---------------------------------------------------------------------------
# Metric computation for one item
# ---------------------------------------------------------------------------

def compute_record(item: EvalItem, run: dict) -> dict:
    final_state = run["final_state"]
    clause_graph = final_state.get("clause_graph", []) or []
    situation_summary = final_state.get("situation_summary") or item.eval_case.get("full_situation", "")
    ground_truth_clauses = item.eval_case.get("ground_truth_clauses", [])
    final_answer = run["final_answer"]

    # --- retrieval (always computed -- meaningful even on a non-"answer" outcome) ---
    recall_must = det.recall_must(clause_graph, ground_truth_clauses)
    overall_recall = det.overall_recall(clause_graph, ground_truth_clauses)
    split = det.matched_and_extra_clauses(clause_graph, ground_truth_clauses)

    reasoning_block, hallucination_block, quality_block = None, None, None

    can_judge = run["terminated_via"] == "answer" and final_answer

    if can_judge:
        # --- reasoning: coverage per matched clause ---
        coverage_per_clause = []
        for entry in split["matched"]:
            gt = split["gt_by_ref"][entry["clause_ref"]]
            verdict = judge.judge_coverage(
                situation_summary=situation_summary,
                clause_ref=entry["clause_ref"],
                clause_text=_clause_text(entry),
                reference_reasoning=gt.get("contribution_reasoning", ""),
                system_reasoning=entry.get("reasoning") or "",
            )
            coverage_per_clause.append({
                "clause_ref": entry["clause_ref"],
                "coverage": verdict.coverage,
                "justification": verdict.justification,
            })

        # --- reasoning: must_mention coverage ---
        must_mention_results = []
        for point in (item.eval_case.get("reasoning_expectations") or {}).get("must_mention", []):
            verdict = judge.judge_must_mention(final_answer, point)
            must_mention_results.append({
                "point": point,
                "covered": verdict.covered,
                "justification": verdict.justification,
            })

        reasoning_block = {
            "coverage_per_clause": coverage_per_clause,
            "must_mention": must_mention_results,
        }

        # --- hallucination: clause_ref grounding (hard fail, deterministic) ---
        # Determines if final answer includes any clause outside the clause_graph because of hallucination
        clause_ref_grounding = det.clause_ref_grounding(final_answer, clause_graph)

        # --- hallucination: groundedness per matched+extra clause actually cited ---
        groundedness_per_clause = []
        for entry in split["matched"] + split["extra"]:
            if not entry.get("reasoning"):
                continue
            verdict = judge.judge_groundedness(
                situation_summary=situation_summary,
                clause_ref=entry["clause_ref"],
                clause_text=_clause_text(entry),
                system_reasoning=entry["reasoning"],
            )
            groundedness_per_clause.append({
                "clause_ref": entry["clause_ref"],
                "groundedness": verdict.groundedness,
                "justification": verdict.justification,
                "unsupported_claims": verdict.unsupported_claims,
            })

        # --- hallucination: non-gold clause relevance ---
        non_gold_relevance = []
        for entry in split["extra"]:
            if not entry.get("relevance_role"):
                continue  # never made it into the reasoned/final answer set
            verdict = judge.judge_non_gold_relevance(
                situation_summary=situation_summary,
                clause_ref=entry["clause_ref"],
                system_reasoning=entry.get("reasoning") or "",
            )
            non_gold_relevance.append({
                "clause_ref": entry["clause_ref"],
                "relevance": verdict.relevance,
                "justification": verdict.justification,
            })

        hallucination_block = {
            "clause_ref_grounding": clause_ref_grounding,
            "groundedness_per_clause": groundedness_per_clause,
            "non_gold_relevance": non_gold_relevance,
        }

        # --- quality ---
        quality_verdict = judge.judge_quality(
            situation_summary=situation_summary,
            final_answer=final_answer,
            expected_answer_structure=(item.eval_case.get("reasoning_expectations") or {}).get("answer_structure"),
        )
        quality_block = {
            "responsiveness_score": quality_verdict.responsiveness_score,
            "responsiveness_justification": quality_verdict.responsiveness_justification,
            "structural_clarity_score": quality_verdict.structural_clarity_score,
            "structural_clarity_justification": quality_verdict.structural_clarity_justification,
        }
    else:
        # Deterministic clause_ref grounding still makes sense even without
        # a "real" answer (e.g. the handoff-decline message never cites
        # clauses, so this trivially passes -- kept for completeness/audit).
        hallucination_block = {
            "clause_ref_grounding": det.clause_ref_grounding(final_answer or "", clause_graph),
            "groundedness_per_clause": [],
            "non_gold_relevance": [],
        }

    # --- agentic / operational metrics ---
    reasoner_log = final_state.get("reasoner_call_log", []) or []
    durations = [entry["duration_seconds"] for entry in reasoner_log]
    tool_call_counts = [entry["tool_call_count"] for entry in reasoner_log]

    agentic_block = {
        "clarification_count": final_state.get("clarification_count", 0),
        "reasoning_cycles": final_state.get("reasoning_cycles", 0),
        "reasoner_call_log": reasoner_log,
        "avg_reasoner_duration_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "total_tool_calls": sum(tool_call_counts) if tool_call_counts else 0,
        "total_graph_steps": run["total_graph_steps"],
        "handoff_triggered": run["handoff_triggered"],
        "escalation_reason_last_seen": final_state.get("escalation_reason"),
        "simulated_user_turns": run["simulated_user_turns"],
        "explanation_events": run.get("explanation_events", []),
        "false_explain_triggers": sum(
            1 for e in run.get("explanation_events", []) if e["likely_false_trigger"]
        ),
    }

    return {
        "item_id": item.item_id,
        "situation_id": item.situation_id,
        "situation_folder": item.situation_folder,
        "difficulty": item.difficulty,
        "thread_id": run["thread_id"],
        "query_raw": item.question["query"]["raw"],
        "query_is_complete": item.question["query"].get("is_complete"),
        "terminated_via": run["terminated_via"],
        "final_answer": final_answer,
        "transcript": run["transcript"],
        "retrieval": {
            "recall_must": recall_must,
            "overall_recall": overall_recall,
        },
        "reasoning": reasoning_block,
        "hallucination": hallucination_block,
        "quality": quality_block,
        "agentic": agentic_block,
        "token_usage": {
            "by_model": run["usage_by_model"],
            "total_tokens": total_tokens(run["usage_by_model"]),
        },
        "evaluation_flags": item.eval_case.get("evaluation_flags", {}),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(items: list[EvalItem], run_dir: Path) -> list[dict]:
    records_path = run_dir / "records.jsonl"
    records = []
    with open(records_path, "w", encoding="utf-8") as f:
        for i, item in enumerate(items, 1):
            print(f"[{i}/{len(items)}] {item.item_id} ...", flush=True)
            t0 = time.perf_counter()
            try:
                conv = drive_conversation(item)
                record = compute_record(item, conv)
            except Exception as exc:  # noqa: BLE001 -- eval must survive one bad item
                record = {
                    "item_id": item.item_id,
                    "situation_id": item.situation_id,
                    "situation_folder": item.situation_folder,
                    "difficulty": item.difficulty,
                    "terminated_via": "error",
                    "error": repr(exc),
                }
            record["wall_clock_seconds"] = round(time.perf_counter() - t0, 2)
            f.write(json.dumps(record) + "\n")
            f.flush()
            records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description="Run the FINRA compliance agent eval suite.")
    parser.add_argument("--situation", type=str, default=None,
                         help="Only run one situation folder, e.g. situation3.")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only run the first N items (after any --situation filter).")
    parser.add_argument("--run-id", type=str, default=None,
                         help="Name for this run's output folder. Defaults to a UTC timestamp.")
    args = parser.parse_args()

    items = load_situation(args.situation) if args.situation else load_all_eval_items()
    if args.limit:
        items = items[:args.limit]

    if not items:
        print("No eval items found -- check EVAL_DATA_DIR / --situation.")
        return

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = EVAL_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running {len(items)} eval items -> {run_dir}")
    records = run(items, run_dir)

    # Only well-formed records (no top-level "error") feed the aggregate --
    # errored items are still visible in records.jsonl for debugging, but
    # summarize() assumes the normal record shape.
    clean_records = [r for r in records if r.get("terminated_via") != "error"]
    report = build_report(clean_records)
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