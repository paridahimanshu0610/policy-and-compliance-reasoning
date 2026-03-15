"""
intent_pipeline.py
==================
Conversational intent gathering pipeline.

Flow:
    User message
        │
        ▼
    Clarification agent (llama_cpp)
    asks up to 3 targeted questions
        │
        ├── [READY_TO_STRUCTURE] detected?
        │         NO  → show question to user → loop
        │         YES → extract summary
        │                   │
        │                   ▼
        │           Intent extraction LLM call
        │                   │
        │                   ▼
        │           Structured intent JSON
        │           (used to query ChromaDB)

Usage:
    pip install llama-cpp-python
    python intent_pipeline.py
"""

import json
import re
from llama_cpp import Llama


# ── Model Setup ───────────────────────────────────────────────────────────────

def load_model(model_path: str) -> Llama:
    """
    Loads the GGUF model via llama_cpp.

    n_ctx      : context window — 16384 is safe for this pipeline
    n_gpu_layers: set to -1 to offload all layers to GPU if available,
                  0 for CPU-only
    verbose    : set to False to suppress llama.cpp startup logs
    """
    return Llama(
        model_path   = model_path,
        n_ctx        = 16384,
        n_gpu_layers = -1,
        verbose      = False,
    )


# ── Prompts ───────────────────────────────────────────────────────────────────

CLARIFICATION_SYSTEM_PROMPT = """You are a compliance assistant for a FINRA \
regulatory query system. Your sole job is to gather any missing information \
before the query is handed off to a retrieval system.

FIRST — at the start of EVERY response, re-assess all five fields
from scratch using the entire conversation so far, not just the
most recent message. A field is only CLEAR if it has been addressed
anywhere in the conversation. A field remains MISSING if it has
never been mentioned by the user at any point.

Go through each field in order and mark it as CLEAR or MISSING:

  1. activity: what the situation or activity is about
     CLEAR if: the user describes what is happening
     Question if missing: "Could you describe what activity or
     situation you need guidance on?"

  2. actor: who is involved and in what role
     CLEAR if: the user names a specific role or party
     Question if missing: "Who is involved in this situation,
     and in what role?"

  3. involves_customer: whether a customer or their account
     is affected
     CLEAR if: the user explicitly mentions a customer, OR
     explicitly states no customer is involved
     MISSING if: the user has not mentioned customers at all
     Question if missing: "Does this situation involve a
     customer or their account?"

  4. involves_third_party: whether anyone outside the firm
     is involved
     CLEAR if: the user explicitly mentions an outside party
     such as a bank, exchange, or outside employer, OR
     explicitly states no outside party is involved
     MISSING if: the user has not mentioned any outside party
     Question if missing: "Does this situation involve any
     party outside your firm?"

  5. has_financial_threshold: whether a specific financial
     figure or threshold is relevant
     CLEAR if: the user mentions a specific amount or figure,
     OR explicitly states no financial figure is relevant
     MISSING if: the user has not mentioned any financial
     figure at all
     Question if missing: "Is there a specific financial
     amount or threshold relevant to this situation?"

Output your assessment in exactly this format before doing anything else:

1. activity: CLEAR or MISSING
2. actor: CLEAR or MISSING
3. involves_customer: CLEAR or MISSING
4. involves_third_party: CLEAR or MISSING
5. has_financial_threshold: CLEAR or MISSING

Then:
- If ANY field is MISSING and you have not yet reached the question
  limit, ask the question for the first missing field.
- If ALL fields are CLEAR, or you have reached the question limit,
  proceed to [READY_TO_STRUCTURE].

RULES — FOLLOW THESE EXACTLY:

OUTPUT FORMAT RULES:
- Each response must contain EITHER a clarifying question OR the
  [READY_TO_STRUCTURE] marker. Never both in the same response.
- If you are asking a question, output only the question. Nothing else.
- If you are ready to structure, output [READY_TO_STRUCTURE] on its own
  line, followed immediately by the summary paragraph. Nothing else.
- Do not output your internal assessment, reasoning, or field-by-field
  evaluation anywhere in your response. These are internal steps only
  and must never appear in the output.

QUESTION RULES:
- Ask only ONE question per response.
- Keep each question short — one sentence.
- Frame your question around the user's specific situation. Do not ask
  abstract checklist questions.
- Only ask about fields that are genuinely missing from what the user
  has said. Do not ask about anything already stated or clearly implied.
- Do not repeat a question you have already asked.
- Do not ask more than {max_questions} question(s) total. After that,
  proceed to [READY_TO_STRUCTURE] regardless of remaining gaps.
- If the user responds to a question with uncertainty — for example
  "I don't know", "I'm not sure", "I'm not certain", or any similar
  expression of not knowing — treat that field as CLEAR with an
  unknown value. Do not ask about it again. A user's inability to
  answer is itself a valid response.

SUMMARY RULES:
- The summary is a restatement of the user's situation and question in
  formal third-person language. It is not a compliance determination,
  answer, or interpretation.
- Write in the third person (e.g. "The member is seeking to determine
  whether...", "A registered representative has been offered...").
- If the user is asking a question, the summary must preserve that it
  is a question. Do not convert a question into a statement of fact or
  a compliance conclusion. For example, if the user asks "do we need
  to inspect every year?", the summary must reflect that this is being
  asked, not assert that an annual inspection is required.
- Do not state what the user must do, should do, or is required to do.
  Do not draw any compliance conclusions. Do not suggest next steps.
- Restate in your own formal words — do not copy the user's message
  verbatim. The summary must be a properly constructed formal paragraph,
  not a repetition of what the user typed.
- Include every detail the user mentioned: the actor, the activity,
  any parties involved, and any reporting obligations, deadlines,
  timeframes, financial figures, existing documentation, or written
  procedures they mentioned — even if you did not ask about them.
- Do not include rule numbers.
- Do not write anything after the summary paragraph.

Now respond to the user's message by following the rules above exactly.
If one or more fields are missing, ask your first clarifying question.
If all fields are clear, output [READY_TO_STRUCTURE] followed by the
summary paragraph."""


INTENT_EXTRACTION_PROMPT = """TASK
====
You are a regulatory data analyst. A compliance situation has been described
to you. Your job is to populate a structured JSON object that will be used
to retrieve relevant FINRA rule clauses from a database.

The database stores clause documents with the following filterable fields:
  - category, activity_type, obligated_actor, regulated_subject
  - applies_to_firm_type, involves_customer, involves_third_party
  - has_financial_threshold, documentation_required
  - frequency, reporting_recipient

Your structured output drives the database filter. A wrong value silently
excludes the correct clauses. A null or false value widens the search, which
is always safer than an incorrect specific value. When uncertain, use null
or false — never guess.

CRITICAL RULES
==============
1. Return ONLY a valid JSON object. No explanations, no markdown fences,
   no commentary before or after the JSON.
2. Every string value MUST come from the allowed values listed for that
   field. Do not use values outside those lists.
3. Fill in every field. Do not omit any field.
4. "activity_type" is the most important field — choose carefully.
5. For list fields with no applicable values, return [].
6. For boolean fields you cannot determine, return false.
7. For string or null fields you cannot determine, return null.

SCHEMA
======
{{
    "activity_type": "",
    // The primary regulated activity the situation involves.
    // THIS IS THE MOST IMPORTANT FIELD.
    // Choose exactly ONE value:
    //
    // 3000 series:
    // "supervision"                    "inspection"
    // "review"                         "certification"
    // "registration_verification"      "correspondence_review"
    // "transaction_review"             "complaint_handling"
    // "designation"                    "tape_recording"
    // "mail_holding"                   "outside_business_activity"
    // "private_securities_transaction" "borrowing_lending"
    // "telemarketing"                  "AML_monitoring"
    // "account_opening"                "discretionary_trading"
    // "beneficiary_designation"        "employee_compensation"
    // "networking_arrangement"         "outside_account_disclosure"
    //
    // 4000 series:
    // "capital_compliance"             "restricted_firm_reporting"
    // "regulatory_notification"        "business_curtailment"
    // "audit"                          "guarantee_flow_through"
    // "asset_verification"             "margin_calculation"
    // "margin_recordkeeping"           "margin_extension_request"
    // "swap_margin"                    "short_interest_reporting"
    // "books_and_records"              "clock_synchronization"
    //
    // HOW TO DECIDE: Identify the core regulated action the
    // situation involves. Ask — what is the person or firm
    // actually doing or trying to do?
    //
    // Common mappings:
    // borrowing or lending money involving a customer
    //     → "borrowing_lending"
    // rep working at another company on the side
    //     → "outside_business_activity"
    // rep opening or maintaining an account at another broker
    //     → "outside_account_disclosure"
    // rep buying or selling securities away from the firm
    //     → "private_securities_transaction"
    // firm inspecting a branch office or OSJ
    //     → "inspection"
    // CEO or principal certifying supervisory processes
    //     → "certification"
    // firm receiving a written customer complaint
    //     → "complaint_handling"
    // firm reviewing trades for insider trading or manipulation
    //     → "transaction_review"
    // firm establishing or testing supervisory procedures
    //     → "supervision"
    // firm notifying FINRA of a financial or operational problem
    //     → "regulatory_notification"
    // firm conducting an independent audit of its finances
    //     → "audit"
    //
    // If the situation spans multiple activities, choose the one
    // most central to what the user is directly asking about.

    "category": null,
    // High-level regulatory domain. Choose ONE or null.
    //
    // "supervision"               → situation is about supervisory
    //                               systems, control procedures, or
    //                               oversight obligations
    //                               (rules 3110, 3120, 3130)
    // "customer_communication"    → situation involves customer mail,
    //                               networking arrangements with banks,
    //                               or tape recording of registered
    //                               persons (rules 3150, 3160, 3170)
    // "associated_person_conduct" → situation is about what a
    //                               registered or associated person
    //                               may or may not do personally
    //                               (rules 3210, 3220, 3240, 3241,
    //                                3270, 3280)
    // "telemarketing"             → situation involves outbound
    //                               telephone solicitation
    //                               (rule 3230)
    // "account_management"        → situation involves account
    //                               designation or discretionary
    //                               trading authority (rules 3250, 3260)
    // "AML"                       → situation involves anti-money
    //                               laundering compliance obligations
    //                               (rule 3310)
    // "financial_condition"       → situation involves capital levels,
    //                               net capital, audits, or financial
    //                               distress (rules 4110–4160)
    // "margin"                    → situation involves margin
    //                               requirements or margin accounts
    //                               (rules 4210–4240)
    // "books_and_records"         → situation involves recordkeeping
    //                               or retention obligations
    //                               (rules 4570–4590)
    // "ATS_reporting"             → situation involves market or
    //                               short interest reporting (rule 4560)
    //
    // HOW TO DECIDE: Use the activity_type you identified to
    // infer the category. If the activity_type maps clearly to
    // a rule series, use the corresponding category.
    // If uncertain between two categories, use null —
    // a null value widens the search safely.

    "obligated_actor": null,
    // Who in the situation bears the compliance obligation.
    // Choose ONE or null if not determinable.
    //
    // "member"                         → the broker-dealer firm itself
    // "associated_person"              → any person associated with
    //                                    the member firm
    // "registered_person"              → any registered person
    // "registered_representative"      → a registered representative
    // "registered_principal"           → a registered principal
    // "supervisory_personnel"          → a supervisor or compliance
    //                                    officer
    // "CEO"                            → the chief executive officer
    // "CFO"                            → the chief financial officer
    // "financial_operations_principal" → the firm's FINOP
    // "other"                          → clearly an obligated party
    //                                    but not listed above
    //
    // HOW TO DECIDE: Who is the subject of the question —
    // who did something or needs to do something?
    // "our rep" or "our trader"  → "registered_representative"
    // "our firm" or "we"         → "member"
    // "our principal"            → "registered_principal"
    // "our compliance officer"   → "supervisory_personnel"
    // "our CEO"                  → "CEO"
    // If multiple actors are involved, choose the one most
    // directly bearing the obligation being asked about.
    // If no actor is identifiable, use null.

    "regulated_subject": null,
    // What is being governed, reviewed, or restricted in
    // this situation. Choose ONE or null if not determinable.
    //
    // "associated_person"    "registered_person"    "customer"
    // "customer_account"     "member_firm"          "supervisory_personnel"
    // "OSJ"                  "branch_office"        "non_branch_location"
    // "written_procedures"   "communication"        "transaction"
    // "capital_position"     "margin_account"       "security_position"
    // "business_clock"       "books_and_records"    "short_position"
    // "government_securities""swap_position"        "other"
    //
    // HOW TO DECIDE: Ask — what is the thing being acted upon,
    // supervised, or measured in this situation?
    // Situation is about borrowing from a client
    //     → "customer"
    // Situation is about a rep's outside job
    //     → "associated_person"
    // Situation is about inspecting an office
    //     → "OSJ" or "branch_office"
    // Situation is about reviewing trades
    //     → "transaction"
    // Situation is about written supervisory procedures
    //     → "written_procedures"
    // Situation is about a customer complaint or communication
    //     → "communication"
    // If not determinable, use null.

    "applies_to_firm_type": ["broker_dealer"],
    // Firm types this situation applies to.
    // Default to ["broker_dealer"] unless the situation
    // explicitly names a different firm type.
    //
    // "broker_dealer"           "carrying_firm"
    // "introducing_firm"        "section_15C_member"
    // "restricted_firm"         "ATS_operator"
    // "tape_recording_firm"     "investment_banking_firm"
    // "financial_institution"
    //
    // HOW TO DECIDE: If the situation applies to the firm
    // generally without naming a specific type, use
    // ["broker_dealer"]. Only add a more specific type if
    // the situation explicitly identifies it — for example,
    // if the user mentions investment banking services, add
    // "investment_banking_firm". When uncertain, default to
    // ["broker_dealer"].

    "involves_customer": false,
    // HOW TO DECIDE: Set to true if the situation involves
    // a customer, client, or their account in any way —
    // including borrowing from a customer, receiving a
    // customer complaint, or managing customer assets.
    // Set to false if the situation is purely internal
    // to the firm and its associated persons.

    "involves_third_party": false,
    // HOW TO DECIDE: Set to true if the situation involves
    // any entity outside the member firm. Ask — is there
    // a named or implied external party?
    // Outside employer or company     → true
    // Another broker-dealer or bank   → true
    // FINRA, SEC, or any regulator    → true
    // Purely internal firm situation  → false
    // Customer alone (no other entity)→ false

    "has_financial_threshold": false,
    // HOW TO DECIDE: Set to true only if the situation
    // explicitly mentions a financial metric that affects
    // what obligations apply — for example, a capital ratio,
    // a revenue figure, a margin percentage, or an account
    // value threshold. If no financial metric is mentioned,
    // set to false. If unclear, use null.

    "documentation_required": false,
    // HOW TO DECIDE: Set to true if the situation clearly
    // involves a question about written records, reports,
    // or filings. Look for phrases like "do we need to
    // document", "do we need a written record", "do we need
    // to keep records", or situations where a written report
    // is an explicit part of the obligation being asked about.
    // Otherwise set to false.

    "frequency": null,
    // HOW TO DECIDE: Set this only if the situation explicitly
    // involves a question about how often something must be done,
    // or if a recurring obligation is clearly central to the
    // question.
    //
    // "annual"        → question is about a yearly obligation
    // "upon_trigger"  → question is about what to do when a
    //                   specific event occurs
    // "within_N_days" → question involves a specific deadline
    // "one_time"      → question is about a setup obligation
    // "ongoing"       → question is about a continuous duty
    // null            → frequency is not central to the question
    //                   or cannot be determined — this is the
    //                   safe default in most cases
    //
    // IMPORTANT: Do not set frequency just because the situation
    // involves an obligation. Only set it if the question is
    // specifically about how often or by when something must occur.

    "reporting_recipient": null,
    // HOW TO DECIDE: Set this only if the user is explicitly
    // asking whether something must be reported, filed, or
    // disclosed to a specific party.
    //
    // "FINRA"                       → user asks about reporting
    //                                 to FINRA
    // "SEC"                         → user asks about reporting
    //                                 to the SEC
    // "senior_management"           → user asks about internal
    //                                 reporting obligations
    // "customer"                    → user asks about notifying
    //                                 a customer
    // "self_regulatory_organization"→ user asks about reporting
    //                                 to an SRO generally
    // "other"                       → user asks about reporting
    //                                 but recipient is unclear
    // null                          → user is not asking about
    //                                 reporting — this is the
    //                                 default in most cases
    //
    // IMPORTANT: Do not set this field just because the activity
    // type sometimes involves reporting. Only set it if the user
    // is explicitly asking whether a report or notification is
    // required.

    "subject_matter": [],
    // 3 to 6 topic tags in lowercase_with_underscores.
    //
    // HOW TO DECIDE: Choose tags that would match the keywords
    // stored in the relevant clause documents. Think about what
    // a compliance professional would type when searching for
    // the applicable rule — not just what the user said.
    // Examples:
    // "annual_inspection", "OSJ", "written_report",
    // "written_procedures", "outside_business_activity",
    // "customer_complaint", "borrowing_from_customer",
    // "registered_representative", "supervisory_personnel",
    // "AML_program", "annual_certification"
    // Make tags specific enough to be discriminating but
    // broad enough to match clause keywords.
}}

STEP-BY-STEP INSTRUCTIONS
==========================
STEP 1 — Read the situation description fully before filling any field.
STEP 2 — Identify activity_type first. Ask: what is the core regulated
          activity this situation involves?
STEP 3 — Identify obligated_actor. Who is required to act or comply?
STEP 4 — Identify regulated_subject. What is being governed or measured?
STEP 5 — Select category from the activity_type identified in STEP 2.
STEP 6 — Set boolean fields by checking whether the situation mentions
          customers, third parties, financial metrics, or documentation.
STEP 7 — Set frequency and reporting_recipient only if clearly stated.
          When uncertain, use null.
STEP 8 — Write subject_matter tags that would match clause keywords.
STEP 9 — Review. Confirm all values are from allowed lists. Output JSON only.

NOW PROCESS THE FOLLOWING SITUATION
=====================================
{situation_summary}"""


# ── LLM Helpers ───────────────────────────────────────────────────────────────

def _llm_call(
    model:    Llama,
    messages: list[dict],
    temp:     float = 0.0,
) -> str:
    """
    Single call to the llama_cpp model with a messages list.
    Returns the assistant reply string.
    """
    response = model.create_chat_completion(
        messages    = messages,
        temperature = temp,
        max_tokens  = 1024,
    )
    return response["choices"][0]["message"]["content"].strip()


def _strip_think_tags(text: str) -> str:
    """Removes <think>...</think> blocks emitted by some models."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ── Stage 1: Clarification Agent ─────────────────────────────────────────────

def run_clarification_agent(
    model:         Llama,
    first_query:   str,
    max_questions: int = 5,
) -> str:

    conversation: list[dict] = [
        {
            "role":    "system",
            "content": CLARIFICATION_SYSTEM_PROMPT.format(
                max_questions = max_questions
            )
        },
        {"role": "user", "content": first_query},
    ]

    print(f"\nUser: {first_query}")

    questions_asked = 0
    itr = 0
    while True:
        
        itr += 1
        raw_reply = _llm_call(model, conversation, temp=0.3)
        reply     = _strip_think_tags(raw_reply)
        print(f"\n[Debug] Iteration {itr}, raw reply:\n{raw_reply}\n")

        if "[READY_TO_STRUCTURE]" in reply:
            parts    = reply.split("[READY_TO_STRUCTURE]", 1)
            summary  = parts[1].strip() if len(parts) > 1 else ""
            preamble = parts[0].strip()
            if preamble:
                print(f"\nAssistant: {preamble}")
            print(f"\n[Situation understood. Structuring intent...]\n")
            return summary

        if questions_asked >= max_questions:
            conversation.append({"role": "assistant", "content": reply})
            conversation.append({
                "role":    "user",
                "content": "That is all the information I can provide. "
                           "Please proceed with what you have."
            })
            forced_reply = _llm_call(model, conversation, temp=0.0)
            forced_reply = _strip_think_tags(forced_reply)

            if "[READY_TO_STRUCTURE]" in forced_reply:
                parts   = forced_reply.split("[READY_TO_STRUCTURE]", 1)
                summary = parts[1].strip() if len(parts) > 1 else forced_reply
            else:
                summary = forced_reply

            print(f"\n[Maximum questions reached. Structuring intent...]\n")
            return summary

        conversation.append({"role": "assistant", "content": reply})
        print(f"\nAssistant: {reply}")
        # Only count as a question if the model actually asked one
        if "?" in reply:
            questions_asked += 1

        user_input = input("\nYou: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            raise SystemExit("User exited.")

        conversation.append({"role": "user", "content": user_input})


# ── Stage 2: Intent Extraction ────────────────────────────────────────────────

def extract_structured_intent(
    model:            Llama,
    situation_summary: str,
) -> dict | None:
    """
    Takes the situation summary produced by the clarification agent
    and calls the LLM with the intent extraction prompt.

    Returns the parsed structured intent JSON dict, or None on failure.
    """
    prompt = INTENT_EXTRACTION_PROMPT.format(
        situation_summary = situation_summary
    )

    messages = [{"role": "user", "content": prompt}]

    raw  = _llm_call(model, messages, temp=0.0)
    raw  = _strip_think_tags(raw)

    # Strip accidental markdown fences
    raw  = re.sub(r"^```(?:json)?\s*", "", raw)
    raw  = re.sub(r"\s*```$",          "", raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"\n✗ Failed to parse structured intent: {e}")
        print(f"  Raw output was:\n{raw[:400]}")
        return None


# ── Full Pipeline ─────────────────────────────────────────────────────────────

def run_intent_pipeline(
    model:         Llama,
    first_query:   str,
    max_questions: int = 5,
) -> tuple[dict, str] | None:
    """
    Runs the full intent pipeline: clarification agent followed by
    structured intent extraction.
 
    Parameters
    ----------
    model         : loaded Llama instance
    first_query   : the user's initial compliance question
    max_questions : maximum clarifying questions to ask  (default: 3)
 
    Returns
    -------
    A (intent_dict, situation_summary) tuple on success, or None if
    the pipeline could not produce a usable situation summary.
 
    The situation_summary is returned alongside the intent_dict because
    it is needed downstream for both retrieval (semantic query) and
    compliance reasoning (the situation the LLM reasons against).
    """
    situation_summary = run_clarification_agent(
        model         = model,
        first_query   = first_query,
        max_questions = max_questions,
    )
 
    if not situation_summary:
        print("✗ No situation summary produced.")
        return None
 
    intent = extract_structured_intent(model, situation_summary)
 
    if intent is None:
        return None
 
    return intent, situation_summary

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":

    MODEL_PATH = "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/models/qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf" # "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"

    print("Loading model...")
    model = load_model(MODEL_PATH)
    print("✓ Model loaded\n")

    print("FINRA Compliance Query System")
    print("=" * 40)
    print("Type your compliance question below.")
    print("Type 'quit' at any point to exit.\n")

    first_query = input("You: ").strip()
    if not first_query:
        print("No query entered.")
    else:
        result = run_intent_pipeline(model, first_query)
    
        if result:
            intent, summary = result
            print("\\n✓ Situation summary:")
            print(summary)
            print("\\n✓ Structured intent:")
            print(json.dumps(intent, indent=2))
        else:
            print("\\n✗ Intent structuring failed.")