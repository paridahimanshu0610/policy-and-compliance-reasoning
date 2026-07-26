"""
eval/loader.py

Discovers every `situationN/` folder under config.settings.EVAL_DATA_DIR,
loads that folder's `sitN_eval_cases.jsonl`, and joins every question in
whichever `sitN_{easy,medium,hard}_questions.jsonl` files happen to exist
in that folder onto the matching eval case by `situation_id`.

A folder is allowed to have any subset of {easy, medium, hard} question
files -- missing ones are just skipped, not treated as an error.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import EVAL_DATA_DIR

_DIFFICULTY_SUFFIXES = ("easy", "medium", "hard")


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


@dataclass
class EvalItem:
    """One (question, eval_case) pair ready to run through the system."""
    situation_id: str
    situation_folder: str
    difficulty: str  # "easy" | "medium" | "hard"
    question: dict[str, Any]
    eval_case: dict[str, Any]

    @property
    def item_id(self) -> str:
        return f"{self.situation_id}__{self.difficulty}"


def _find_eval_cases_file(situation_dir: Path) -> Path | None:
    matches = list(situation_dir.glob("sit*_eval_cases.jsonl"))
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"Multiple eval_cases files found in {situation_dir}: {matches}")
    return matches[0]


def _find_question_files(situation_dir: Path) -> dict[str, Path]:
    """Returns {"easy": path, "medium": path, ...} for whichever difficulty
    files actually exist in this folder."""
    found = {}
    for difficulty in _DIFFICULTY_SUFFIXES:
        matches = list(situation_dir.glob(f"*_{difficulty}_questions.jsonl"))
        if matches:
            if len(matches) > 1:
                raise ValueError(f"Multiple {difficulty} question files found in {situation_dir}: {matches}")
            found[difficulty] = matches[0]
    return found


def load_all_eval_items(data_dir: Path | None = None) -> list[EvalItem]:
    """Walk every situationN folder and return one EvalItem per question,
    joined to its eval case. Raises if a question references a
    situation_id with no matching eval case in the same folder (that's a
    dataset bug worth surfacing loudly, not silently skipping)."""
    data_dir = data_dir or EVAL_DATA_DIR
    items: list[EvalItem] = []

    situation_dirs = sorted(
        (p for p in data_dir.iterdir() if p.is_dir() and re.match(r"situation\d+$", p.name)),
        key=lambda p: int(re.search(r"\d+", p.name).group()),
    )

    for situation_dir in situation_dirs:
        eval_cases_path = _find_eval_cases_file(situation_dir)
        if eval_cases_path is None:
            raise FileNotFoundError(f"No *_eval_cases.jsonl found in {situation_dir}")

        eval_cases_by_id = {row["situation_id"]: row for row in _read_jsonl(eval_cases_path)}
        question_files = _find_question_files(situation_dir)

        if not question_files:
            continue  # a folder with only eval_cases and no questions yet -- skip quietly

        for difficulty, qpath in question_files.items():
            for question in _read_jsonl(qpath):
                sid = question["situation_id"]
                eval_case = eval_cases_by_id.get(sid)
                if eval_case is None:
                    raise KeyError(
                        f"{qpath} references situation_id={sid!r} which has no matching "
                        f"row in {eval_cases_path}"
                    )
                items.append(EvalItem(
                    situation_id=sid,
                    situation_folder=situation_dir.name,
                    difficulty=difficulty,
                    question=question,
                    eval_case=eval_case,
                ))

    return items


def load_situation(situation_folder_name: str, data_dir: Path | None = None) -> list[EvalItem]:
    """Convenience filter for running just one situation folder, e.g. for
    fast iteration while debugging: load_situation("situation3")."""
    return [i for i in load_all_eval_items(data_dir) if i.situation_folder == situation_folder_name]
