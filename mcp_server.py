"""
mcp_server.py
=============
MCP server exposing the FINRA regulatory knowledge base as a structured
compliance retrieval service.

This server is the MCP interface layer over a knowledge base built from
live FINRA rule pages (rules 3110, 3120, 3130, 4511, 3230). The rules
are scraped, parsed at clause level, normalized into structured metadata
using an LLM, and stored in a ChromaDB vector database. This server
exposes that knowledge base to any MCP-compatible client — Claude Desktop,
Cursor, or any custom agent — without those clients needing to know
anything about the underlying data model or retrieval logic.

The server's responsibility is knowledge retrieval only. Compliance
reasoning over the retrieved clauses is left to the consuming agent's
own LLM, which can use the compliance_reasoning_prompt exposed here to
apply the same reasoning structure used in the reference implementation.

Offerings
---------
Tools:
    retrieve_clauses  — searches the knowledge base using semantic
                        similarity and optional metadata filtering and
                        returns the most relevant FINRA rule clauses for
                        a described situation. This is the core capability
                        of the server — no LLM has this knowledge natively.

    extract_intent    — converts a plain-language situation description
                        into a structured intent object whose fields map
                        directly to the metadata schema used by
                        retrieve_clauses. Allows a consuming agent to
                        improve retrieval precision without needing to
                        know the internal schema.

Resources:
    finra://rules/index        — lists all rules in the knowledge base
                                 with their rule IDs, names, categories,
                                 and clause counts. Read this before
                                 querying to understand what is available.

    finra://rules/{rule_id}    — returns all clauses stored for a specific
                                 rule, identified by rule ID (e.g. "3110").
                                 Useful for inspecting what the knowledge
                                 base knows about a particular rule before
                                 issuing a retrieval query.
    finra://clauses/{clause_ref}  — returns the complete stored record for a
                                single clause identified by its clause
                                reference (e.g. "FINRA-3110(c)(3)(C)").
                                Includes the full merged clause text, all
                                normalized metadata fields, and hierarchy
                                information. Use this to inspect a specific
                                clause in full after identifying it through
                                retrieve_clauses or finra://rules/{rule_id}.

Prompts:
    clarification_prompt       — the multi-turn clarification conversation
                                 prompt that gathers the five key fields
                                 needed for precise retrieval: activity,
                                 actor, involves_customer,
                                 involves_third_party, and
                                 has_financial_threshold. A consuming agent
                                 uses this to conduct a focused clarification
                                 conversation before calling extract_intent.

    compliance_reasoning_prompt — the reasoning prompt template used to
                                  produce a structured compliance analysis
                                  from retrieved clauses. Returns four
                                  sections: DETERMINATION, APPLICABLE
                                  CLAUSES, REASONING, and CAVEATS. A
                                  consuming agent passes retrieved clause
                                  text into this template and sends it to
                                  its own LLM.

Recommended usage flow for a consuming agent
--------------------------------------------
    1. Read finra://rules/index to understand available rules.
    2. Use clarification_prompt to conduct a focused conversation with
       the user and produce a complete situation summary.
    3. Call extract_intent with the situation summary to get structured
       intent fields.
    4. Call retrieve_clauses with the situation summary and intent fields
       to get the relevant clauses.
    5. Use compliance_reasoning_prompt with the situation and retrieved
       clauses to reason over them with the agent's own LLM.

Requirements
------------
    pip install mcp chromadb sentence-transformers anthropic
    ANTHROPIC_API_KEY environment variable (required by extract_intent)
    Knowledge base must be built first: python build_knowledge_base.py

Claude Desktop configuration
-----------------------------
    Add to ~/Library/Application Support/Claude/claude_desktop_config.json
    (macOS) or %APPDATA%\\Claude\\claude_desktop_config.json (Windows):

    {
      "mcpServers": {
        "finra-compliance": {
          "command": "python",
          "args": ["/full/path/to/your/project/mcp_server.py"]
        }
      }
    }

    Restart Claude Desktop after saving. A hammer icon in the chat input
    confirms the server is connected and tools are available.
"""

import json

from mcp.server.fastmcp import FastMCP

from compliance_reasoning import COMPLIANCE_REASONING_PROMPT
from retrieval import load_collection, retrieve_clauses as _retrieve_clauses
from intent_pipeline import (
    extract_structured_intent_api,
    CLARIFICATION_SYSTEM_PROMPT,
)

# ── Startup ───────────────────────────────────────────────────────────────────

mcp = FastMCP("FINRAComplianceServer")

print("Loading ChromaDB collection...", flush=True)
_collection = load_collection()
print(f"  ✓ {_collection.count()} clauses loaded", flush=True)


# ── Tool ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def retrieve_clauses(
    situation:            str,
    top_k:                int  = 8,
    activity_type:        str  = None,
    category:             str  = None,
    obligated_actor:      str  = None,
    regulated_subject:    str  = None,
    involves_customer:    bool = False,
    involves_third_party: bool = False,
) -> str:
    """
    Search the FINRA regulatory knowledge base and return the most
    relevant rule clauses for a described compliance situation.

    The knowledge base contains FINRA rules 3110, 3120, 3130, 4511,
    and 3230, stored as normalized clause-level documents with
    structured metadata. Retrieval uses both semantic similarity and
    metadata filtering — pass any structured fields you are confident
    about to improve precision. Fields left as None fall back to pure
    semantic search.

    Args:
        situation:            Plain-language description of the compliance
                              situation. Include who is involved, what
                              activity is taking place, and any relevant
                              context. This is the primary search signal.
        top_k:                Number of clauses to return (default 8).
        activity_type:        The regulated activity if known. Examples:
                              "supervision", "inspection", "books_and_records",
                              "outside_business_activity", "certification",
                              "complaint_handling", "transaction_review".
        category:             Regulatory domain if known. Examples:
                              "supervision", "books_and_records",
                              "associated_person_conduct".
        obligated_actor:      The party bearing the obligation if known.
                              Examples: "member", "registered_representative",
                              "registered_principal", "supervisory_personnel".
        regulated_subject:    What is being governed if known. Examples:
                              "written_procedures", "customer_account",
                              "associated_person", "branch_office".
        involves_customer:    Set True if a customer or their account
                              is involved in the situation.
        involves_third_party: Set True if a party outside the member
                              firm is involved (e.g. another broker-dealer,
                              outside employer, bank).
    """
    intent = {
        "situation_summary":       situation,
        "activity_type":           activity_type,
        "category":                category,
        "obligated_actor":         obligated_actor,
        "regulated_subject":       regulated_subject,
        "involves_customer":       involves_customer,
        "involves_third_party":    involves_third_party,
        "has_financial_threshold": False,
        "subject_matter":          [],
        "applies_to_firm_type":    ["broker_dealer"],
    }

    clauses = _retrieve_clauses(intent, _collection, top_k=top_k)

    if not clauses:
        return (
            "No relevant clauses found. The situation may fall outside "
            "the scope of the rules currently in the knowledge base, or "
            "the structured filters may be too narrow. Try passing fewer "
            "structured fields to widen the search."
        )

    lines = []
    for c in clauses:
        lines.append(
            f"[{c['clause_ref']}]  {c.get('rule_name', '')}  "
            f"— {c.get('activity_type', '')}\n"
            f"{c['document']}"
        )
    return "\n\n---\n\n".join(lines)


# ── Resources ─────────────────────────────────────────────────────────────────

@mcp.resource("finra://rules/index")
def rule_index() -> str:
    """
    Index of all FINRA rules currently in the knowledge base.

    Returns a JSON list of objects, each with:
        rule_id     : FINRA rule number (e.g. "3110")
        rule_name   : human-readable name (e.g. "Supervision")
        category    : regulatory domain (e.g. "supervision")
        clause_count: number of clauses stored for this rule

    Read this before calling retrieve_clauses to understand what
    rules are available and which categories they cover.
    """
    result = _collection.get(include=["metadatas"])

    index: dict[str, dict] = {}
    for meta in result["metadatas"]:
        rid = meta.get("rule_id", "")
        if not rid:
            continue
        if rid not in index:
            index[rid] = {
                "rule_id":      rid,
                "rule_name":    meta.get("rule_name", ""),
                "category":     meta.get("category", ""),
                "clause_count": 0,
            }
        index[rid]["clause_count"] += 1

    rules = sorted(index.values(), key=lambda x: x["rule_id"])
    return json.dumps(rules, indent=2)


@mcp.resource("finra://rules/{rule_id}")
def rule_clauses(rule_id: str) -> str:
    """
    All clauses stored for a specific FINRA rule.

    Returns a JSON list of clause objects, each with:
        clause_ref    : unique clause identifier (e.g. "FINRA-3110(a)(1)")
        activity_type : the regulated activity this clause governs
        text          : the clause text (truncated at 400 characters)

    Use rule_id values from finra://rules/index.
    Examples: "3110", "3120", "3130", "4511", "3230"
    """
    result = _collection.get(
        where   = {"rule_id": {"$eq": rule_id}},
        include = ["metadatas", "documents"],
    )

    if not result["ids"]:
        return json.dumps({
            "error":   f"No clauses found for rule {rule_id}.",
            "hint":    "Check finra://rules/index for available rule IDs.",
        })

    clauses = []
    for doc_id, doc, meta in zip(
        result["ids"], result["documents"], result["metadatas"]
    ):
        clauses.append({
            "clause_ref":    doc_id,
            "activity_type": meta.get("activity_type", ""),
            "text":          doc[:400] + "..." if len(doc) > 400 else doc,
        })

    clauses.sort(key=lambda x: x["clause_ref"])
    return json.dumps(clauses, indent=2)

@mcp.resource("finra://clauses/{clause_ref}")
def clause_detail(clause_ref: str) -> str:
    """
    Full detail for a specific FINRA clause by its clause reference.

    Returns the complete stored document including the clause text,
    all normalized metadata fields, and provenance information.

    Fields returned:
        clause_ref         : unique clause identifier (e.g. "FINRA-3110(c)(3)(C)")
        parent_clause      : reference of the immediate parent clause
        clause_heading     : heading text if present
        merged_up_to       : ancestor clause where the merge chain stopped
        rule_id            : FINRA rule number
        rule_name          : human-readable rule name
        regulator          : always "FINRA"
        category           : regulatory domain
        obligated_actor    : party bearing the obligation
        regulated_subject  : what is being governed
        activity_type      : the regulated activity
        frequency          : how often the obligation applies
        reporting_recipient: who receives any required report
        applies_to_firm_type: firm types this clause applies to
        subject_matter     : topic tags for semantic search
        keywords           : key phrases from the clause text
        involves_customer  : whether a customer is involved
        involves_third_party: whether an outside party is involved
        has_financial_threshold: whether a financial metric applies
        documentation_required: whether written records are required
        document           : the full merged clause text used for retrieval

    Use clause_ref values from finra://rules/{rule_id} or from the
    clause_ref fields returned by retrieve_clauses.
    Example: "FINRA-3110(c)(3)(C)"

    Note: clause_ref values contain parentheses — URL-encode them if
    your client requires it, e.g. "FINRA-3110%28c%29%283%29%28C%29".
    """
    result = _collection.get(
        ids     = [clause_ref],
        include = ["metadatas", "documents"],
    )

    if not result["ids"]:
        return json.dumps({
            "error": f"No clause found with ref '{clause_ref}'.",
            "hint":  (
                "Use finra://rules/{rule_id} to browse available "
                "clause refs for a given rule."
            ),
        })

    meta = result["metadatas"][0]
    doc  = result["documents"][0]

    return json.dumps({
        "clause_ref":          result["ids"][0],
        "document":            doc,
        "parent_clause":       meta.get("parent_clause", ""),
        "clause_heading":      meta.get("clause_heading", ""),
        "merged_up_to":        meta.get("merged_up_to", ""),
        "rule_id":             meta.get("rule_id", ""),
        "rule_name":           meta.get("rule_name", ""),
        "regulator":           meta.get("regulator", "FINRA"),
        "category":            meta.get("category", ""),
        "obligated_actor":     meta.get("obligated_actor", ""),
        "regulated_subject":   meta.get("regulated_subject", ""),
        "activity_type":       meta.get("activity_type", ""),
        "frequency":           meta.get("frequency", ""),
        "reporting_recipient": meta.get("reporting_recipient", ""),
        "applies_to_firm_type":  meta.get("applies_to_firm_type", ""),
        "subject_matter":        meta.get("subject_matter", ""),
        "keywords":              meta.get("keywords", ""),
        "involves_customer":     meta.get("involves_customer", False),
        "involves_third_party":  meta.get("involves_third_party", False),
        "has_financial_threshold": meta.get("has_financial_threshold", False),
        "documentation_required":  meta.get("documentation_required", False),
    }, indent=2)

# ── Prompt ────────────────────────────────────────────────────────────────────

@mcp.prompt()
def compliance_reasoning_prompt(situation: str, clauses: str) -> str:
    """
    Prompt template for reasoning over retrieved FINRA clauses.

    Use this after calling retrieve_clauses to produce a structured
    compliance analysis. The template instructs the LLM to produce
    four sections: DETERMINATION, APPLICABLE CLAUSES, REASONING,
    and CAVEATS.

    Args:
        situation: The compliance situation description (use the same
                   situation string passed to retrieve_clauses).
        clauses:   The retrieved clause text returned by retrieve_clauses,
                   passed through directly.
    """
    return COMPLIANCE_REASONING_PROMPT.format(
        situation_summary = situation,
        formatted_clauses = clauses,
    )

@mcp.tool()
def extract_intent(situation_summary: str) -> str:
    """
    Convert a plain-language compliance situation description into a
    structured intent object that can be passed directly to
    retrieve_clauses.

    Use this when you have a complete situation description — either
    written by the user directly or produced after a clarification
    conversation — and want to extract the structured fields that
    improve retrieval precision.

    The returned JSON contains fields including activity_type,
    category, obligated_actor, regulated_subject, involves_customer,
    involves_third_party, and subject_matter. Pass these directly
    as arguments to retrieve_clauses.

    Args:
        situation_summary: A complete plain-language description of
                           the compliance situation. Should include
                           who is involved, what activity is taking
                           place, and any relevant context. The richer
                           the description, the more precise the
                           extracted fields will be.
    """
    intent = extract_structured_intent_api(situation_summary)
    if intent is None:
        return json.dumps({
            "error": (
                "Could not extract structured intent from the situation "
                "description. Try providing a more detailed description "
                "including the actor, activity, and any parties involved."
            )
        })
    return json.dumps(intent, indent=2)

@mcp.prompt()
def clarification_prompt() -> str:
    """
    The clarification conversation prompt used to gather the five key
    fields needed for precise clause retrieval: activity, actor,
    involves_customer, involves_third_party, and has_financial_threshold.

    Use this to conduct a focused clarification conversation with the
    user before calling extract_intent. The prompt instructs the LLM
    to ask one targeted question at a time and produce a formal
    situation summary when all fields are clear, signalled by
    [READY_TO_STRUCTURE].

    Typical flow:
        1. Use this prompt to run a clarification conversation
        2. Extract the situation summary after [READY_TO_STRUCTURE]
        3. Pass the summary to extract_intent
        4. Pass the intent fields to retrieve_clauses
        5. Pass retrieved clauses to compliance_reasoning_prompt
    """
    return CLARIFICATION_SYSTEM_PROMPT


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")