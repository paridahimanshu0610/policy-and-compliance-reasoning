"""
FINRA Rules Scraper and Normalizer
====================================
Scrapes FINRA rules 2000, 3000, 4000 and normalizes them
into structured JSON for the MCP Compliance Reasoning System.

Usage:
    pip install requests beautifulsoup4 anthropic
    python finra_scraper.py

Output:
    knowledge_base.json  — structured rule clauses ready for MCP ingestion
"""

import re
import json
import time
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from llama_cpp import Llama
import uuid
from collections import Counter
from typing import Any, IO
from config.settings import TARGET_RULES, PARSED_CHECKPOINT, DATA_DIR, MODEL_CONFIGS, HTML_DIR, SERIES_MAP, INFERENCE_BACKEND, TAMU_CONFIG
from config.prompts  import CLAUSE_NORMALISATION_PROMPT


def aggregate_json_objects(objects: list[dict]) -> dict:
    if not objects:
        return {}

    def normalize(value):
        return None if value == "null" else value

    def majority_or_list(values: list) -> Any:
        values = [normalize(v) for v in values]
        non_none = [v for v in values if v is not None]
        none_count = len(values) - len(non_none)
        if not non_none:
            return None
        counter = Counter(non_none)
        max_count = max(counter.values())
        total = len(values)
        if none_count > max_count:
            return None
        top_values = [v for v, c in counter.items() if c == max_count]
        if len(top_values) == 1 and max_count > total / 2:
            return top_values[0]
        result = sorted(top_values)
        if none_count == max_count:
            result = [None] + result
        return result

    def majority_bool(values: list[bool]) -> bool:
        true_count = sum(1 for v in values if v is True)
        false_count = sum(1 for v in values if v is False)
        return True if true_count >= false_count else False

    def aggregate_list_field(list_of_lists: list[list]) -> Any:
        flat = [normalize(item) for sublist in list_of_lists for item in (sublist or [])]
        if not flat:
            return []
        counter = Counter(flat)
        total_lists = len(list_of_lists)
        max_count = max(counter.values())
        top_values = [v for v, c in counter.items() if c == max_count]
        if len(top_values) == 1 and max_count > total_lists / 2:
            return [top_values[0]]
        return sorted(top_values)

    scalar_fields = ["obligated_actor", "regulated_subject", "activity_type", "frequency", "reporting_recipient"]
    bool_fields = ["involves_customer", "involves_third_party", "has_financial_threshold", "documentation_required"]
    list_fields = ["applies_to_firm_type"]

    result = {}
    for field in scalar_fields:
        result[field] = majority_or_list([obj.get(field) for obj in objects])
        if isinstance(result[field], list) and len(result[field]) == 1:
            result[field] = result[field][0]
    for field in bool_fields:
        result[field] = majority_bool([obj.get(field) for obj in objects])
    for field in list_fields:
        result[field] = aggregate_list_field([obj.get(field) or [] for obj in objects])
    return result

# ── Configuration ────────────────────────────────────────────────────────────

# Hidden marker to preserve strong/bold tag boundaries post-HTML extraction
HEADING_MARKER = "\ue000"

def find_html_file(rule_id: str) -> Path | None:
    """
    Locates the saved HTML file for a given rule_id by scanning the
    appropriate series subfolder for any filename containing the rule_id.

    This is tolerant of whatever filename the browser used when saving,
    e.g. "4210. Margin Requirements _ FINRA.org.html" or "4210.html".
    """
    series = SERIES_MAP.get(rule_id)
    if not series:
        return None

    folder = HTML_DIR / series
    if not folder.exists():
        return None

    # Match any file whose name starts with or contains the rule_id
    matches = [f for f in folder.iterdir() if rule_id in f.name and f.suffix == ".html"]
    if not matches:
        return None

    return matches[0]  # take first match if multiple

# ── Step 1: Scraping ──────────────────────────────────────────────────────────

# Tags that introduce a genuine block-level/structural boundary, or that
# receive special handling later in the pipeline (strong/b -> HEADING_MARKER).
# Every other tag found in the source HTML is treated as purely inline/
# cosmetic (underlined spans, hyperlinked cross-references, custom <ref>
# tags for rule citations, italics, superscripts, etc.) and is stripped
# before parsing. Stripping from the raw string is essential: unwrapping
# after parsing does NOT fix the problem because BeautifulSoup's
# get_text(separator="\n") inserts the separator between every text node
# regardless of tag nesting, so even an unwrapped tag's former content
# remains a distinct sibling node and still picks up a spurious "\n".
STRUCTURAL_TAGS = {
    "div", "p", "table", "tr", "td", "th", "tbody", "thead", "tfoot",
    "li", "ul", "ol", "br",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b",
}

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")


def _strip_non_structural_tags(html: str) -> str:
    """
    Removes any HTML/XML-style tag whose name is not in STRUCTURAL_TAGS,
    keeping its text content in place.
    """
    def _replace(match: re.Match) -> str:
        tag_name = match.group(1).lower()
        return match.group(0) if tag_name in STRUCTURAL_TAGS else ""

    return _TAG_RE.sub(_replace, html)


def scrape_rule_page(rule_id: str) -> str:
    """
    Reads a locally saved FINRA rule HTML file and extracts the rule body text.
    Replaces the original HTTP-based scraper — parsing logic is identical.
    """
    html_path = find_html_file(rule_id)

    if not html_path:
        print(f"  ✗ HTML file not found for rule {rule_id} — save the page manually first")
        return ""

    print(f"  Reading: {html_path.name}")

    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  ✗ Failed to read {html_path}: {e}")
        return ""

    # Strip all non-structural inline tags (span, a, ref, em, sup, …) from
    # the raw HTML string before BeautifulSoup parses it. This prevents
    # get_text(separator="\n") from inserting a spurious line break at every
    # tag boundary mid-sentence, which would otherwise make parenthetical
    # cross-references like "(b)(1)" or "(Supervision)" appear at the start
    # of their own lines and be mistaken for genuine clause markers.
    html = _strip_non_structural_tags(html)

    soup = BeautifulSoup(html, "html.parser")

    rule_div = None
    the_rule_pane = soup.find("div", {"id": "the-rule"})
    if the_rule_pane:
        rule_div = the_rule_pane.find(
            "div", class_=lambda c: c and "field--name-body" in c
        )
    if not rule_div:
        rule_div = soup.find("div", class_=lambda c: c and "field--name-body" in c)
    if not rule_div:
        rule_div = soup.find("div", {"id": "block-body"})
    if not rule_div:
        rule_div = soup.find("main")
    if not rule_div:
        print(f"  ✗ Could not locate rule body in {html_path.name} — returning full page text")
        return soup.get_text(separator="\n", strip=True)

    for table in rule_div.find_all("table", class_="footnote"):
        table.decompose()
    for tag in rule_div.find_all(["strong", "b"]):
        tag_text = tag.get_text(separator=" ", strip=True)
        if tag_text:
            tag.replace_with(f"{HEADING_MARKER}{tag_text}{HEADING_MARKER}")
        else:
            tag.decompose()

    text = rule_div.get_text(separator="\n", strip=True)

    # Normalize non-breaking spaces (\xa0 from &nbsp;) to regular spaces.
    # &nbsp; appears throughout FINRA's HTML (e.g. "FINRA&nbsp;Rule 2010").
    # If left as \xa0, the continuation checks in should_merge_upward
    # (endswith(" and"), endswith("; or") etc.) silently miss clauses whose
    # conjunctions were joined with a non-breaking space, causing those
    # fragments to escape the upward merge they need.
    text = text.replace("\xa0", " ")

    return text

# ── Step 2: Clause Splitting ──────────────────────────────────────────────────

import re
from dataclasses import dataclass
from enum import Enum, auto

# ── 1. Sequence classification ────────────────────────────────────────────────

class Seq(Enum):
    DECIMAL      = auto()   # .01, .02 … (Supplementary Material only)
    NUMERIC      = auto()   # (1), (2), (3) …
    ALPHA_UP     = auto()   # (A), (B), (C) …
    ALPHA_LO     = auto()   # (a), (b), (c) …
    ROMAN        = auto()   # (i), (ii), (iii) …
    DOT_NUMERIC  = auto()   # 1., 2., 3. …  (dot-marker sub-levels)
    DOT_ALPHA_UP = auto()   # A., B., C. …
    DOT_ALPHA_LO = auto()   # a., b., c. …

_ROMAN_VALUES: dict[str, int] = {
    "i": 1, "v": 5, "x": 10, "l": 50,
    "c": 100, "d": 500, "m": 1000,
}


def _int_to_roman(n: int) -> str:
    """Standard integer → lowercase roman numeral."""
    table = [
        (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
        (100,  "c"), (90,  "xc"), (50,  "l"), (40,  "xl"),
        (10,   "x"), (9,   "ix"), (5,   "v"), (4,   "iv"), (1, "i"),
    ]
    result = []
    for value, symbol in table:
        count, n = divmod(n, value)
        result.append(symbol * count)
    return "".join(result)


def _roman_to_int(token: str) -> int | None:
    """
    Converts a lowercase roman numeral string to its 1-based integer value,
    or returns None if it isn't a valid roman numeral.

    The original implementation used a static list capped at "xxx" (30).
    Rule 4210(f)(2)(A) has sub-clause labels running through "(xxxvi)" (36),
    so every token past "xxx" fell through to the ALPHA_LO catch-all in
    _interp(), which always returns ordinal 0. _push() then treated each
    one as "the start of a new child list" instead of "the next sibling",
    producing runaway nesting: (xxx) → (xxx)(xxxi) → (xxx)(xxxi)(xxxii) …

    This version computes the value directly with no ceiling.
    The round-trip check (re-encode the integer back to roman and compare)
    rejects malformed strings like "iiii" or "vx".
    """
    token = token.lower()
    if not token or any(ch not in _ROMAN_VALUES for ch in token):
        return None

    total = 0
    prev_value = 0
    for ch in reversed(token):
        value = _ROMAN_VALUES[ch]
        if value < prev_value:
            total -= value
        else:
            total += value
            prev_value = value

    if _int_to_roman(total) != token:
        return None

    return total


def _interp(token: str) -> list[tuple[Seq, int]]:
    """Return every plausible (Seq, 0-based-ordinal) pair for a marker token."""

    # SM decimal marker: .01, .02 …
    if token.startswith("."):
        return [(Seq.DECIMAL, int(token[1:]) - 1)]

    # Dot markers: a., b., A., B., 1., 2. …
    # Exactly one letter or non-zero digit followed by a period.
    # Each has its own unique Seq type so _push never confuses them with
    # their parenthesised equivalents (ALPHA_LO, ALPHA_UP, NUMERIC).
    if re.fullmatch(r"[a-zA-Z1-9]\.", token):
        ch = token[0]
        if ch.isupper():
            return [(Seq.DOT_ALPHA_UP, ord(ch) - ord("A"))]
        elif ch.isdigit():
            return [(Seq.DOT_NUMERIC, int(ch) - 1)]
        else:
            return [(Seq.DOT_ALPHA_LO, ord(ch) - ord("a"))]

    if re.fullmatch(r"\d+", token):
        return [(Seq.NUMERIC, int(token) - 1)]
    if re.fullmatch(r"[A-Z]", token):
        return [(Seq.ALPHA_UP, ord(token) - ord("A"))]

    lo = token.lower()
    out: list[tuple[Seq, int]] = []
    if len(token) == 1:
        out.append((Seq.ALPHA_LO, ord(lo) - ord("a")))

    roman_value = _roman_to_int(lo)
    if roman_value is not None:
        out.append((Seq.ROMAN, roman_value - 1))

    return out or [(Seq.ALPHA_LO, 0)]


@dataclass
class _SE:
    token:   str
    seq:     Seq
    ordinal: int


def _push(stack: list[_SE], token: str) -> list[_SE]:
    """Incorporate one marker token into the hierarchy stack."""
    interps = _interp(token)
    parent  = stack[-1] if stack else None

    # 1. Preferred child: when inside a NUMERIC/ROMAN/ALPHA_UP sequence,
    #    treat a token that could be roman-zero as the start of a roman list.
    if parent is not None and parent.seq in (Seq.NUMERIC, Seq.ROMAN, Seq.ALPHA_UP):
        roman_zero = next(
            ((s, o) for s, o in interps if s is Seq.ROMAN and o == 0), None
        )
        if roman_zero:
            return stack + [_SE(token, *roman_zero)]

    # 2. Ancestor continuation: walk back for a matching sequence type
    #    whose ordinal is exactly one less than ours.
    for i in range(len(stack) - 1, -1, -1):
        e = stack[i]
        for s, o in interps:
            if s is e.seq and o == e.ordinal + 1:
                return stack[:i] + [_SE(token, s, o)]

    # 3. New child
    zero = [(s, o) for s, o in interps if o == 0]
    if zero:
        if parent and parent.seq in (Seq.NUMERIC, Seq.ROMAN, Seq.ALPHA_UP):
            chosen = next(((s, o) for s, o in zero if s is Seq.ROMAN), zero[0])
        else:
            chosen = next(((s, o) for s, o in zero if s is Seq.ALPHA_LO), zero[0])
    else:
        chosen = interps[0]

    return stack + [_SE(token, *chosen)]


# ── 2. Clause Splitting Logic ─────────────────────────────────────────────────

_MARKER_CORE  = r"\([a-zA-Z0-9]+\)(?:\([a-zA-Z0-9]+\))*"
_DECIMAL_CORE = r"\.[0-9]+"

# Dot markers: a single letter or non-zero digit followed by a period,
# e.g. "a.", "b.", "1.", "2.", "A.", "B.".
#
# False-positive guard: the [ \t]+\S suffix in CLAUSE_PATTERN requires one
# or more spaces then a visible character after the dot, so:
#   • "e.g. something" does NOT match  ("e." is followed by "g", not a space)
#   • "(f)(2)(H)(i)b. of this Rule" does NOT match  (mid-line, not line-start)
#   • ".35 percent" does NOT match  (starts with ".", not a letter/digit)
#   • "1.5 percent" does NOT match  ("1." is followed by "5", not a space)
# Only "a. the long positions…" at the very start of a line matches.
_DOT_MARKER_CORE = r"[a-zA-Z1-9]\."

# Two CLAUSE_PATTERN variants so that ".NN" decimals (Supplementary Material
# numbering like ".01") are only recognised as markers inside the SM section.
# In the main rule body they would otherwise collide with percentage values
# written without a leading zero (".35 percent" in margin tables).
CLAUSE_PATTERN_MAIN = re.compile(
    rf"(?m)(?=^\s*{HEADING_MARKER}?(?:{_MARKER_CORE}|{_DOT_MARKER_CORE}[ \t]+\S))"
)
CLAUSE_PATTERN_SM = re.compile(
    rf"(?m)(?=^\s*{HEADING_MARKER}?(?:{_MARKER_CORE}|{_DECIMAL_CORE}(?:[ \t]|$)|{_DOT_MARKER_CORE}[ \t]+\S))"
)

# REF_MATCH uses a lookahead (?=[ \t]) for the dot branch: the trailing
# space is confirmed but NOT consumed, so body = seg[ref_match.end():].strip()
# works identically to the paren-marker path.
REF_MATCH_MAIN = re.compile(
    rf"^{HEADING_MARKER}?((?:{_MARKER_CORE})|(?:{_DOT_MARKER_CORE})(?=[ \t]))"
)
REF_MATCH_SM = re.compile(
    rf"^{HEADING_MARKER}?((?:{_MARKER_CORE})|(?:{_DECIMAL_CORE})|(?:{_DOT_MARKER_CORE})(?=[ \t]))"
)

# Seq types whose tokens are written verbatim in clause_ref (no parens).
_DOT_SEQS = (Seq.DECIMAL, Seq.DOT_NUMERIC, Seq.DOT_ALPHA_UP, Seq.DOT_ALPHA_LO)


def get_parent_clause(clause_ref: str) -> str | None:
    """
    Returns the parent clause_ref for a given FINRA clause_ref.
    Returns None if the clause is a topmost node or has no parent.
    """
    # Match the last marker segment at the end of the ref — paren marker
    # "(xyz)", SM decimal ".01", or dot marker "a." / "1." / "A."
    pattern = r'(\([a-zA-Z0-9]+\)|\.[0-9]+|[a-zA-Z1-9]\.)$'

    match = re.search(pattern, clause_ref)
    if not match:
        return None

    parent_ref = clause_ref[:match.start()]

    if re.fullmatch(r'FINRA-\d+(?:-SM)?', parent_ref):
        return None

    return parent_ref


def split_text_block(text: str, rule_id: str, is_sm: bool) -> list[dict]:
    """Worker function to process a single block of text (Main or SM)."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    clause_pattern = CLAUSE_PATTERN_SM if is_sm else CLAUSE_PATTERN_MAIN
    ref_pattern    = REF_MATCH_SM     if is_sm else REF_MATCH_MAIN

    segments = clause_pattern.split(text)
    clauses  = []
    stack: list[_SE] = []

    base_prefix = f"FINRA-{rule_id}-SM" if is_sm else f"FINRA-{rule_id}"

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        clause_heading = ""

        # Grab the first line to look for our flattened heading string
        first_line = seg.split('\n', 1)[0]
        strong_matches = re.findall(
            rf"{HEADING_MARKER}([^{HEADING_MARKER}]+){HEADING_MARKER}", first_line
        )

        ref_match = ref_pattern.match(seg)

        if ref_match:
            full_marker_str = ref_match.group(1)

            # ── Extract Heading ──────────────────────────────────────────────
            if strong_matches:
                raw_heading = strong_matches[0].strip()
                if raw_heading.startswith(full_marker_str):
                    clause_heading = raw_heading[len(full_marker_str):].strip()
                else:
                    clause_heading = raw_heading

            # ── Extract marker tokens and push onto the hierarchy stack ──────
            if full_marker_str.startswith("."):
                # SM decimal: .01, .02 — single token, no parens
                markers = [full_marker_str]
            elif re.fullmatch(r"[a-zA-Z1-9]\.", full_marker_str):
                # Dot marker: a., 1., A. — single token, no parens to strip
                markers = [full_marker_str]
            else:
                # Paren marker: (a), (ii), (e)(2)(F) — extract content inside parens
                markers = re.findall(r"\(([a-zA-Z0-9]+)\)", full_marker_str)

            for m in markers:
                stack = _push(stack, m)

            # ── Build clause_ref ─────────────────────────────────────────────
            # Dot-family and SM-decimal tokens are written verbatim;
            # paren-marker tokens are re-wrapped in parentheses.
            nested_ref = "".join(
                e.token if e.seq in _DOT_SEQS else f"({e.token})"
                for e in stack
            )
            clause_ref = f"{base_prefix}{nested_ref}"

            body = seg[ref_match.end():].strip()
        else:
            if strong_matches:
                clause_heading = strong_matches[0].strip()
            clause_ref = f"{base_prefix}-intro"
            body = seg

        # Clean all residual hidden markers so they don't show up in raw_text
        body_clean = body.replace(HEADING_MARKER, "").strip()

        if len(seg) < 40 and not body_clean:
            continue

        clauses.append({
            "clause_ref":    clause_ref,
            "parent_clause": get_parent_clause(clause_ref),
            "raw_text":      body_clean,
            "clause_heading": clause_heading,
        })

    return clauses


# ── 3. The Orchestrator ───────────────────────────────────────────────────────

def parse_finra_rule(text: str, rule_id: str) -> list[dict]:
    """
    Splits the document into Main Text and Supplementary Material,
    then processes both streams through the clause chunker.

    The raw_text key in the resulting clause dicts is the original
    unmodified text from the rule. It is not accumulated or merged in
    any way.
    """
    sm_separator = re.compile(
        rf"(?m)^[ \t{HEADING_MARKER}]*•[\s\xa0]*•[\s\xa0]*•[\s\xa0{HEADING_MARKER}]*Supplementary Material[^\n]*$"
    )

    parts = sm_separator.split(text, maxsplit=1)

    # Process the Main Rule text
    main_text  = parts[0]
    all_clauses = split_text_block(main_text, rule_id, is_sm=False)

    # If Supplementary Material was found, process it and append
    if len(parts) > 1:
        sm_text    = parts[1]
        sm_clauses = split_text_block(sm_text, rule_id, is_sm=True)
        all_clauses.extend(sm_clauses)

    return all_clauses


def should_merge_upward(raw_text: str, clause_heading: str = "") -> bool:
    """
    Evaluates whether a clause is a fragment that cannot stand alone.

    IMPORTANT: Always call this with the ORIGINAL raw_text from
    all_clauses — never with accumulated or merged text.
    This is what prevents the trap.

    Note: "ends with colon" is intentionally NOT a condition here.
    A clause ending with ":" is a setup sentence whose children should
    merge INTO it — that is a downward concern. The clause itself may
    still be complete enough to stop upward merging.
    """
    # Normalize non-breaking spaces before any string checks.
    # &nbsp; (\xa0) appears in FINRA's HTML as a word-joiner, e.g.
    # "FINRA\xa0Rule 2010" or "records;\xa0and". The endswith() checks
    # below use plain spaces, so without this step a clause ending with
    # ";\xa0and" would silently pass all continuation checks and escape
    # the upward merge it needs.
    text = raw_text.replace("\xa0", " ").strip()

    if not text:
        return True

    # Condition 1: Pure heading with no obligation content
    if clause_heading.strip() and text == clause_heading.strip():
        return True

    # Condition 2: Too short to be a standalone obligation
    if len(text.split()) < 20:
        return True

    # Condition 3: List item — ends with semicolon or semicolon + conjunction
    if text.rstrip().endswith(";"):
        return True
    if text.rstrip().endswith("; and") or text.rstrip().endswith("; or"):
        return True

    # Condition 4: Sentence continuation — clause is mid-sentence
    if text.rstrip().endswith(" and") or text.rstrip().endswith(" or"):
        return True

    # Condition 5: Starts mid-sentence — clearly a fragment
    if text[0].islower():
        return True

    return False


def merge_clause_to_completion(start_ref: str, all_clauses: dict, merge_until_root=False) -> dict:
    """
    Starting from start_ref, walks upward through the ancestor chain,
    collecting nodes until it finds one whose ORIGINAL text is
    self-complete, or until the root is reached.

    Trap avoidance mechanism
    ─────────────────────────
    should_merge_upward is always called on all_clauses[ref]["raw_text"]
    — the original unmodified text stored in the dict — never on the
    accumulated merged text being built up.

    This means that trailing punctuation inherited from a child clause
    (e.g. "; and") cannot trigger a false positive on a parent whose
    original text is already complete.
    """
    chain = []
    current_ref = start_ref

    while current_ref and current_ref in all_clauses:
        node = all_clauses[current_ref]
        chain.append(node)

        original_text = node["raw_text"]
        heading       = node.get("clause_heading", "")

        if (not merge_until_root) and (not should_merge_upward(original_text, heading)):
            break

        parent_ref = node.get("parent_clause")
        if not parent_ref or parent_ref not in all_clauses:
            break

        current_ref = parent_ref

    chain.reverse()

    merged_text = "\n".join(node["raw_text"].strip() for node in chain)

    result = all_clauses[start_ref].copy()
    result["raw_text"]     = merged_text
    result["clause_ref"]   = start_ref
    result["merged_up_to"] = chain[0]["clause_ref"]

    return result


def build_merged_clause_set(all_clauses: dict) -> dict:
    """
    Applies merge_clause_to_completion to every clause in all_clauses
    and returns a new dict of merged clauses keyed by clause_ref.
    """
    merged = {}
    for ref in all_clauses:
        merged[ref] = merge_clause_to_completion(ref, all_clauses)
    return merged


def build_context_bundle(clause_ref: str, all_clauses: dict) -> dict:
    """
    Builds a full ancestor chain from the target clause up to the root.
    """
    ancestors = []
    clause    = all_clauses.get(clause_ref)
    current_ref = clause.get("parent_clause")

    while current_ref and current_ref in all_clauses:
        ancestor = all_clauses[current_ref]
        ancestors.append({
            "clause_ref":    ancestor["clause_ref"],
            "raw_text":      ancestor["raw_text"],
            "parent_clause": ancestor.get("parent_clause"),
        })
        current_ref = ancestor.get("parent_clause")

    ancestors.reverse()

    for i, ancestor in enumerate(ancestors):
        ancestor["level"] = i + 1

    target_level = len(ancestors) + 1

    return {
        "ancestors": ancestors,
        "target_clause": {
            **clause,
            "level": target_level,
        },
    }


# ── Bundle Formatter ──────────────────────────────────────────────────────────

def format_bundle_for_prompt(bundle: dict) -> str:
    sections = []

    if bundle["ancestors"]:
        ancestor_lines = []
        for ancestor in bundle["ancestors"]:
            indent = "  " * (ancestor["level"] - 1)
            ancestor_lines.append(
                f"{indent}[Level {ancestor['level']} — "
                f"{ancestor['clause_ref']}]:\n"
                f"{indent}{ancestor['raw_text'].strip()}"
            )
        sections.append(
            "ANCESTOR CONTEXT (root first, immediate parent last):\n"
            + "\n\n".join(ancestor_lines)
        )
    else:
        sections.append("ANCESTOR CONTEXT: None — this is a root clause.")

    target = bundle["target_clause"]
    sections.append(
        f"[Level {target['level']} — TARGET CLAUSE {target['clause_ref']}]"
        f" — THIS IS WHAT YOU ARE STRUCTURING:\n"
        f"{target['raw_text'].strip()}"
    )

    sections.append(
        "IMPORTANT: Fill the schema based on the TARGET CLAUSE only.\n"
        "Use ancestor context solely to understand its meaning.\n"
        "Do not include ancestor text in the 'text' field of the output."
    )

    separator = "\n\n" + "=" * 60 + "\n\n"
    divider   = "\n\n" + "-" * 40 + "\n\n"

    return separator + divider.join(sections) + separator


# ── Prompt Builder ────────────────────────────────────────────────────────────

def build_normalisation_prompt(
    target:      dict,
    rule_id:     str,
    rule_name:   str,
    all_clauses: dict,
) -> str:
    target_clause = target.get("raw_text", "")

    clause_ref = target["clause_ref"]
    parent_ref = target.get("parent_clause") or "null"

    context_text = merge_clause_to_completion(clause_ref, all_clauses, merge_until_root=True)
    context_text = context_text.get("raw_text", "")

    return CLAUSE_NORMALISATION_PROMPT.format(
        rule_id       = rule_id,
        rule_name     = rule_name,
        parent_ref    = parent_ref,
        clause_ref    = clause_ref,
        target_clause = target_clause,
        context_text  = context_text,
    )


def _parse_tamu_content(raw) -> str:
    # Already a parsed SDK object
    if hasattr(raw, "choices"):
        return raw.choices[0].message.content or ""

    # Raw SSE string fallback
    for line in str(raw).split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        json_str = line[len("data:"):].strip()
        if json_str == "[DONE]":
            continue
        try:
            chunk = json.loads(json_str)
            content = chunk["choices"][0]["delta"].get("content", "")
            if content:
                return content
        except (KeyError, IndexError, json.JSONDecodeError):
            continue

    return ""


# ── Model Configuration ───────────────────────────────────────────────────────

class _TAMUBackend:
    """
    Wraps the TAMU Chat API (OpenAI-compatible) so it presents the same
    .create_chat_completion(messages, temperature, max_tokens) interface
    as llama_cpp.Llama — no changes needed in normalize_clause.
    """
    def __init__(self, model_name = TAMU_CONFIG["model"]):
        from openai import OpenAI
        self._client = OpenAI(
            api_key  = TAMU_CONFIG["api_key"],
            base_url = TAMU_CONFIG["base_url"],
        )
        self._model = model_name

    def create_chat_completion(
        self,
        messages:    list[dict],
        temperature: float = 0.0,
        top_p:      float = 1.0,
        frequency_penalty: float = 0.0,
        presence_penalty:  float = 0.0,
        max_tokens:  int   = 16384,
    ) -> dict:
        """Returns a dict shaped like llama_cpp's chat completion output."""

        if self._model == "protected.Claude Opus 4.7": 
            raw = self._client.chat.completions.create(
                model       = self._model,
                messages    = messages,
                max_tokens  = max_tokens,
            )
        else:
            raw = self._client.chat.completions.create(
                model       = self._model,
                messages    = messages,
                temperature = temperature,
                top_p = 1.0, 
                frequency_penalty = 0.0,
                presence_penalty = 0.0,
                max_tokens  = max_tokens,
            )

        # Parse the raw SSE/response string if it comes back as text
        content = _parse_tamu_content(raw)
        # Re-wrap into llama_cpp-compatible shape
        return {
            "choices": [
                {"message": {"content": content}}
            ]
        }


def load_normalizer_model(model_name: str = "qwen"):
    """
    Returns a backend instance — either a local Llama or a _TAMUBackend —
    depending on INFERENCE_BACKEND in settings.py.
    """
    if INFERENCE_BACKEND == "tamu":
        print(f"  Using TAMU inference backend: {model_name}")
        return _TAMUBackend(model_name=model_name)

    if model_name not in MODEL_CONFIGS:
        raise ValueError(
            f"Unknown model '{model_name}'. "
            f"Choose from: {list(MODEL_CONFIGS)}"
        )
    path = MODEL_CONFIGS[model_name]["path"]
    print(f"  Loading local normaliser model : {model_name}")
    print(f"  Path                           : {path}")
    return Llama(
        model_path   = path,
        n_ctx        = 16384,
        n_gpu_layers = -1,
        verbose      = False,
    )


# ── Step 3: LLM Normalisation ─────────────────────────────────────────────────

def normalize_clause(
    model,
    prompt:      str,
    max_retries: int = 3,
) -> dict | None:

    messages = [{"role": "user", "content": prompt}]

    for attempt in range(1, max_retries + 1):
        try:
            response = model.create_chat_completion(
                messages    = messages,
                temperature = 0.0,
                max_tokens  = 16384,
            )
            raw = response["choices"][0]["message"]["content"].strip()

            # Guard: empty response
            if not raw:
                print(f"    ✗ Empty response (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(1)
                continue

            # Strip <think>...</think> blocks (Gemini 2.5 Pro)
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

            # Guard: response was only a <think> block — likely truncated mid-think
            if not raw:
                print(f"    ✗ Response was only a <think> block — likely truncated (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(1)
                continue

            # Strip markdown code fences
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$",           "", raw)
            raw = raw.strip()

            # Guard: truncated JSON (valid JSON must end with } or ])
            if not raw.rstrip().endswith(('}', ']')):
                print(f"    ✗ Response appears truncated — doesn't end with '}}' or ']' (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(1)
                continue

            return json.loads(raw)

        except json.JSONDecodeError as e:
            print(f"    ✗ JSON parse error (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(1)

        except Exception as e:
            print(f"    ✗ Inference error (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(1)

    return None


# ── Step 4: Document Assembly ─────────────────────────────────────────────────

def _safe_str(value) -> str:
    """Converts a value to a ChromaDB-safe string."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def assemble_document(
    raw_clause:    dict,
    merged_clause: dict,
    rule_meta:     dict,
    normalized:    dict,
) -> dict:
    """
    Merges raw clause fields, merged clause text, rule metadata, and
    LLM-normalised fields into a single flat document ready for ChromaDB.
    """
    merged_text = merged_clause.get("raw_text") or raw_clause.get("raw_text", "")

    return {
        "id":       _safe_str(uuid.uuid4()),
        "document": merged_text,

        "clause_ref":     raw_clause["clause_ref"],
        "parent_clause":  _safe_str(raw_clause.get("parent_clause")),
        "clause_heading": _safe_str(raw_clause.get("clause_heading")),
        "merged_up_to":   _safe_str(
            merged_clause.get("merged_up_to", raw_clause["clause_ref"])
        ),
        "rule_id":        rule_meta["rule_id"],
        "rule_name":      rule_meta["name"],
        "regulator":      "FINRA",

        "obligated_actor":      _safe_str(normalized.get("obligated_actor")),
        "regulated_subject":    _safe_str(normalized.get("regulated_subject")),
        "activity_type":        _safe_str(normalized.get("activity_type")),
        "frequency":            _safe_str(normalized.get("frequency")),
        "reporting_recipient":  _safe_str(normalized.get("reporting_recipient")),

        "applies_to_firm_type": _safe_str(normalized.get("applies_to_firm_type")),

        "involves_customer":       bool(normalized.get("involves_customer",       False)),
        "involves_third_party":    bool(normalized.get("involves_third_party",    False)),
        "has_financial_threshold": bool(normalized.get("has_financial_threshold", False)),
        "documentation_required":  bool(normalized.get("documentation_required",  False)),
    }


# ── Steps 1 & 2: Scraping Pipeline ───────────────────────────────────────────

def run_scraping_pipeline() -> dict:
    """
    Iterates over TARGET_RULES, reads each locally saved HTML file,
    parses the raw text into clause dicts, and builds merged clause sets.

    Saves a checkpoint to PARSED_CHECKPOINT after all rules are processed.
    This checkpoint is the input to run_normalization_pipeline.
    """
    PARSED_CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    all_rules: dict = {}

    for rule in TARGET_RULES:
        rule_id = rule["rule_id"]
        print(f"\n  Rule {rule_id}: {rule['name']}")

        raw_text = scrape_rule_page(rule_id)
        if not raw_text:
            print(f"    ✗ Skipping — no content retrieved")
            continue

        clauses_list = parse_finra_rule(raw_text, rule_id)
        if not clauses_list:
            print(f"    ✗ Skipping — no clauses parsed from raw text")
            continue

        clauses_dict = {c["clause_ref"]: c for c in clauses_list}
        merged_dict  = build_merged_clause_set(clauses_dict)

        all_rules[rule_id] = {
            "meta":    rule,
            "clauses": clauses_dict,
            "merged":  merged_dict,
        }
        print(f"    ✓ {len(clauses_dict)} clauses parsed")

    if all_rules:
        with open(PARSED_CHECKPOINT, "w") as f:
            json.dump(all_rules, f, indent=2, ensure_ascii=False)
        print(f"\n  ✓ Scraping checkpoint saved → {PARSED_CHECKPOINT}")
    else:
        print("\n  ✗ No rules were successfully scraped.")

    return all_rules

def saving_aggregated_results(models: list[str]):
    """
    saving the aggregated results of all models into a single jsonl file for further processing.
    Input: list of model names. E.g. ["protected.o3", "protected.Claude Opus 4.7", "protected.gpt-5", "protected.gemini-2.5-pro"]
    Output: a single jsonl file containing the aggregated results of all models
    """
    
    def checkpoint_for(model_name: str) -> Path:
        safe_name = model_name.replace("protected.", "")
        return DATA_DIR / f"normalized_{safe_name}.jsonl"

    model_results = {}
    for model_name in models:
        path = checkpoint_for(model_name)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                model_results[model_name] = [json.loads(line) for line in f]
                model_results[model_name] = {temp["clause_ref"] : temp for temp in model_results[model_name]}

    output_path = DATA_DIR + "normalized_aggregated_clause.jsonl"
    other_agg_fields = ['id','document','clause_ref','parent_clause','clause_heading','merged_up_to','rule_id','rule_name','regulator']

    all_clause_refs = model_results[models[0]].keys() 
    with open(output_path, "w", encoding="utf-8") as fout:
        for clause_ref in all_clause_refs:
            other_field_vals = {field: model_results[models[0]][clause_ref].get(field) for field in other_agg_fields}
            agg_clause = aggregate_json_objects([model_results[model_name][clause_ref] for model_name in models])

            record = {
                "clause_ref": clause_ref,
                **other_field_vals,
                **agg_clause
            }

            fout.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Step 3: Normalisation Pipeline ───────────────────────────────────────────

def run_normalization_pipeline(
    models: list[_TAMUBackend],
    all_rules: dict,
) -> dict[str, list[dict]]:
    """
    Normalises every clause in all_rules using each backend model in `models`.
    Iterates rule-by-rule; for each clause, runs all models before moving on.
    Supports resume: already-normalised clause_refs per model are skipped.
    A 20s gap is inserted between successive normalize_clause calls.
    """

    # ------------------------------------------------------------------ #
    # 1. Build per-model checkpoint paths & resume state                  #
    # ------------------------------------------------------------------ #
    def checkpoint_for(model_name: str) -> Path:
        safe_name = model_name.replace("protected.", "")
        return DATA_DIR / f"normalized_{safe_name}.jsonl"

    model_state: dict[str, dict] = {}

    for model in models:
        model_name = model._model  # extract string name from instance
        ckpt = checkpoint_for(model_name)
        ckpt.parent.mkdir(parents=True, exist_ok=True)

        processed_refs: set[str]   = set()
        existing_docs:  list[dict] = []

        if ckpt.exists():
            with open(ckpt, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        doc = json.loads(line)
                        processed_refs.add(doc["clause_ref"])
                        existing_docs.append(doc)
            if processed_refs:
                print(f"  [{model_name}] Resuming: {len(processed_refs)} clauses already normalised")

        total     = sum(len(r["clauses"]) for r in all_rules.values())
        remaining = total - len(processed_refs)
        print(
            f"  [{model_name}] Total clauses: {total}  |  "
            f"Already done: {len(processed_refs)}  |  Remaining: {remaining}"
        )

        model_state[model_name] = {
            "instance":       model,        # _TAMUBackend instance for normalize_clause
            "ckpt":           ckpt,
            "processed_refs": processed_refs,
            "existing_docs":  existing_docs,
            "new_docs":       [],
            "skipped":        [],
            "done_count":     len(processed_refs),
            "total":          total,
            "remaining":      remaining,
        }

    # ------------------------------------------------------------------ #
    # 2. Open one output file handle per model                            #
    # ------------------------------------------------------------------ #
    out_handles: dict[str, IO] = {
        model_name: open(state["ckpt"], "a")
        for model_name, state in model_state.items()
    }

    try:
        # -------------------------------------------------------------- #
        # 3. Outer loop: rule → clause  (preserve original iteration order)
        # -------------------------------------------------------------- #
        first_call = True

        for rule_id, rule_data in all_rules.items():
            rule_meta    = rule_data["meta"]
            clauses_dict = rule_data["clauses"]
            merged_dict  = rule_data["merged"]

            for clause_ref, raw_clause in clauses_dict.items():

                # Determine which models still need this clause
                pending_model_names = [
                    model_name for model_name, state in model_state.items()
                    if clause_ref not in state["processed_refs"]
                ]
                if not pending_model_names:
                    continue

                prompt = build_normalisation_prompt(
                    target      = raw_clause,
                    rule_id     = rule_id,
                    rule_name   = rule_meta["name"],
                    all_clauses = clauses_dict,
                )

                # -------------------------------------------------------- #
                # 4. Inner loop: run each pending model for this clause     #
                # -------------------------------------------------------- #
                for model_name in pending_model_names:
                    state = model_state[model_name]

                    if not first_call:
                        print(f"    Waiting 20s before next call...", flush=True)
                        time.sleep(20)
                    first_call = False

                    state["done_count"] += 1
                    print(
                        f"  [{model_name}] [{state['done_count']}/{state['total']}] "
                        f"{clause_ref} ...",
                        end="\n", flush=True,
                    )

                    normalized = normalize_clause(state["instance"], prompt)  # pass instance

                    if normalized is None:
                        print("✗ skipped (normalisation failed after retries)")
                        state["skipped"].append(clause_ref)
                        continue

                    merged_clause = merged_dict.get(clause_ref, raw_clause)
                    doc = assemble_document(
                        raw_clause    = raw_clause,
                        merged_clause = merged_clause,
                        rule_meta     = rule_meta,
                        normalized    = normalized,
                    )

                    out_handles[model_name].write(json.dumps(doc, ensure_ascii=False) + "\n")
                    out_handles[model_name].flush()
                    state["new_docs"].append(doc)
                    print(f"✓ normalised and saved {clause_ref} for {model_name}")

    finally:
        for fh in out_handles.values():
            fh.close()

    # ------------------------------------------------------------------ #
    # 5. Summary & return                                                  #
    # ------------------------------------------------------------------ #
    results: dict[str, list[dict]] = {}

    for model_name, state in model_state.items():
        print(f"\n  ✓ [{model_name}] Normalisation run complete.")
        print(f"    Newly normalised : {len(state['new_docs'])}")
        print(f"    Skipped (failed) : {len(state['skipped'])}")
        if state["skipped"]:
            print("    Failed refs:")
            for ref in state["skipped"]:
                print(f"      {ref}")
        print(f"  ✓ Checkpoint updated → {state['ckpt']}")

        results[model_name] = state["existing_docs"] + state["new_docs"]

    return results

if __name__ == "__main__":
    with open(PARSED_CHECKPOINT) as f:
        all_rules = json.load(f)

    all_rules = {k: v for k, v in all_rules.items()}
    models     = [load_normalizer_model(model_name) for model_name in ["protected.o3", "protected.Claude Opus 4.7", "protected.gpt-5", "protected.gemini-2.5-pro"]]
    print(f"\n  ✓ Loaded {len(models)} normaliser models: {[m._model for m in models]}")
    print(f"  ✓ Running normalisation pipeline on {len(all_rules)} rules: {all_rules.keys()}")
    documents = run_normalization_pipeline(models, all_rules)