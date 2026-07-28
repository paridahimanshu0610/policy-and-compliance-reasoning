"""
mcp_server/data_access.py

Lightweight, dependency-free readers for the two flat data files this
project's ingestion pipeline already produces:

    - data/aggregate_normalized_clauses.jsonl  (one normalized clause per line)
    - data/parsed_rules.json                   (rule_id -> {meta, clauses, merged})

Used by the MCP resources (finra-clause://, finra-rule://) and by the
discovery/cross-reference tools (list_rules, resolve_cross_references) --
none of which need semantic search, just an exact-key lookup against data
that's already on disk. Deliberately kept separate from
ingestion.build_vector_db / agent.retrieval_tools: those talk to Qdrant,
these don't touch the vector DB at all, so "give me the text of
FINRA-2010-intro" or "give me everything under Rule 3110" doesn't pay for
an embedding call or a network round trip it doesn't need.

Both loaders are process-local, in-memory caches (functools.lru_cache), not
a real cache layer -- this is a few thousand clauses of JSON. Restart the
server process to pick up new data after a re-ingestion run.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from config.settings import DATA_DIR

CLAUSES_PATH = Path(DATA_DIR) / "aggregate_normalized_clauses.jsonl"
RULES_PATH = Path(DATA_DIR) / "parsed_rules.json"


@lru_cache(maxsize=1)
def _clauses_by_ref() -> dict[str, dict]:
    """clause_ref -> normalized clause dict, loaded once from the jsonl."""
    index: dict[str, dict] = {}
    with open(CLAUSES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            index[record["clause_ref"]] = record
    return index


@lru_cache(maxsize=1)
def _rules_by_id() -> dict[str, dict]:
    """rule_id -> {"meta": ..., "clauses": ..., "merged": ...}, loaded once."""
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_normalized_clause(clause_ref: str) -> dict | None:
    """Exact-key lookup into aggregate_normalized_clauses.jsonl."""
    return _clauses_by_ref().get(clause_ref)


def get_rule(rule_id: str) -> dict | None:
    """Exact-key lookup into parsed_rules.json. rule_id is the bare rule
    number, e.g. '2010' -- matches the file's top-level keys, NOT a
    clause_ref."""
    return _rules_by_id().get(rule_id)


def list_rule_metas(category: str | None = None) -> list[dict]:
    """Every rule's meta block (rule_id, name, category, source url),
    optionally filtered by rule_category. Used for discovery -- a client
    that doesn't yet know a specific rule_id/clause_ref can browse this
    before calling search_clauses or fetching a finra-rule:// resource."""
    metas = [entry["meta"] for entry in _rules_by_id().values()]
    if category is None:
        return metas
    return [m for m in metas if m.get("category") == category]


def intro_clause_for_rule(rule_id: str) -> dict | None:
    """The top-level clause for a rule (parent_clause is None). A bare
    cross-reference in another clause's text (e.g. '2111, Suitability')
    points at the rule as a whole, not a specific sub-clause, so this is
    what resolve_cross_references resolves it to."""
    rule = get_rule(rule_id)
    if not rule:
        return None
    clauses = rule.get("merged") or rule.get("clauses") or {}
    for clause in clauses.values():
        if clause.get("parent_clause") is None:
            return clause
    return None
