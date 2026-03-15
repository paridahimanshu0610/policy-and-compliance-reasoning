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

# ── Configuration ────────────────────────────────────────────────────────────

TARGET_RULES = [
    {
        "rule_id": "3110",
        "name": "Supervision",
        "category": "supervision",
        "url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3110",
    },
    # {
    #     "rule_id": "3120",
    #     "name": "Supervisory Control System",
    #     "category": "supervision",
    #     "url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3120",
    # },
    # {
    #     "rule_id": "3130",
    #     "name": "Annual Certification of Compliance and Supervisory Processes",
    #     "category": "supervision",
    #     "url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3130",
    # },
    # {
    #     "rule_id": "4511",
    #     "name": "General Requirements for Books and Records",
    #     "category": "recordkeeping",
    #     "url": "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4511",
    # },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

OUTPUT_FILE = Path("knowledge_base.json")

# Hidden marker to preserve strong/bold tag boundaries post-HTML extraction
HEADING_MARKER = "\ue000" 

# ── Step 1: Scraping ──────────────────────────────────────────────────────────

def scrape_rule_page(url: str) -> str:
    print(f"  Fetching: {url}")
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"  ✗ Request failed: {e}")
        return ""

    soup = BeautifulSoup(response.text, "html.parser")

    # ── Primary path: "The Rule" tab pane → field--name-body ─────────────────
    rule_div = None

    the_rule_pane = soup.find("div", {"id": "the-rule"})
    if the_rule_pane:
        rule_div = the_rule_pane.find(
            "div",
            class_=lambda c: c and "field--name-body" in c
        )

    if not rule_div:
        rule_div = soup.find("div", class_=lambda c: c and "field--name-body" in c)

    if not rule_div:
        rule_div = soup.find("div", {"id": "block-body"})

    if not rule_div:
        rule_div = soup.find("main")

    if not rule_div:
        print("  ✗ Could not locate rule body — returning full page text")
        return soup.get_text(separator="\n", strip=True)

    # Remove amendment/footnote tables
    for table in rule_div.find_all("table", class_="footnote"):
        table.decompose()

    # FIX: Flatten strong/b tags into a single text node so get_text() 
    # doesn't inject newlines between the markers and the text.
    for tag in rule_div.find_all(['strong', 'b']):
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

_ROMAN: list[str] = [
    "i","ii","iii","iv","v","vi","vii","viii","ix","x",
    "xi","xii","xiii","xiv","xv","xvi","xvii","xviii","xix","xx",
    "xxi","xxii","xxiii","xxiv","xxv","xxvi","xxvii","xxviii","xxix","xxx",
]
_ROMAN_ORD: dict[str, int] = {r: i for i, r in enumerate(_ROMAN)}

def _interp(token: str) -> list[tuple[Seq, int]]:
    """Return every plausible (Seq, 0-based-ordinal) pair for a marker token."""
    # Catch FINRA Supplementary Material markers (e.g., ".01")
    if token.startswith("."):
        # Strip the dot and convert to int (e.g., ".01" -> 1 -> ordinal 0)
        return [(Seq.DECIMAL, int(token[1:]) - 1)]
        
    if re.fullmatch(r"\d+", token):
        return [(Seq.NUMERIC, int(token) - 1)]
    if re.fullmatch(r"[A-Z]", token):
        return [(Seq.ALPHA_UP, ord(token) - ord("A"))]

    lo = token.lower()
    out: list[tuple[Seq, int]] = []
    if len(token) == 1:
        out.append((Seq.ALPHA_LO, ord(lo) - ord("a")))
    if lo in _ROMAN_ORD:
        out.append((Seq.ROMAN, _ROMAN_ORD[lo]))
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


# ── 2. Clause Splitting Logic ─────────────────────────────────────────────────

CLAUSE_PATTERN = re.compile(
    rf"(?m)(?=^\s*{HEADING_MARKER}?(?:\([a-zA-Z0-9]+\)(?:\([a-zA-Z0-9]+\))*|\.[0-9]+(?:[ \t]|$)))"
)


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

    segments = CLAUSE_PATTERN.split(text)
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
        ref_match = re.match(rf"^{HEADING_MARKER}?(\([a-zA-Z0-9]+\)(?:\([a-zA-Z0-9]+\))*|\.[0-9]+)", seg)
        
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

# ── Prompt Template ───────────────────────────────────────────────────────────

CLAUSE_NORMALISATION_PROMPT = """TASK
====
You are a regulatory data analyst. Your job is to read a raw FINRA rule clause
and populate a structured JSON object by following the schema and instructions
below exactly.

CRITICAL RULES — READ BEFORE YOU BEGIN
=======================================
1. Return ONLY a valid JSON object. No explanations, no markdown fences,
   no commentary before or after the JSON.
2. Never invent information. If a field cannot be determined from the clause
   text, use the default value shown in the schema (null, false, or []).
3. Every string value you fill in MUST come from the allowed values listed
   in the comments for that field. Do not use values outside those lists.
4. Fill in every field. Do not omit any field from the output.
5. "activity_type" is the MOST IMPORTANT field. Read the clause carefully
   and choose the single best match from the allowed values.
6. If a list field has no applicable values, return an empty array [].
7. If a boolean field cannot be determined, return false as the default.
8. If a string or null field cannot be determined, return null.

INPUTS YOU WILL RECEIVE
=======================
- RULE ID         : The FINRA rule number (e.g. 3110)
- RULE NAME       : The name of the rule (e.g. Supervision)
- PARENT REF      : The parent clause reference, or null if top-level
- CLAUSE REF      : The specific clause identifier (e.g. FINRA-3110(c)(1)(A))
- TARGET CLAUSE   : The specific clause you must structure. Use exact or
                    near-exact language from this text for keywords.
- FULL RULE TEXT  : The complete rule text provided for context.
                    Read this alongside the TARGET CLAUSE to fully
                    understand the meaning and intent of what you
                    are structuring.

SCHEMA TO POPULATE
==================
{{
    "category": "",
    // Top-level regulatory domain. Choose exactly ONE value.
    //
    // ALLOWED VALUES AND WHEN TO USE THEM:
    //
    // "supervision"               → clause is about supervisory systems,
    //                               supervisory control, or annual
    //                               certification (rules 3110, 3120, 3130)
    //
    // "customer_communication"    → clause governs how members communicate
    //                               with or handle communications from
    //                               customers (rules 3150, 3160, 3170)
    //
    // "associated_person_conduct" → clause restricts or governs what a
    //                               registered or associated person may do
    //                               (rules 3210, 3220, 3240, 3241,
    //                                3270, 3280)
    //
    // "telemarketing"             → clause is about telemarketing rules
    //                               (rule 3230)
    //
    // "account_management"        → clause governs account designation or
    //                               discretionary trading authority
    //                               (rules 3250, 3260)
    //
    // "AML"                       → clause is about anti-money laundering
    //                               compliance (rule 3310)
    //
    // "financial_condition"       → clause governs capital requirements,
    //                               financial distress, audits, or asset
    //                               verification (rules 4110–4160)
    //
    // "margin"                    → clause governs margin calculations,
    //                               margin accounts, or margin records
    //                               (rules 4210–4240)
    //
    // "books_and_records"         → clause governs recordkeeping obligations
    //                               (rules 4570–4590)
    //
    // "ATS_reporting"             → clause governs market activity or
    //                               short interest reporting (rule 4560)
    //
    // HOW TO DECIDE: Use the rule ID to narrow down the domain.
    // Then confirm by checking the activity_type you identified.

    "obligated_actor": "",
    // The party who must comply with this clause.
    // Choose exactly ONE value from this list:
    //
    // "member"
    // "associated_person"
    // "registered_person"
    // "registered_representative"
    // "registered_principal"
    // "supervisory_personnel"
    // "CEO"
    // "CFO"
    // "financial_operations_principal"
    // "other"
    //
    // HOW TO DECIDE: Find the party who is explicitly required
    // to DO something under the governing obligation of this
    // clause. Look for the subject of an obligation sentence
    // (e.g. "each member shall", "the registered representative
    // must"). IMPORTANT — Do not confuse a role or entity that
    // appears in a descriptive or qualifying phrase with the
    // obligated actor. For example, "the person associated with
    // the member" in the phrase "over whose account the person
    // associated with the member has control" is describing a
    // relationship, not bearing an obligation. The obligated
    // actor must be the party who is explicitly required to DO
    // something, not a party mentioned in passing in a
    // descriptive context.
    // Always try to be as specific as possible. If the clause
    // names a specific role (e.g. member, registered
    // representative, supervisory personnel), use that.
    // Use "other" only if the obligated party is clearly not
    // one of the listed roles.

    "regulated_subject": "",
    // The entity or object that the obligation is about.
    // Choose exactly ONE value from this list:
    //
    // "associated_person"
    // "registered_person"
    // "customer"
    // "customer_account"
    // "member_firm"
    // "supervisory_personnel"
    // "OSJ"
    // "branch_office"
    // "non_branch_location"
    // "written_procedures"
    // "communication"
    // "transaction"
    // "capital_position"
    // "margin_account"
    // "security_position"
    // "business_clock"
    // "books_and_records"
    // "short_position"
    // "government_securities"
    // "swap_position"
    // "other"
    //
    // HOW TO DECIDE: Ask yourself — what is being supervised,
    // restricted, reviewed, or measured by the governing
    // obligation this clause belongs to? That is the
    // regulated_subject.
    // Always try to be as specific as possible. If the clause
    // names a specific entity or object (e.g. customer account,
    // supervisory personnel, OSJ), use that.
    // Use "other" only if the regulated subject is clearly not
    // one of the listed entities or objects.

    "activity_type": "",
    // The regulated activity this clause governs.
    // THIS IS THE MOST IMPORTANT FIELD.
    // Choose exactly ONE value from this list:
    //
    // 3000 series:
    // "supervision", "inspection",
    // "review", "certification",
    // "registration_verification", "correspondence_review",
    // "transaction_review", "complaint_handling",
    // "designation", "tape_recording",
    // "mail_holding", "outside_business_activity",
    // "private_securities_transaction", "borrowing_lending",
    // "telemarketing", "AML_monitoring",
    // "account_opening", "discretionary_trading",
    // "beneficiary_designation", "employee_compensation",
    // "networking_arrangement", "outside_account_disclosure",
    //
    // 4000 series:
    // "capital_compliance", "restricted_firm_reporting",
    // "regulatory_notification", "business_curtailment",
    // "audit", "guarantee_flow_through",
    // "asset_verification", "margin_calculation",
    // "margin_recordkeeping", "margin_extension_request",
    // "swap_margin", "short_interest_reporting",
    // "books_and_records", "clock_synchronization",
    //
    // HOW TO DECIDE: Ask — what is the member or person
    // actually required to DO under the governing obligation
    // this clause belongs to? Match that action to the closest
    // value in the list above. IMPORTANT — If the clause is
    // definitional, a sub-element, or a list item, do not
    // attempt to derive an activity from verbs used in a
    // descriptive or scoping context (e.g. "has control",
    // "shall include", "is held by" are structural phrases,
    // not regulated activities). Always match to the activity
    // of the governing obligation, not to incidental verbs
    // within the clause text.

    "applies_to_firm_type": [],
    // List all firm types this clause applies to.
    // Choose one or more values from this list:
    //
    // "broker_dealer"           → applies to all broker-dealers
    // "carrying_firm"           → holds customer assets
    // "introducing_firm"        → introduces accounts to carrying firms
    // "section_15C_member"      → government securities dealers
    // "restricted_firm"         → firms under 4111 obligations
    // "ATS_operator"            → operates alternative trading system
    // "tape_recording_firm"     → firms with tape recording history
    // "investment_banking_firm" → conducts investment banking services
    // "financial_institution"   → networking partner under 3160
    //
    // HOW TO DECIDE: Identify which firm type the governing
    // obligation applies to. If the clause applies to all
    // members generally, use ["broker_dealer"].
    // If uncertain, use ["broker_dealer"].

    "involves_customer": false,
    // Set to true if the clause directly concerns:
    // - customer accounts or assets
    // - interactions between firm employees and customers
    // - protection of customer interests
    // - any mention of "public customers", "retail customers",
    //   "clients", or any direct reference to customers of
    //   the member firm
    // Otherwise set to false.

    "involves_third_party": false,
    // Set to true if the clause involves an entity
    // outside the member firm, such as:
    // - another broker-dealer or financial institution
    // - a bank or counterparty
    // - an outside employer
    // - a registered national securities exchange
    // - a clearing firm or self-regulatory organization
    // - any external venue, platform, or institution
    //   not itself part of the member firm
    // SIMPLE CHECK: If the clause names or references ANY
    // specific organization, institution, venue, or entity
    // other than the member firm or its associated persons,
    // set this to true. The presence of any named external
    // entity in the clause is sufficient.
    // Otherwise set to false.

    "has_financial_threshold": false,
    // Set to true if the clause's applicability or
    // requirements depend on a financial metric such as:
    // - capital ratios or net capital levels
    // - gross revenue thresholds (e.g. $200M)
    // - margin percentages or account values
    // Otherwise set to false.

    "documentation_required": false,
    // Set to true if the clause explicitly requires:
    // - a written record, report, or filing
    // - documentation to be retained or submitted
    // Look for phrases like "evidenced in writing",
    // "written report", "kept on file", "must retain".
    // Otherwise set to false.

    "frequency": null,
    // How often the obligation must be performed.
    // Choose exactly ONE value or null:
    //
    // "ongoing"       → continuous obligation with no fixed cycle;
    //                   look for phrases like "at all times",
    //                   "continuously", "shall maintain", "must
    //                   always ensure", or any obligation that
    //                   implies a permanent, uninterrupted duty
    //                   with no specific time interval stated
    // "annual"        → once per calendar year
    // "triennial"     → once every three years
    // "quarterly"     → once per quarter
    // "monthly"       → once per month
    // "daily"         → every business day
    // "semi_annual"   → twice per year
    // "upon_trigger"  → only when a specific event occurs;
    //                   look for conditional language such as
    //                   "if", "when", "upon", "in the event that",
    //                   "where a member determines", or any
    //                   obligation that activates only after
    //                   a specific condition is met
    // "within_N_days" → within a specific number of days
    // "one_time"      → a setup or establishment obligation
    //                   required only once; look for phrases like
    //                   "shall establish", "must adopt", "shall
    //                   develop", or any obligation that is
    //                   fulfilled permanently once completed
    //                   and does not recur
    // "other"         → a frequency is clearly stated in the
    //                   clause but does not match any value above;
    //                   use this sparingly and only when the
    //                   clause explicitly states a time constraint
    //                   that cannot be mapped to any other value
    // null            → frequency is not stated in the clause and
    //                   cannot be safely inferred; when in doubt,
    //                   prefer null over an uncertain inference
    //
    // HOW TO DECIDE:
    // 1. Look for an explicit time phrase that answers the
    //    question "how often must this obligation be performed?"
    //    If found, map it directly to the matching value.
    // 2. IMPORTANT — A valid frequency signal must express how
    //    often the compliance obligation recurs. Words like
    //    "regularly", "routinely", or "continuously" appearing
    //    in a definitional or scoping context (e.g. describing
    //    what qualifies as a branch office) are NOT frequency
    //    signals for the compliance obligation.
    // 3. CRITICAL: Many clauses use obligation language — words
    //    like "shall", "must", "is required to", "is prohibited
    //    from", and similar terms — to express that a duty exists,
    //    not to express how often it must be performed. Do NOT
    //    treat obligation language as evidence of frequency.
    //    Always look for a separate, explicit signal that answers
    //    the question "how often?" before assigning any value.
    // 4. If a frequency is clearly stated but does not match
    //    any known value, use "other".
    // 5. When in doubt, use null. null is the safe default.

    "reporting_recipient": null,
    // If the clause requires submitting a report or filing,
    // identify who receives it. Choose ONE value or null:
    //
    // null                       → no reporting required
    // "FINRA"                    → report goes to FINRA
    // "SEC"                      → report goes to the SEC
    // "senior_management"        → report goes to firm leadership
    // "customer"                 → notification goes to customer
    // "self_regulatory_organization" → report goes to a self-regulatory organization
    // "other"                     → report goes to a recipient not listed above
    //
    // HOW TO DECIDE: Identify whether the governing obligation
    // requires submitting a report or filing, and if so, who
    // receives it. If no reporting obligation is stated, use
    // null. If a reporting obligation exists but no recipient
    // is named, use "other".

    "subject_matter": [],
    // List 3 to 6 topic tags that describe what this clause
    // is about. Use short phrases in lowercase with underscores.
    // Examples: "annual_inspection", "OSJ", "written_report",
    // "customer_account", "margin_calculation", "AML_program".
    // These are used for semantic search, so choose tags that
    // a compliance professional might type when searching.

    "keywords": []
    // List 4 to 8 important phrases taken directly from the
    // TARGET CLAUSE text. Use exact or near-exact language
    // from the clause. Examples: "inspect at least annually",
    // "calendar-year basis", "written report", "kept on file".
}}

STEP-BY-STEP INSTRUCTIONS
==========================
Follow these steps in order before writing any output.

STEP 1 — Read both the TARGET CLAUSE and the FULL RULE TEXT
          together to fully understand the governing obligation,
          its scope, and the role the target clause plays within
          it. Keep this understanding in mind throughout every
          subsequent step.

STEP 2 — Identify the obligated_actor by finding the party who
          is explicitly required to perform the governing
          obligation. Be careful not to confuse a role or entity
          mentioned in a descriptive or qualifying phrase with
          the obligated actor — only the party explicitly
          required to DO something qualifies.

STEP 3 — Identify the activity_type by asking: what is the actor
          actually required to DO under the governing obligation?
          If the target clause is definitional or a sub-element,
          do not derive the activity from structural or
          descriptive verbs within it — match to the activity of
          the governing obligation instead.

STEP 4 — Identify the regulated_subject by asking: what is being
          acted upon, supervised, or measured by the governing
          obligation this clause belongs to?

STEP 5 — Select the category based on the rule ID and the
          activity identified in STEP 3.

STEP 6 — Set the boolean fields (involves_customer,
          involves_third_party, has_financial_threshold,
          documentation_required) by applying the criteria
          described in each field's instructions.

STEP 7 — Fill in frequency and reporting_recipient by looking
          for explicit time phrases and reporting targets.
          For frequency: the signal must explicitly answer "how
          often must this obligation be performed?" — do not
          treat definitional language or obligation language as
          frequency signals. When in doubt, use null.
          For reporting_recipient: identify whether the governing
          obligation requires a report or filing and who receives
          it. If none, use null.

STEP 8 — Write subject_matter tags and keywords last, after all
          other fields are complete. Keywords must use exact or
          near-exact language from the TARGET CLAUSE text.

STEP 9 — Review your output. Confirm every string value appears
          in its allowed values list. Confirm no fields are missing.

STEP 10 — Output the final JSON object only. Nothing else.

NOW PROCESS THE FOLLOWING INPUT
================================
RULE ID       : {rule_id}
RULE NAME     : {rule_name}
PARENT REF    : {parent_ref}
CLAUSE REF    : {clause_ref}
TARGET CLAUSE : {target_clause}
FULL RULE TEXT: {context_text}"""


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
    target_clause = target.get("raw_text", "")
    
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


# ── Model Configuration ───────────────────────────────────────────────────────

MODEL_CONFIGS = {
    "qwen": (
        "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
        "/models/qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf"
    ),
    "llama": (
        "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
        "/models/Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"
    ),
}


def load_normalizer_model(model_name: str = "qwen") -> Llama:
    """
    Loads the specified quantised GGUF model for clause normalisation.

    Uses a larger context window (8192) than the intent pipeline because
    the normalisation prompt includes the full merged clause text, which
    can be several paragraphs long for deeply nested FINRA clauses.

    Parameters
    ----------
    model_name : "qwen" or "llama"

    Returns
    -------
    A loaded Llama instance ready for inference.
    """
    if model_name not in MODEL_CONFIGS:
        raise ValueError(
            f"Unknown model '{model_name}'. "
            f"Choose from: {list(MODEL_CONFIGS)}"
        )
    path = MODEL_CONFIGS[model_name]
    print(f"  Loading normaliser model: {model_name}")
    print(f"  Path: {path}")
    return Llama(
        model_path   = path,
        n_ctx        = 16384,
        n_gpu_layers = -1,      # -1 = offload all layers to GPU if available
        verbose      = False,
    )


# ── Step 3: LLM Normalisation ─────────────────────────────────────────────────

def normalize_clause(
    model:       Llama,
    prompt:      str,
    max_retries: int = 3,
) -> dict | None:
    """
    Calls the local LLM with a fully built normalisation prompt and
    returns the structured JSON dict the model produces.

    Strips <think>...</think> blocks that Qwen models emit before the
    JSON output, then strips accidental markdown fences, then parses.

    Retries up to max_retries times on JSON parse failures or LLM errors
    before giving up and returning None. A None return causes the caller
    to skip that clause and log a warning — the pipeline continues.

    Parameters
    ----------
    model       : loaded Llama instance
    prompt      : fully built normalisation prompt string
    max_retries : number of attempts before returning None

    Returns
    -------
    Parsed JSON dict, or None if all retries failed.
    """
    import re as _re

    messages = [{"role": "user", "content": prompt}]

    for attempt in range(1, max_retries + 1):
        try:
            response = model.create_chat_completion(
                messages    = messages,
                temperature = 0.0,   # deterministic — critical for structured output
                max_tokens  = 4096,
            )
            raw = response["choices"][0]["message"]["content"].strip()

            # Strip <think>...</think> blocks (Qwen reasoning traces)
            raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()

            # Strip accidental markdown fences
            raw = _re.sub(r"^```(?:json)?\s*", "", raw)
            raw = _re.sub(r"\s*```$",          "", raw)

            return json.loads(raw)

        except json.JSONDecodeError as e:
            print(f"    ✗ JSON parse error (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(1)

        except Exception as e:
            print(f"    ✗ LLM error (attempt {attempt}/{max_retries}): {e}")
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
BASE_DIR = Path(__file__).resolve().parent.parent
PARSED_CHECKPOINT = BASE_DIR / "data" / "parsed_rules.json"

# NORMALIZED_CHECKPOINT = Path("data/normalized_documents.jsonl")
NORMALIZED_CHECKPOINT = BASE_DIR / "data" / "normalized_documents.jsonl"


def run_scraping_pipeline() -> dict:
    """
    Iterates over TARGET_RULES, scrapes each rule page, parses the
    raw text into clause dicts, and builds merged clause sets.

    Saves a checkpoint to PARSED_CHECKPOINT after all rules are
    processed. This checkpoint is the input to run_normalization_pipeline.
    If scraping succeeds but normalisation later fails, re-run with
    --skip-scraping to avoid re-hitting the FINRA server.

    A 1-second delay is inserted between requests as a courtesy to the
    FINRA server.

    Returns
    -------
    Dict keyed by rule_id:
        {
            "meta":    {rule_id, name, category, url},
            "clauses": {clause_ref: raw_clause_dict, ...},    # unmerged
            "merged":  {clause_ref: merged_clause_dict, ...}, # merged
        }

    An empty dict is returned (and logged) if no rules could be scraped.
    """
    PARSED_CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    all_rules: dict = {}

    for rule in TARGET_RULES:
        rule_id = rule["rule_id"]
        print(f"\n  Rule {rule_id}: {rule['name']}")

        raw_text = scrape_rule_page(rule["url"])
        if not raw_text:
            print(f"    ✗ Skipping — no content retrieved")
            continue

        time.sleep(1)   # polite delay

        clauses_list = parse_finra_rule(raw_text, rule_id)
        # For debugging: limit to 10 clauses per rule to speed up early runs
        # clauses_list = clauses_list[:10]
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
            json.dump(all_rules, f, indent=2)
        print(f"\n  ✓ Scraping checkpoint saved → {PARSED_CHECKPOINT}")
    else:
        print("\n  ✗ No rules were successfully scraped.")

    return all_rules


# ── Step 3: Normalisation Pipeline ───────────────────────────────────────────

def run_normalization_pipeline(
    model:     Llama,
    all_rules: dict,
) -> list[dict]:
    """
    Normalises every clause in all_rules using the local LLM, assembles
    the final document dicts, and writes them incrementally to
    NORMALIZED_CHECKPOINT as a JSONL file.

    RESUME SUPPORT
    Clause_refs already present in the checkpoint file are skipped on
    re-run. This means if the process is killed partway through (which
    is likely given the number of clauses and local inference speed),
    simply re-running the script resumes exactly where it left off.

    PROGRESS LOGGING
    Each clause is logged with its position in the full work queue
    (e.g. [47/312]) so you can estimate remaining time.

    Parameters
    ----------
    model     : loaded Llama instance from load_normalizer_model
    all_rules : dict returned by run_scraping_pipeline

    Returns
    -------
    Full list of assembled document dicts (existing + newly normalised).
    This is passed directly to ingest_documents in build_knowledge_base.py.
    """
    NORMALIZED_CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)

    # ── Load already-processed clause_refs for resume support ────────────
    processed_refs: set[str] = set()
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

    # ── Count total work remaining ────────────────────────────────────────
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

    # Open in append mode so existing progress is preserved
    with open(NORMALIZED_CHECKPOINT, "a") as out_f:

        for rule_id, rule_data in all_rules.items():
            rule_meta    = rule_data["meta"]
            clauses_dict = rule_data["clauses"]
            merged_dict  = rule_data["merged"]

            for clause_ref, raw_clause in clauses_dict.items():

                if clause_ref in processed_refs:
                    continue   # already in checkpoint

                done_count += 1
                print(
                    f"  [{done_count}/{total}] {clause_ref} ...",
                    end=" ", flush=True
                )

                # Build the context bundle and full normalisation prompt
                # bundle = build_context_bundle(clause_ref, clauses_dict)

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

                # Write immediately — preserves progress on crash
                out_f.write(json.dumps(doc) + "\n")
                out_f.flush()

                new_docs.append(doc)
                print("✓")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n  ✓ Normalisation run complete.")
    print(f"    Newly normalised : {len(new_docs)}")
    print(f"    Skipped (failed) : {len(skipped)}")
    if skipped:
        print("    Failed refs:")
        for ref in skipped:
            print(f"      {ref}")
    print(f"  ✓ Checkpoint updated → {NORMALIZED_CHECKPOINT}")

    return existing_docs + new_docs