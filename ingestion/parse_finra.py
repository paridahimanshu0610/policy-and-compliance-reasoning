"""
FINRA Rules Scraper and Normalizer
====================================
Scrapes FINRA rules 3110, 3120, 3130, 4511 and normalizes them
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
from config.settings import TARGET_RULES, SCRAPER_HEADERS, PARSED_CHECKPOINT, NORMALIZED_CHECKPOINT, MODEL_CONFIGS, HTML_DIR, SERIES_MAP, INFERENCE_BACKEND, TAMU_CONFIG
from config.prompts  import CLAUSE_NORMALISATION_PROMPT

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

# Tags that introduce a genuine block-level/structural boundary, or that
# receive special handling later in the pipeline (strong/b -> HEADING_MARKER).
# Every other tag found in the source HTML is treated as purely inline/
# cosmetic (underline spans, hyperlinked cross-references, custom <ref>
# tags, italics, superscripts, etc.) and is stripped before parsing.
STRUCTURAL_TAGS = {
    "div", "p", "table", "tr", "td", "th", "tbody", "thead", "tfoot",
    "li", "ul", "ol", "br",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b",
}

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")


def _strip_non_structural_tags(html: str) -> str:
    """
    Removes any tag whose name is not in STRUCTURAL_TAGS, keeping its text
    content in place. Operates on the raw string before BeautifulSoup
    parses it, so the surrounding text merges into a single text node
    instead of remaining fragmented across a tag boundary -- which is what
    matters, since get_text(separator=...) separates per text node
    regardless of tag nesting.
    """
    def _replace(match: re.Match) -> str:
        tag_name = match.group(1).lower()
        return match.group(0) if tag_name in STRUCTURAL_TAGS else ""

    return _TAG_RE.sub(_replace, html)

# ── Step 1: Scraping ──────────────────────────────────────────────────────────

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

    return rule_div.get_text(separator="\n", strip=True)

# ── Step 2: Clause Splitting ──────────────────────────────────────────────────

import re
from dataclasses import dataclass
from enum import Enum, auto

# ── 1. Sequence classification (Updated with Decimal) ─────────────────────────

class Seq(Enum):
    DECIMAL  = auto()   # .01, .02, .03 … (For Supplementary Material)
    NUMERIC  = auto()   # 1, 2, 3 …
    ALPHA_UP = auto()   # A, B, C …
    ALPHA_LO = auto()   # a, b, c … (plain letter)
    ROMAN    = auto()   # i, ii, iii, iv, v …

_ROMAN_VALUES: dict[str, int] = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}

def _int_to_roman(n: int) -> str:
    table = [
        (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
        (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
        (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
    ]
    result = []
    for value, symbol in table:
        count, n = divmod(n, value)
        result.append(symbol * count)
    return "".join(result)

def _roman_to_int(token: str) -> int | None:
    """
    Converts a lowercase roman numeral to its 1-based integer value, or
    None if invalid. No hardcoded ceiling -- the previous static list
    stopped at "xxx" (30), so every marker past it (4210(f)(2)(A) runs
    through "xxxvi") fell through to the ALPHA_LO catch-all, which always
    returns ordinal 0, so _push() treated each one as the start of a new
    child list instead of the next sibling: (xxx) -> (xxx)(xxxi) -> ...
    The round-trip check rejects malformed input like "iiii" or "vx".
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
    if token.startswith("."):
        return [(Seq.DECIMAL, int(token[1:]) - 1)]
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

    # 1. Preferred child
    if parent is not None and parent.seq in (Seq.NUMERIC, Seq.ROMAN, Seq.ALPHA_UP):
        roman_zero = next(
            ((s, o) for s, o in interps if s is Seq.ROMAN and o == 0), None
        )
        if roman_zero:
            return stack + [_SE(token, *roman_zero)]

    # 2. Ancestor continuation
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

class _TAMUBackend:
    """
    Wraps the TAMU Chat API (OpenAI-compatible) so it presents the same
    .create_chat_completion(messages, temperature, max_tokens) interface
    as llama_cpp.Llama — no changes needed in normalize_clause.
    """
    def __init__(self):
        from openai import OpenAI
        self._client = OpenAI(
            api_key  = TAMU_CONFIG["api_key"],
            base_url = TAMU_CONFIG["base_url"],
        )
        self._model = TAMU_CONFIG["model"]

    def create_chat_completion(
        self,
        messages:    list[dict],
        temperature: float = 0.0,
        max_tokens:  int   = 4096,
    ) -> dict:
        """Returns a dict shaped like llama_cpp's chat completion output."""
        raw = self._client.chat.completions.create(
            model       = self._model,
            messages    = messages,
            temperature = temperature,
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
    
# ── 2. Clause Splitting Logic ─────────────────────────────────────────────────

# The decimal marker (".01", ".02", ...) exists to catch FINRA's
# Supplementary Material numbering, but it's syntactically identical to a
# plain decimal with no leading zero (".35 percent" in a margin table).
# FINRA only ever uses ".NN" as a structural marker inside the dedicated
# Supplementary Material section, so we build is_sm-aware variants and pick
# the right one in split_text_block, instead of one pattern for both.
_MARKER_CORE  = r"\([a-zA-Z0-9]+\)(?:\([a-zA-Z0-9]+\))*"
_DECIMAL_CORE = r"\.[0-9]+"

CLAUSE_PATTERN_MAIN = re.compile(
    rf"(?m)(?=^\s*{HEADING_MARKER}?{_MARKER_CORE})"
)
CLAUSE_PATTERN_SM = re.compile(
    rf"(?m)(?=^\s*{HEADING_MARKER}?(?:{_MARKER_CORE}|{_DECIMAL_CORE}(?:[ \t]|$)))"
)

REF_MATCH_MAIN = re.compile(rf"^{HEADING_MARKER}?({_MARKER_CORE})")
REF_MATCH_SM   = re.compile(rf"^{HEADING_MARKER}?({_MARKER_CORE}|{_DECIMAL_CORE})")


def get_parent_clause(clause_ref: str) -> str | None:
    """
    Returns the parent clause_ref for a given FINRA clause_ref.
    Returns None if the clause is a topmost node or has no parent.
    """
    # Regex targets the last marker at the end of the string: 
    # either parentheses like (A), (1), (a) OR a decimal like .01
    pattern = r'(\([a-zA-Z0-9]+\)|\.[0-9]+)$'
    
    match = re.search(pattern, clause_ref)
    
    # If no marker is found at the end (e.g., "FINRA-3110-intro")
    if not match:
        return None
        
    # Strip the last marker
    parent_ref = clause_ref[:match.start()]
    
    # Check if what remains is just the base rule prefix (e.g., "FINRA-3110" or "FINRA-3110-SM")
    if re.fullmatch(r'FINRA-\d+(?:-SM)?', parent_ref):
        return None
        
    return parent_ref

def split_text_block(text: str, rule_id: str, is_sm: bool) -> list[dict]:
    """Worker function to process a single block of text (Main or SM)."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    clause_pattern = CLAUSE_PATTERN_SM if is_sm else CLAUSE_PATTERN_MAIN
    ref_pattern     = REF_MATCH_SM if is_sm else REF_MATCH_MAIN

    segments = clause_pattern.split(text)   # was: CLAUSE_PATTERN.split(text)

    clauses = []
    stack: list[_SE] = []

    base_prefix = f"FINRA-{rule_id}-SM" if is_sm else f"FINRA-{rule_id}"

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        clause_heading = ""
        
        # Grab the first line to look for our flattened heading string
        first_line = seg.split('\n', 1)[0]
        strong_matches = re.findall(rf"{HEADING_MARKER}([^{HEADING_MARKER}]+){HEADING_MARKER}", first_line)

        # Extract leading marker (accounting for an optional hidden heading marker)
        ref_match = ref_pattern.match(seg)
        
        if ref_match:
            full_marker_str = ref_match.group(1)
            
            # --- Extract Heading ---
            if strong_matches:
                raw_heading = strong_matches[0].strip()
                # If the tag contained the marker (e.g. "<strong>(a) System</strong>") strip it
                if raw_heading.startswith(full_marker_str):
                    clause_heading = raw_heading[len(full_marker_str):].strip()
                else:
                    clause_heading = raw_heading
            # -----------------------

            if full_marker_str.startswith("."):
                markers = [full_marker_str]
            else:
                markers = re.findall(r"\(([a-zA-Z0-9]+)\)", full_marker_str)
            
            for m in markers:
                stack = _push(stack, m)
            
            nested_ref = "".join((e.token if e.token.startswith(".") else f"({e.token})") for e in stack)
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
            "clause_ref": clause_ref,
            "parent_clause": get_parent_clause(clause_ref),
            "raw_text": body_clean,
            "clause_heading": clause_heading
        })

    return clauses

# ── 3. The Orchestrator ───────────────────────────────────────────────────────

def parse_finra_rule(text: str, rule_id: str) -> list[dict]:
    """
    Splits the document into Main Text and Supplementary Material, 
    then processes both streams through the clause chunker.

    The raw_text key in the resulting clause dicts is the original unmodified text from the rule. It is not accumulated or merged in any way.
    """
    # Updated regex to tolerate the hidden HEADING_MARKER (\ue000) 
    # anywhere before the bullets or before the text itself.
    sm_separator = re.compile(
        rf"(?m)^[ \t{HEADING_MARKER}]*•[\s\xa0]*•[\s\xa0]*•[\s\xa0{HEADING_MARKER}]*Supplementary Material[^\n]*$"
    )
    
    parts = sm_separator.split(text, maxsplit=1)
    
    # Process the Main Rule text
    main_text = parts[0]
    all_clauses = split_text_block(main_text, rule_id, is_sm=False)
    
    # If Supplementary Material was found, process it and append
    if len(parts) > 1:
        sm_text = parts[1]
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
    text = raw_text.strip()

    if not text:
        return True

    # Condition 1: Pure heading with no obligation content
    if clause_heading.strip() and text == clause_heading.strip():
        # print("cond1")
        return True

    # Condition 2: Too short to be a standalone obligation
    if len(text.split()) < 20:
        # print("cond2")
        return True

    # Condition 3: List item — ends with semicolon or semicolon + conjunction
    # These are properties of the original clause, not of merged text
    if text.rstrip().endswith(";"):
        # print("cond3-1")
        return True
    if text.rstrip().endswith("; and") or text.rstrip().endswith("; or"):
        # print("cond3-2")
        return True

    # Condition 4: Sentence continuation — clause is mid-sentence
    if text.rstrip().endswith(" and") or text.rstrip().endswith(" or"):
        # print("cond4")
        return True

    # Condition 5: Starts mid-sentence — clearly a fragment
    if text[0].islower():
        # print("cond5")
        return True

    return False


def merge_clause_to_completion(start_ref: str, all_clauses: dict, merge_until_root = False) -> dict:
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

    Parameters
    ──────────
    start_ref   : clause_ref of the clause to start from
    all_clauses : dict keyed by clause_ref, each value containing
                  at minimum: raw_text, clause_heading, parent_clause

    Returns
    ───────
    A copy of the start clause dict with:
      - raw_text    : merged text from stopping ancestor down to start
      - merged_up_to: clause_ref of the ancestor where walking stopped
    """

    chain = []        # accumulates nodes from start up to stopping ancestor
    current_ref = start_ref

    while current_ref and current_ref in all_clauses:
        node = all_clauses[current_ref]
        chain.append(node)

        # ── TRAP PREVENTION ──────────────────────────────────────────────
        # Always evaluate the ORIGINAL text from all_clauses.
        # Never evaluate the accumulated merged text.
        # ─────────────────────────────────────────────────────────────────
        original_text = node["raw_text"]
        heading       = node.get("clause_heading", "")

        if (not merge_until_root) and (not should_merge_upward(original_text, heading)):
            # This node is complete on its own — stop walking upward
            break

        parent_ref = node.get("parent_clause")
        if not parent_ref or parent_ref not in all_clauses:
            # Reached the root — stop regardless
            break

        current_ref = parent_ref

    
    # chain is ordered [start → parent → grandparent → stopping_ancestor]
    # Reverse to concatenate root-first, so the most general context
    # appears at the top of the merged text (mirrors how a human reads the rule)
    chain.reverse()

    merged_text = "\n".join(node["raw_text"].strip() for node in chain)

    result = all_clauses[start_ref].copy()
    result["raw_text"]     = merged_text
    result["clause_ref"]   = start_ref                # preserves original identity
    result["merged_up_to"] = chain[0]["clause_ref"]   # root of the merge chain

    return result


def build_merged_clause_set(all_clauses: dict) -> dict:
    """
    Applies merge_clause_to_completion to every clause in all_clauses
    and returns a new dict of merged clauses keyed by clause_ref.
    The raw_text of each clause in the output is the merged text from its stopping ancestor down to itself.
    The stopping ancestor is the nearest ancestor (including itself) whose original text is self-complete enough to stop the merge walk. 
    This ensures that each clause's raw_text is as self-contained and context-rich as possible without risking the trap of over-merging.
    The condition to check the original text of ancestors for merge-worthiness is encapsulated in should_merge_upward, 
    which is carefully designed to avoid false positives triggered by inherited punctuation in the merged text. 
    Note that in should_merge_upward, all evaluations are based on the original raw_text from all_clauses, never on the accumulated merged text, which is what prevents the trap.

    Clauses that are already complete are returned unchanged.
    """
    merged = {}
    for ref in all_clauses:
        merged[ref] = merge_clause_to_completion(ref, all_clauses)
    return merged

def build_context_bundle(clause_ref: str, all_clauses: dict) -> dict:
    """
    Builds a full ancestor chain from the target clause up to the root.
    all_clauses is a dict keyed by clause_ref for fast lookup.

    The bundle is passed to the LLM for normalization context only.
    It is never stored in ChromaDB.

    Each ancestor entry contains:
        clause_ref   : the clause identifier
        raw_text     : the original clause text
        parent_clause: the ancestor's own parent ref (or None if root)
        level        : depth in the hierarchy, root = 1
    """
    ancestors = []
    clause = all_clauses.get(clause_ref)
    current_ref = clause.get("parent_clause")

    while current_ref and current_ref in all_clauses:
        ancestor = all_clauses[current_ref]
        ancestors.append({
            "clause_ref":    ancestor["clause_ref"],
            "raw_text":      ancestor["raw_text"],
            "parent_clause": ancestor.get("parent_clause"),
        })
        current_ref = ancestor.get("parent_clause")

    # Currently ordered nearest → root. Reverse to get root → nearest.
    ancestors.reverse()

    # Assign levels now that ancestors are in root-first order.
    # Root is level 1. Each step deeper increments by 1.
    for i, ancestor in enumerate(ancestors):
        ancestor["level"] = i + 1

    # The target clause sits one level below the immediate parent.
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
    """
    Converts a context bundle into a single formatted string for the
    RAW CLAUSE TEXT section of the normalisation prompt.

    Ancestors are presented root-first so the LLM reads from general
    to specific, mirroring how a compliance officer reads the rulebook.
    """
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
    target:    dict,
    rule_id:   str,
    rule_name: str,
    all_clauses: dict
) -> str:
    """
    Combines a context bundle with the clause normalisation prompt
    template to produce the final string ready to send to the LLM.

    Parameters
    ──────────
    target   : dict containing the target clause's raw_text, clause_ref, and parent_clause
    rule_id   : FINRA rule number, e.g. "3110"
    rule_name : human-readable rule name, e.g. "Supervision"

    Returns
    ───────
    A fully populated prompt string ready for LLM inference.
    """
    target_clause = target.get("raw_text", "") # This is not the merged text. It is the raw text of the target clause itself.
    
    clause_ref = target["clause_ref"]
    parent_ref = target.get("parent_clause") or "null"

    # Merging the current clause with all its ancestors to build a full clause
    context_text = merge_clause_to_completion(clause_ref, all_clauses, merge_until_root=True)
    context_text = context_text.get("raw_text", "")
    
    # Format the bundle into the RAW CLAUSE TEXT block
    # raw_clause_text = format_bundle_for_prompt(bundle)

    return CLAUSE_NORMALISATION_PROMPT.format(
        rule_id         = rule_id,
        rule_name       = rule_name,
        parent_ref      = parent_ref,
        clause_ref      = clause_ref,
        target_clause   = target_clause,
        context_text  = context_text,
        # raw_clause_text = raw_clause_text,
    )

def _parse_tamu_content(raw) -> str:
    """
    Handles both cases:
      - raw is already an openai ChatCompletion object  → direct access
      - raw is a plain string (SSE stream)              → line-by-line parse
    """
    import json as _json

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
            chunk = _json.loads(json_str)
            content = chunk["choices"][0]["delta"].get("content", "")
            if content:
                return content
        except (KeyError, IndexError, _json.JSONDecodeError):
            continue

    return ""

# ── Model Configuration ───────────────────────────────────────────────────────

# MODEL_CONFIGS = {
#     "qwen": (
#         "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
#         "/models/qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf"
#     ),
#     "llama": (
#         "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
#         "/models/Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"
#     ),
# }


def load_normalizer_model(model_name: str = "qwen"):
    """
    Returns a backend instance — either a local Llama or a _TAMUBackend —
    depending on INFERENCE_BACKEND in settings.py.

    Both expose the same create_chat_completion() interface so
    normalize_clause and run_normalization_pipeline are backend-agnostic.

    Parameters
    ----------
    model_name : only used when INFERENCE_BACKEND == "local"
                 ("qwen" or "llama", matched against MODEL_CONFIGS)
    """
    if model_name == "tamu" or INFERENCE_BACKEND == "tamu":
        print(f"  Using TAMU inference backend: {TAMU_CONFIG['model']}")
        return _TAMUBackend()

    # ── Local GGUF inference (original behaviour) ─────────────────────────
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
    model,           # Llama | _TAMUBackend — both have create_chat_completion()
    prompt:      str,
    max_retries: int = 3,
) -> dict | None:
    """
    Calls whichever backend is loaded (local Llama or TAMU API) and
    returns the structured JSON dict the model produces.

    Strips <think>...</think> blocks, markdown fences, then parses JSON.
    Retries up to max_retries times before returning None.
    """
    import re as _re

    messages = [{"role": "user", "content": prompt}]

    for attempt in range(1, max_retries + 1):
        try:
            response = model.create_chat_completion(
                messages    = messages,
                temperature = 0.0,
                max_tokens  = 4096,
            )
            raw = response["choices"][0]["message"]["content"].strip()

            raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
            raw = _re.sub(r"^```(?:json)?\s*", "", raw)
            raw = _re.sub(r"\s*```$",           "", raw)

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
    """
    Converts a value to a ChromaDB-safe string.

    ChromaDB metadata fields accept only str, int, float, or bool.
    This helper converts:
        None  → ""          (ChromaDB rejects None)
        list  → ", ".join   (ChromaDB rejects lists)
        other → str(value)
    """
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

    The TEXT USED FOR EMBEDDING is the merged_clause raw_text, not the
    raw clause text. The merged text is self-contained — it includes the
    governing obligation context — which produces more accurate semantic
    similarity scores at retrieval time.

    All metadata values are converted to ChromaDB-safe types via
    _safe_str and explicit bool() casts. No None values or lists are
    passed to ChromaDB.

    Parameters
    ----------
    raw_clause    : original clause dict from parse_finra_rule
    merged_clause : merged clause dict from build_merged_clause_set
    rule_meta     : entry from TARGET_RULES (rule_id, name, category, url)
    normalized    : LLM output dict from normalize_clause

    Returns
    -------
    Flat dict with keys:
        id        — ChromaDB document ID (= clause_ref)
        document  — text for embedding (merged clause text)
        all other keys — ChromaDB metadata fields
    """
    merged_text = merged_clause.get("raw_text") or raw_clause.get("raw_text", "")

    return {
        # ── ChromaDB identity and embedding text ──────────────────────────
        "id":       _safe_str(uuid.uuid4()), # raw_clause["clause_ref"],
        "document": merged_text,

        # ── Provenance ────────────────────────────────────────────────────
        "clause_ref":     raw_clause["clause_ref"],
        "parent_clause":  _safe_str(raw_clause.get("parent_clause")),
        "clause_heading": _safe_str(raw_clause.get("clause_heading")),
        "merged_up_to":   _safe_str(
            merged_clause.get("merged_up_to", raw_clause["clause_ref"])
        ),
        "rule_id":        rule_meta["rule_id"],
        "rule_name":      rule_meta["name"],
        "regulator":      "FINRA",

        # ── Normalised fields (LLM output) ────────────────────────────────
        # String fields — use _safe_str to guard against None
        "category":             _safe_str(normalized.get("category")),
        "obligated_actor":      _safe_str(normalized.get("obligated_actor")),
        "regulated_subject":    _safe_str(normalized.get("regulated_subject")),
        "activity_type":        _safe_str(normalized.get("activity_type")),
        "frequency":            _safe_str(normalized.get("frequency")),
        "reporting_recipient":  _safe_str(normalized.get("reporting_recipient")),

        # List fields — stored as comma-joined strings for ChromaDB
        # Use $contains logic in Python post-retrieval when filtering
        "applies_to_firm_type": _safe_str(normalized.get("applies_to_firm_type")),
        "subject_matter":       _safe_str(normalized.get("subject_matter")),
        "keywords":             _safe_str(normalized.get("keywords")),

        # Boolean fields — explicit bool() cast guards against LLM
        # returning 0/1 or "true"/"false" strings
        "involves_customer":      bool(normalized.get("involves_customer",      False)),
        "involves_third_party":   bool(normalized.get("involves_third_party",   False)),
        "has_financial_threshold": bool(normalized.get("has_financial_threshold", False)),
        "documentation_required": bool(normalized.get("documentation_required", False)),
    }


# ── Steps 1 & 2: Scraping Pipeline ───────────────────────────────────────────

# PARSED_CHECKPOINT    = Path("data/parsed_rules.json")
# BASE_DIR = Path(__file__).resolve().parent.parent
# PARSED_CHECKPOINT = BASE_DIR / "data" / "parsed_rules.json"

# NORMALIZED_CHECKPOINT = Path("data/normalized_documents.jsonl")
# NORMALIZED_CHECKPOINT = BASE_DIR / "data" / "normalized_documents.jsonl"


def run_scraping_pipeline() -> dict:
    """
    Iterates over TARGET_RULES, reads each locally saved HTML file,
    parses the raw text into clause dicts, and builds merged clause sets.

    Saves a checkpoint to PARSED_CHECKPOINT after all rules are processed.
    This checkpoint is the input to run_normalization_pipeline.

    Returns
    -------
    Dict keyed by rule_id:
        {
            "meta":    {rule_id, name, category, url},
            "clauses": {clause_ref: raw_clause_dict, ...},
            "merged":  {clause_ref: merged_clause_dict, ...},
        }
    """
    PARSED_CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    all_rules: dict = {}

    for rule in TARGET_RULES:
        rule_id = rule["rule_id"]
        print(f"\n  Rule {rule_id}: {rule['name']}")

        raw_text = scrape_rule_page(rule_id)   # now takes rule_id not url
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


# ── Step 3: Normalisation Pipeline ───────────────────────────────────────────

def run_normalization_pipeline(
    model,          # Llama | _TAMUBackend
    all_rules: dict,
) -> list[dict]:
    """
    Normalises every clause in all_rules using whichever backend `model`
    wraps. Everything else — checkpointing, resume support, progress
    logging — is identical to the original.
    """
    NORMALIZED_CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    processed_refs: set[str]  = set()
    existing_docs:  list[dict] = []

    if NORMALIZED_CHECKPOINT.exists():
        with open(NORMALIZED_CHECKPOINT, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    doc = json.loads(line)
                    processed_refs.add(doc["clause_ref"])
                    existing_docs.append(doc)
        if processed_refs:
            print(f"  Resuming: {len(processed_refs)} clauses already normalised")

    all_rules = {key:val for key, val in all_rules.items() if key in {"2010", "2020"}}  # filter out rules with no clauses
    total     = sum(len(r["clauses"]) for r in all_rules.values())
    remaining = total - len(processed_refs)
    print(f"  Total clauses: {total}  |  Already done: {len(processed_refs)}  "
          f"|  Remaining: {remaining}")

    if remaining == 0:
        print("  ✓ All clauses already normalised.")
        return existing_docs

    new_docs:   list[dict] = []
    skipped:    list[str]  = []
    done_count: int        = len(processed_refs)

    with open(NORMALIZED_CHECKPOINT, "a") as out_f:
        for rule_id, rule_data in all_rules.items():
            rule_meta    = rule_data["meta"]
            clauses_dict = rule_data["clauses"]
            merged_dict  = rule_data["merged"]

            for clause_ref, raw_clause in clauses_dict.items():
                if clause_ref in processed_refs:
                    continue

                done_count += 1
                print(f"  [{done_count}/{total}] {clause_ref} ...", end=" ", flush=True)

                prompt = build_normalisation_prompt(
                    target      = raw_clause,
                    rule_id     = rule_id,
                    rule_name   = rule_meta["name"],
                    all_clauses = clauses_dict,
                )

                normalized = normalize_clause(model, prompt)

                if normalized is None:
                    print("✗ skipped (normalisation failed after retries)")
                    skipped.append(clause_ref)
                    continue

                merged_clause = merged_dict.get(clause_ref, raw_clause)
                doc = assemble_document(
                    raw_clause    = raw_clause,
                    merged_clause = merged_clause,
                    rule_meta     = rule_meta,
                    normalized    = normalized,
                )

                out_f.write(json.dumps(doc, indent = 4) + "\n")
                out_f.flush()
                new_docs.append(doc)
                print("✓")

    print(f"\n  ✓ Normalisation run complete.")
    print(f"    Newly normalised : {len(new_docs)}")
    print(f"    Skipped (failed) : {len(skipped)}")
    if skipped:
        print("    Failed refs:")
        for ref in skipped:
            print(f"      {ref}")
    print(f"  ✓ Checkpoint updated → {NORMALIZED_CHECKPOINT}")

    return existing_docs + new_docs

if __name__ == "__main__":
    run_scraping_pipeline()