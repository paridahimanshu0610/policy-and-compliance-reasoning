#!/usr/bin/env python3
"""
Evaluate the FINRA compliance system on the curated test questions.

This script computes two metrics:

1. Retrieval Precision@5
   For each question, score = |retrieved_top_5 ∩ expected_clause_refs| / |expected_clause_refs|

2. End-to-End Correctness
   For each question, score = |cited_required_clauses| / |expected_clause_refs|

It uses the same local pipeline components as the app/server:
    clarification -> intent extraction -> retrieval -> reasoning

During clarification, if the system asks follow-up questions, the script
answers them using the fixture's "answer_to_clarifying_questions" mapping.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


QUESTION_NORMALIZER = re.compile(r"[^a-z0-9]+")
CLAUSE_REF_PATTERN = re.compile(
    r"\b(?:FINRA-)?\d{4}(?:-SM\.\d+|(?:\([A-Za-z0-9]+\))+)?\b",
    re.IGNORECASE,
)


@dataclass
class QuestionCase:
    rule_id: str
    difficulty: str
    index_within_bucket: int
    original_question: str
    answer_to_clarifying_questions: dict[str, str]
    expected_clause_refs: list[str]


@dataclass
class EvaluationResult:
    rule_id: str
    difficulty: str
    index_within_bucket: int
    original_question: str
    clarification_turns: int
    clarification_questions: list[str]
    situation_summary: str
    intent: dict[str, Any] | None
    expected_clause_refs: list[str]
    retrieved_clause_refs: list[str]
    cited_clause_refs: list[str]
    retrieval_score: float
    correctness_score: float


def get_settings() -> Any:
    from config import settings
    return settings


def strip_field_assessment(text: str) -> str:
    from pipeline.intent_pipeline import _strip_field_assessment
    return _strip_field_assessment(text)


def canonicalize_clause_ref(ref: str) -> str:
    cleaned = re.sub(r"\s+", "", ref.strip())
    if not cleaned:
        return ""
    if not cleaned.lower().startswith("finra-"):
        cleaned = f"FINRA-{cleaned}"
    return cleaned


def display_clause_refs(refs: list[str] | set[str]) -> list[str]:
    return sorted(set(refs), key=lambda value: (value.split("-", 1)[-1], value))


def normalize_question_text(text: str) -> str:
    stripped = strip_field_assessment(text)
    lowered = stripped.strip().lower()
    return QUESTION_NORMALIZER.sub(" ", lowered).strip()


def extract_clause_refs(text: str) -> set[str]:
    return {
        canonicalize_clause_ref(match.group(0))
        for match in CLAUSE_REF_PATTERN.finditer(text or "")
    }


def flatten_question_cases(test_questions: dict[str, Any]) -> list[QuestionCase]:
    cases: list[QuestionCase] = []
    for rule_id, buckets in test_questions.items():
        for difficulty, questions in buckets.items():
            for index, question in enumerate(questions, start=1):
                cases.append(
                    QuestionCase(
                        rule_id=rule_id,
                        difficulty=difficulty,
                        index_within_bucket=index,
                        original_question=question["original_question"],
                        answer_to_clarifying_questions=question["answer_to_clarifying_questions"],
                        expected_clause_refs=question["expected_clause_refs"],
                    )
                )
    return cases


def resolve_clarifying_answer(
    asked_question: str,
    canned_answers: dict[str, str],
) -> str:
    normalized_asked = normalize_question_text(asked_question)
    normalized_answers = {
        normalize_question_text(question): answer
        for question, answer in canned_answers.items()
    }

    exact_match = normalized_answers.get(normalized_asked)
    if exact_match is not None:
        return exact_match

    for normalized_question, answer in normalized_answers.items():
        if normalized_asked in normalized_question or normalized_question in normalized_asked:
            return answer

    close_match = difflib.get_close_matches(
        normalized_asked,
        normalized_answers.keys(),
        n=1,
        cutoff=0.6,
    )
    if close_match:
        return normalized_answers[close_match[0]]

    known_questions = "\n".join(f"- {question}" for question in canned_answers)
    raise KeyError(
        "Unable to match clarification question to fixture answers.\n"
        f"Asked: {asked_question}\n"
        f"Known questions:\n{known_questions}"
    )


def run_clarification_flow(
    model: Any,
    case: QuestionCase,
    max_questions: int,
) -> tuple[str, int, list[str]]:
    conversation: list[dict[str, str]] = []
    questions_asked = 0
    clarification_questions: list[str] = []
    user_message = case.original_question

    for _ in range(max_questions + 2):
        from pipeline.intent_pipeline import process_clarification_turn

        result = process_clarification_turn(
            model=model,
            user_message=user_message,
            conversation=conversation,
            questions_asked=questions_asked,
            max_questions=max_questions,
        )

        conversation = result["conversation"]
        questions_asked = result["questions_asked"]

        if result["type"] == "ready":
            return result["content"], len(clarification_questions), clarification_questions

        asked_question = strip_field_assessment(result["content"])
        clarification_questions.append(asked_question)
        user_message = resolve_clarifying_answer(
            asked_question,
            case.answer_to_clarifying_questions,
        )

    raise RuntimeError(
        f"Clarification did not converge for question: {case.original_question}"
    )


def evaluate_case(
    model: Any,
    collection: Any,
    case: QuestionCase,
    top_k: int,
    max_questions: int,
) -> EvaluationResult:
    from pipeline.compliance_reasoning import run_compliance_reasoning
    from pipeline.intent_pipeline import extract_structured_intent
    from pipeline.retrieval import retrieve_clauses

    situation_summary, clarification_turns, clarification_questions = run_clarification_flow(
        model=model,
        case=case,
        max_questions=max_questions,
    )

    intent = extract_structured_intent(model, situation_summary)
    if intent is None:
        raise RuntimeError(
            f"Intent extraction failed for question: {case.original_question}"
        )

    intent["situation_summary"] = situation_summary
    retrieved = retrieve_clauses(intent, collection, top_k=top_k)
    reasoning = run_compliance_reasoning(model, situation_summary, retrieved)

    expected = {canonicalize_clause_ref(ref) for ref in case.expected_clause_refs}
    retrieved_refs = {canonicalize_clause_ref(item["clause_ref"]) for item in retrieved[:top_k]}

    correctness_text = "\n".join(
        [
            reasoning.get("determination", ""),
            reasoning.get("applicable_clauses", ""),
        ]
    )
    cited_refs = extract_clause_refs(correctness_text)

    retrieval_score = len(retrieved_refs & expected) / len(expected)
    correctness_score = len(cited_refs & expected) / len(expected)

    return EvaluationResult(
        rule_id=case.rule_id,
        difficulty=case.difficulty,
        index_within_bucket=case.index_within_bucket,
        original_question=case.original_question,
        clarification_turns=clarification_turns,
        clarification_questions=clarification_questions,
        situation_summary=situation_summary,
        intent=intent,
        expected_clause_refs=display_clause_refs(case.expected_clause_refs),
        retrieved_clause_refs=display_clause_refs(retrieved_refs),
        cited_clause_refs=display_clause_refs(cited_refs),
        retrieval_score=retrieval_score,
        correctness_score=correctness_score,
    )


def load_test_questions(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def build_summary(results: list[EvaluationResult]) -> dict[str, Any]:
    if not results:
        return {
            "question_count": 0,
            "retrieval_precision_at_5": 0.0,
            "correctness": 0.0,
        }

    retrieval_average = sum(item.retrieval_score for item in results) / len(results)
    correctness_average = sum(item.correctness_score for item in results) / len(results)

    return {
        "question_count": len(results),
        "retrieval_precision_at_5": retrieval_average,
        "correctness": correctness_average,
    }


def print_report(results: list[EvaluationResult], summary: dict[str, Any], top_k: int) -> None:
    print("\nEvaluation Summary")
    print("==================")
    print(f"Questions evaluated        : {summary['question_count']}")
    print(f"Retrieval Precision@{top_k}: {summary['retrieval_precision_at_5']:.4f}")
    print(f"Correctness                : {summary['correctness']:.4f}")

    print("\nPer-question Results")
    print("====================")
    for result in results:
        print(
            f"[{result.rule_id} / {result.difficulty} / #{result.index_within_bucket}] "
            f"retrieval={result.retrieval_score:.4f} "
            f"correctness={result.correctness_score:.4f}"
        )
        print(f"Q: {result.original_question}")
        print(f"Expected : {', '.join(result.expected_clause_refs) or '(none)'}")
        print(f"Retrieved: {', '.join(result.retrieved_clause_refs) or '(none)'}")
        print(f"Cited    : {', '.join(result.cited_clause_refs) or '(none)'}")
        if result.clarification_questions:
            print("Clarifying questions asked:")
            for question in result.clarification_questions:
                print(f"  - {question}")
        print()


def parse_args() -> argparse.Namespace:
    settings = get_settings()

    parser = argparse.ArgumentParser(
        description="Evaluate the FINRA compliance system on test_questions.json."
    )
    parser.add_argument(
        "--questions-file",
        type=Path,
        default=settings.BASE_DIR / "data" / "test_questions.json",
        help="Path to the test question fixture JSON.",
    )
    parser.add_argument(
        "--model",
        choices=["qwen", "llama"],
        default=settings.DEFAULT_MODEL,
        help="Model to use for clarification, intent extraction, and reasoning.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=settings.DEFAULT_TOP_K,
        help="Number of clauses to retrieve. Metric 1 is intended for top-5.",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=settings.MAX_CLARIFY_QUESTIONS,
        help="Maximum clarification questions before forcing summarization.",
    )
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        help="Optional rule id filter. Can be passed multiple times.",
    )
    parser.add_argument(
        "--difficulty",
        action="append",
        default=[],
        help="Optional difficulty filter. Can be passed multiple times.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for quick smoke tests.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional output path for a JSON report.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from pipeline.retrieval import load_collection
    from web.server import _load_model_sync

    test_questions = load_test_questions(args.questions_file)
    cases = flatten_question_cases(test_questions)

    if args.rule:
        allowed_rules = set(args.rule)
        cases = [case for case in cases if case.rule_id in allowed_rules]

    if args.difficulty:
        allowed_difficulties = set(args.difficulty)
        cases = [case for case in cases if case.difficulty in allowed_difficulties]

    if args.limit is not None:
        cases = cases[: args.limit]

    if not cases:
        raise SystemExit("No questions matched the provided filters.")

    print(f"Loading model: {args.model}")
    model = _load_model_sync(args.model)

    print("Loading ChromaDB collection...")
    collection = load_collection()
    print(f"Loaded {len(cases)} question(s). Starting evaluation...")

    results: list[EvaluationResult] = []
    for index, case in enumerate(cases, start=1):
        print(f"\n[{index}/{len(cases)}] {case.original_question}")
        results.append(
            evaluate_case(
                model=model,
                collection=collection,
                case=case,
                top_k=args.top_k,
                max_questions=args.max_questions,
            )
        )

    summary = build_summary(results)
    print_report(results, summary, args.top_k)

    if args.json_out is not None:
        payload = {
            "summary": summary,
            "results": [asdict(result) for result in results],
        }
        args.json_out.write_text(json.dumps(payload, indent=2))
        print(f"JSON report written to: {args.json_out}")


if __name__ == "__main__":
    main()
