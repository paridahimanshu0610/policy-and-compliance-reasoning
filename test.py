from time import time

from openai import max_retries


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
- TARGET CLAUSE   : The specific clause you must structure.
- FULL RULE TEXT  : The complete rule text provided for context.
                    Read this alongside the TARGET CLAUSE to fully
                    understand the meaning and intent of what you
                    are structuring.

SCHEMA TO POPULATE
==================
{{
    "obligated_actor": "",
    // The party who must comply with this clause.
    // Choose exactly ONE value from this list:
    //
    // "member"                        → use when the obligation's
    //                                   subject is "member" or
    //                                   "broker-dealer" generally,
    //                                   with no carrying/introducing
    //                                   distinction stated
    // "associated_person"             → use when the subject is any
    //                                   person associated with a
    //                                   member, and the clause does
    //                                   NOT specify a registration
    //                                   category (representative,
    //                                   principal) for that person
    // "registered_person"             → use when the subject is
    //                                   explicitly described as
    //                                   "registered" but no specific
    //                                   registration category
    //                                   (representative vs principal)
    //                                   is stated
    // "registered_representative"     → use when the subject is
    //                                   explicitly identified as a
    //                                   registered representative
    // "registered_principal"          → use when the subject is
    //                                   explicitly identified as a
    //                                   registered principal
    // "supervisory_personnel"         → use when the subject is
    //                                   identified by a supervisory
    //                                   role or title (e.g.
    //                                   "designated supervisor",
    //                                   "OSJ manager") rather than by
    //                                   registration category
    // "CEO"                           → use when the subject is
    //                                   explicitly the Chief
    //                                   Executive Officer (or
    //                                   equivalent officer) by title
    // "CCO"                           → use when the subject is
    //                                   explicitly the Chief
    //                                   Compliance Officer by title
    // "CFO"                           → use when the subject is
    //                                   explicitly the Chief
    //                                   Financial Officer by title
    // "financial_operations_principal" → use when the subject is
    //                                   explicitly the Financial and
    //                                   Operations Principal by title
    // "senior_management"             → use when the subject is a
    //                                   firm's leadership collectively
    //                                   (e.g. "senior management") with
    //                                   no single officer title named
    // "carrying_firm"                 → use ONLY when the clause text
    //                                   itself uses the term "carrying
    //                                   firm" (or "carrying broker-
    //                                   dealer") as the subject — not
    //                                   merely implied by context
    // "introducing_firm"              → use ONLY when the clause text
    //                                   itself uses the term
    //                                   "introducing firm" (or
    //                                   "introducing broker-dealer")
    //                                   as the subject — not merely
    //                                   implied by context
    // "clearing_agency_participant"   → use when the subject is
    //                                   explicitly a participant of a
    //                                   registered clearing agency
    // "other"                         → use when a governing
    //                                   obligation exists (a modal
    //                                   verb is present, in this
    //                                   clause or an ancestor) but its
    //                                   subject does not match any
    //                                   value above
    // "null"                          → use ONLY when no modal
    //                                   obligation verb exists in the
    //                                   clause or anywhere in its
    //                                   ancestor chain — i.e. the
    //                                   clause is purely definitional
    //                                   or descriptive with no
    //                                   obligation attached at any level
    //
    // HOW TO DECIDE: Find the party who is explicitly required
    // to DO something under the governing obligation of this clause. 
    //
    // IMPORTANT — Do not confuse a role or entity that appears
    // in a descriptive or qualifying phrase with the obligated
    // actor. For example, "the person associated with the
    // member" in the phrase "over whose account the person
    // associated with the member has control" is describing a
    // relationship, not bearing an obligation. The obligated
    // actor must be the party explicitly required to DO
    // something, not a party mentioned in passing.
    //
    // Use "carrying_firm" or "introducing_firm" when the clause
    // explicitly names one of these roles as the party bearing
    // the obligation (common in rules 4311, 4314). Use "member"
    // when the obligation applies to member firms generally
    // without distinguishing role.
    //
    // Always be as specific as possible.
    // Use "other" only if an obligation clearly exists but the
    // obligated party does not match any listed role.
    // Use "null" only if the clause imposes no obligation at all
    // and no party is required to DO anything.

"regulated_subject": "",
    // The entity or object that the clause is about — whether the
    // clause imposes an obligation, sets a definition, or states
    // a scope/eligibility condition.
    // Choose exactly ONE value from this list:
    //
    // "associated_person"          → use when the obligation acts upon
    //                               an associated person's conduct or
    //                               status, with no specific account
    //                               or registration category implicated
    // "associated_person_account"  → use when the obligation acts upon
    //                               an account held by an associated
    //                               person, particularly at a firm
    //                               other than their employer (rule 3210)
    // "registered_person"          → use when the obligation acts upon
    //                               a registered person's conduct,
    //                               registration, or status
    // "customer"                   → use when the obligation acts upon
    //                               a customer directly (their
    //                               interests, notifications to them,
    //                               or protections owed to them) rather
    //                               than their account or assets
    //                               specifically
    // "customer_account"           → use when the obligation acts upon
    //                               a customer's account as a structure
    //                               (opening, designation, discretionary
    //                               authority over it)
    // "customer_securities"        → use when the obligation acts upon
    //                               securities belonging to a customer
    //                               specifically (lending, holding,
    //                               protecting) rather than the account
    //                               as a whole (rules 4330, 4340)
    // "member_firm"                → use when the obligation acts upon
    //                               the member firm itself as an
    //                               entity — its status, registration,
    //                               or existence — distinct from its
    //                               capital position or records
    // "supervisory_personnel"      → use when the obligation acts upon
    //                               who is designated, qualified, or
    //                               assigned as a supervisor
    // "OSJ"                        → use when the obligation acts upon
    //                               an Office of Supervisory
    //                               Jurisdiction as a location/structure
    // "branch_office"              → use when the obligation acts upon
    //                               a branch office as a location/
    //                               structure
    // "non_branch_location"        → use when the obligation acts upon
    //                               a location explicitly classified as
    //                               non-branch
    // "written_procedures"         → use when the obligation acts upon
    //                               the procedures document itself
    //                               (its existence, content, or review)
    //                               rather than on the activity the
    //                               procedures govern
    // "communication"              → use when the obligation acts upon
    //                               a communication (its content,
    //                               approval, filing, or review) sent
    //                               to or received from any party
    // "transaction"                → use when the obligation acts upon
    //                               a transaction generally, with no
    //                               more specific value applicable
    // "recommendation"              → use when the obligation acts upon
    //                               the act of recommending a security
    //                               or strategy to a customer
    // "capital_position"           → use when the obligation acts upon
    //                               a firm's net capital or financial
    //                               condition
    // "margin_account"             → use when the obligation acts upon
    //                               a margin account specifically,
    //                               distinct from a customer account
    //                               generally
    // "security_position"          → use when the obligation acts upon
    //                               a position in a security generally,
    //                               with no more specific value
    //                               applicable (not short, not swap)
    // "short_position"             → use when the obligation acts upon
    //                               a short position specifically,
    //                               including fail-to-deliver
    // "government_securities"      → use when the obligation acts upon
    //                               government securities specifically
    // "swap_position"               → use when the obligation acts upon
    //                               a security-based swap position
    //                               specifically
    // "carrying_agreement"         → use when the obligation acts upon
    //                               the carrying agreement document or
    //                               arrangement itself (rule 4311)
    // "business_continuity_plan"   → use when the obligation acts upon
    //                               the BCP document itself — its
    //                               creation, content, or testing
    // "fidelity_bond"              → use when the obligation acts upon
    //                               the fidelity bond coverage itself —
    //                               its existence or amount
    // "payment_or_gratuity"        → use when the obligation acts upon
    //                               a payment, gift, or compensation
    //                               arrangement as the thing being
    //                               restricted or permitted (rules
    //                               3220, 2040)
    // "CRD_record"                 → use when the obligation acts upon
    //                               information recorded in the CRD
    //                               system (rules 2080, 2081)
    // "books_and_records"          → use when the obligation acts upon
    //                               records or documentation generally
    //                               required to be kept, distinct from
    //                               a specific document type already
    //                               listed above (e.g. not specifically
    //                               written_procedures or a BCP)
    // "business_clock"             → use when the obligation acts upon
    //                               the synchronization of business
    //                               clocks used for recordkeeping
    // "other"                      → use when a governing obligation
    //                               exists and clearly acts upon
    //                               something, but that something does
    //                               not match any value above
    // "null"                       → use when no governing obligation
    //                               exists anywhere in the clause's
    //                               ancestor chain (the clause is
    //                               purely definitional or descriptive)
    //
    // HOW TO DECIDE: Ask yourself — what is being supervised,
    // restricted, reviewed, protected, measured, or defined by
    // this clause (via its governing obligation if one exists,
    // or via the clause's own definitional or scoping language
    // if it doesn't)? That is the regulated_subject.
    //
    // Use "associated_person_account" when the subject is an
    // account held by an associated person at another firm
    // (rule 3210). Use "customer_securities" when the subject
    // is securities belonging to customers that the member
    // holds or lends (rules 4330, 4340). Use "CRD_record" when
    // the subject is information recorded in the CRD system
    // (rules 2080, 2081). Use "payment_or_gratuity" when the
    // subject is a payment, gift, or compensation arrangement
    // (rules 3220, 2040).
    //
    // Always be as specific as possible.
    // Use "other" only if a regulated subject clearly exists but
    // does not match any listed entity or object.
    // Use "null" only if there is no entity or object being
    // supervised, restricted, reviewed, protected, measured, or
    // defined — regardless of whether the clause imposes an
    // obligation.

    "activity_type": "",
    // The regulated activity this clause governs.
    // THIS IS THE MOST IMPORTANT FIELD AND CANNOT BE SET NULL.
    // Choose exactly ONE value from this list:
    //
    // 2000 series:
    // "conduct_standard"             → general standards of commercial
    //                                  honor or fraud prohibition
    //                                  (rules 2010, 2020)
    // "pay_to_play"                  → distribution/solicitation
    //                                  activities involving government
    //                                  entities; political contribution
    //                                  restrictions (rule 2030)
    // "payment_to_unregistered_person" → paying compensation to persons
    //                                  not registered as required;
    //                                  finder arrangements (rule 2040)
    // "fiduciary_information_use"    → use or misuse of ownership
    //                                  information obtained in a
    //                                  fiduciary capacity (rule 2060)
    // "FINRA_employee_transaction"   → handling accounts of FINRA
    //                                  employees; loans or gifts to
    //                                  FINRA employees (rule 2070)
    // "expungement"                  → seeking or conditioning
    //                                  expungement of CRD records
    //                                  (rules 2080, 2081)
    // "know_your_customer"           → knowing essential facts about
    //                                  customers and their accounts
    //                                  (rule 2090)
    //
    // 3000 series:
    // "supervision"                  → establishing and maintaining
    //                                  supervisory systems or controls
    //                                  (rules 3110, 3120)
    // "inspection"                   → conducting inspections of offices
    //                                  or locations (rule 3110)
    // "review"                       → reviewing transactions,
    //                                  correspondence, or complaints
    //                                  (rule 3110)
    // "certification"                → annual CEO/CCO certification of
    //                                  compliance processes (rule 3130)
    // "registration_verification"    → verifying registration status
    //                                  of associated persons (rule 3110)
    // "mail_holding"                 → holding customer mail at the
    //                                  member's office (rule 3150)
    // "networking_arrangement"       → broker-dealer services on
    //                                  financial institution premises
    //                                  (rule 3160)
    // "tape_recording"               → tape recording of registered
    //                                  persons' conversations (rule 3170)
    // "outside_account_disclosure"   → disclosure and monitoring of
    //                                  associated persons' accounts at
    //                                  other broker-dealers (rule 3210)
    // "gifts_and_gratuities"         → giving or receiving payments,
    //                                  gifts, or gratuities involving
    //                                  employees of other firms (rule 3220)
    // "telemarketing"                → telephone solicitation rules and
    //                                  do-not-call obligations (rule 3230)
    // "borrowing_lending"            → borrowing from or lending to
    //                                  customers (rule 3240)
    // "beneficiary_designation"      → registered person named as
    //                                  beneficiary or trustee for a
    //                                  customer (rule 3241)
    // "designation"                  → designating accounts by number
    //                                  or symbol rather than customer
    //                                  name (rule 3250)
    // "discretionary_trading"        → granting or exercising
    //                                  discretionary authority over
    //                                  customer accounts (rule 3260)
    // "outside_business_activity"    → engaging in business activity
    //                                  outside the member firm (rule 3270)
    // "private_securities_transaction" → participating in securities
    //                                  transactions outside the member
    //                                  firm (rule 3280)
    // "AML_monitoring"               → developing and implementing
    //                                  AML programs and controls
    //                                  (rule 3310)
    //
    // 4000 series:
    // "margin_calculation"           → calculating initial or
    //                                  maintenance margin requirements
    //                                  (rules 4210, 4240)
    // "margin_recordkeeping"         → maintaining daily margin records
    //                                  for customer accounts (rule 4220)
    // "margin_extension_request"     → submitting or reporting Reg T
    //                                  extension requests (rule 4230)
    // "swap_margin"                  → margin requirements for
    //                                  security-based swaps (rule 4240)
    // "carrying_agreement"           → entering into, approving, or
    //                                  administering carrying agreements
    //                                  between carrying and introducing
    //                                  firms (rule 4311)
    // "securities_lending"           → lending or borrowing securities;
    //                                  disclosing capacity in loan
    //                                  transactions (rule 4314)
    // "short_sale_delivery"          → closing out fail-to-deliver
    //                                  positions in short sales (rule 4320)
    // "customer_asset_protection"    → obtaining authorization to lend
    //                                  customer securities; protecting
    //                                  fully paid or excess margin
    //                                  securities (rule 4330)
    // "callable_securities_allocation" → allocating called or redeemed
    //                                  securities among customers on a
    //                                  fair and impartial basis (rule 4340)
    // "fidelity_bond_maintenance"    → maintaining blanket fidelity bond
    //                                  coverage at required minimums
    //                                  (rule 4360)
    // "business_continuity_planning" → creating and maintaining a
    //                                  written business continuity plan
    //                                  (rule 4370)
    // "BCDR_testing"                 → participating in FINRA's periodic
    //                                  business continuity and disaster
    //                                  recovery plan testing (rule 4380)
    //
    // HOW TO DECIDE: Ask — what is the member or person
    // actually required to DO under the governing obligation
    // this clause belongs to? Match that action to the closest
    // value in the list above.
    //
    // IMPORTANT — If the clause is definitional, a sub-element,
    // or a list item, do not attempt to derive an activity from
    // verbs used in a descriptive or scoping context (e.g.
    // "has control", "shall include", "is held by" are
    // structural phrases, not regulated activities). Always
    // match to the activity of the governing obligation, not
    // to incidental verbs within the clause text.

    "applies_to_firm_type": [],
    // List all firm types this clause applies to.
    // Choose one or more values from this list:
    //
    // "broker_dealer"               → applies to all broker-dealers;
    //                                 use as the default when no
    //                                 specific firm type is named
    // "carrying_firm"               → applies specifically to firms
    //                                 that carry customer accounts
    //                                 (rules 4311, 4314, 4220, 4230)
    // "introducing_firm"            → applies specifically to firms
    //                                 that introduce accounts to
    //                                 carrying firms (rule 4311)
    // "clearing_agency_participant" → applies to participants of a
    //                                 registered clearing agency
    //                                 (rule 4320)
    // "section_15C_member"          → applies to government securities
    //                                 dealers under Section 15C
    // "restricted_firm"             → applies to firms subject to
    //                                 Rule 4111 obligations
    // "ATS_operator"                → applies to operators of
    //                                 alternative trading systems
    // "tape_recording_firm"         → applies to firms with a tape
    //                                 recording history under 3170
    // "financial_institution"       → applies to financial institutions
    //                                 in networking arrangements
    //                                 (rule 3160)
    //
    // HOW TO DECIDE: Identify which firm type the governing
    // obligation applies to. If the clause applies to members
    // generally with no specific firm-type restriction, use
    // ["broker_dealer"]. If uncertain, use ["broker_dealer"].

    "involves_customer": false,
    // Set to true if the clause directly concerns:
    // - customer accounts or assets
    // - interactions between firm employees and customers
    // - explicit protection of the member firm's own customers,
    //   not third parties or customers of an external entity
    // - any mention of "public customers", "retail customers",
    //   "clients" who are direct customers of the member firm, 
    //   or any direct reference to customers of the member firm
    // Otherwise set to false.

    "involves_third_party": false,
    // Set to true if the clause involves ANY entity or individual
    // outside the member firm and its associated persons, such as:
    // - another broker-dealer or financial institution
    // - a bank, counterparty, or clearing agency
    // - an outside employer or government entity
    // - a registered national securities exchange
    // - a self-regulatory organization
    // - any external venue, platform, or institution not
    //   itself part of the member firm
    // - any individual person outside the member firm, such as
    //   a finder, referrer, foreign national, or third-party agent
    // - any customer, securities owner, or external account holder
    //   being referred to or transacting with the member firm
    // - any issuer, counterparty, or external organization
    //   named or implied in the clause
    // SIMPLE CHECK: If the clause names or references ANY
    // person, organization, institution, venue, or entity
    // other than the member firm or its own associated persons,
    // set this to true. The presence of any named or implied
    // external party — whether an individual or an organization —
    // is sufficient.
    // Otherwise set to false.

    "has_financial_threshold": false,
    // Set to true if the clause's applicability or
    // requirements depend on a financial metric such as:
    // - capital ratios or net capital levels
    // - gross revenue thresholds
    // - margin percentages or account values
    // - bond coverage minimums (e.g. $100,000, $250,000)
    // - dollar contribution limits (e.g. $350 per election)
    // Otherwise set to false.

    "documentation_required": false,
    // Set to true if the clause explicitly requires:
    // - a written record, report, plan, or filing
    // - documentation to be retained or submitted
    // - written authorization or written notice
    // - written procedures to be established
    // Look for phrases like "evidenced in writing",
    // "written report", "kept on file", "must retain",
    // "written authorization", "written notice",
    // "written business continuity plan".
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
    //    in a definitional or scoping context are NOT frequency
    //    signals for the compliance obligation.
    // 3. CRITICAL: Many clauses use obligation language — words
    //    like "shall", "must", "is required to", "is prohibited
    //    from" — to express that a duty exists, not to express
    //    how often it must be performed. Do NOT treat obligation
    //    language as evidence of frequency. Always look for a
    //    separate, explicit signal that answers "how often?"
    //    before assigning any value.
    // 4. If a frequency is clearly stated but does not match
    //    any known value, use "other".
    // 5. When in doubt, use null. null is the safe default.

    "reporting_recipient": null,
    // If the clause requires submitting a report or filing,
    // identify who receives it. Choose ONE value or null:
    //
    // null                            → no reporting required
    // "FINRA"                         → report goes to FINRA
    // "SEC"                           → report goes to the SEC
    // "senior_management"             → report goes to firm leadership
    // "customer"                      → notification goes to customer
    // "self_regulatory_organization"  → report goes to an SRO
    // "designated_examining_authority" → report or request goes to
    //                                   the member's DEA (rule 4230)
    // "other"                         → report goes to a recipient
    //                                   not listed above
    //
    // HOW TO DECIDE: Identify whether the governing obligation
    // requires submitting a report or filing, and if so, who
    // receives it. If no reporting obligation is stated, use
    // null. If a reporting obligation exists but no recipient
    // is named, use "other".
}}

NOW PROCESS THE FOLLOWING INPUT
================================
RULE ID       : {rule_id}
RULE NAME     : {rule_name}
PARENT REF    : {parent_ref}
CLAUSE REF    : {clause_ref}
TARGET CLAUSE : {target_clause}
FULL RULE TEXT: {context_text}"""

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

def build_normalisation_prompt(
    target:    dict,
    rule_id:   str,
    rule_name: str,
    all_clauses: dict
) -> str:
    target_clause = target.get("raw_text", "") # This is not the merged text. It is the raw text of the target clause itself.
    
    clause_ref = target["clause_ref"]
    parent_ref = target.get("parent_clause") or "null"

    # Merging the current clause with all its ancestors to build a full clause
    context_text = merge_clause_to_completion(clause_ref, all_clauses, merge_until_root=True)
    context_text = context_text.get("raw_text", "")

    print("Context text:\n", context_text)
    
    # Format the bundle into the RAW CLAUSE TEXT block
    # raw_clause_text = format_bundle_for_prompt(bundle)

    # Exporting the target_clause an context_text as a dictionary to "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/test_context.json"
    with open("/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/test_context.json", "w") as f:
        json.dump({
            "rule_name": rule_name,
            "target_clause": target_clause,
            "context_text": context_text,
        }, f, indent=2)

    prompt = CLAUSE_NORMALISATION_PROMPT.format(
        rule_id         = rule_id,
        rule_name       = rule_name,
        parent_ref      = parent_ref,
        clause_ref      = clause_ref,
        target_clause   = target_clause,
        context_text  = context_text,
        # raw_clause_text = raw_clause_text,
    )
    
    # Exporting the prompt to "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/test_context.txt"
    with open("/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/test_context.txt", "w") as f:
        f.write(prompt)

    return prompt


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

class _TAMUBackend:
    """
    Wraps the TAMU Chat API (OpenAI-compatible) so it presents the same
    .create_chat_completion(messages, temperature, max_tokens) interface
    as llama_cpp.Llama — no changes needed in normalize_clause.
    """
    def __init__(self):
        from openai import OpenAI
        self._client = OpenAI(
            api_key  = "sk-8e7ccb67c4ab4d5eb69b5ed8e8d08814",
            base_url = "https://chat-api.tamu.ai/api/v1",
        )
        self._model = MODEL_NAME

    def create_chat_completion(
        self,
        messages:    list[dict],
        temperature: float = 0.0,
        max_tokens:  int   = 16384,
    ) -> dict:
        """Returns a dict shaped like llama_cpp's chat completion output."""
        raw = self._client.chat.completions.create(
            model       = self._model,
            messages    = messages,
            # temperature = temperature,
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

def normalize_clause(
    model,
    prompt:      str,
    max_retries: int = 3,
) -> dict | None:
    import re as _re
    import time

    messages = [{"role": "user", "content": prompt}]

    for attempt in range(1, max_retries + 1):
        try:
            response = model.create_chat_completion(
                messages    = messages,
                # temperature = 0.0,
                max_tokens  = 16384,
            )
            print("Response:\n", response)
            print("----------------------")

            raw = response["choices"][0]["message"]["content"].strip()

            # Guard: empty response
            if not raw:
                print(f"    ✗ Empty response (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(1)
                continue

            # Strip <think>...</think> blocks (Gemini 2.5 Pro)
            raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()

            # Guard: response was only a <think> block — likely truncated mid-think
            if not raw:
                print(f"    ✗ Response was only a <think> block — likely truncated (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(1)
                continue

            # Strip markdown code fences
            raw = _re.sub(r"^```(?:json)?\s*", "", raw)
            raw = _re.sub(r"\s*```$",           "", raw)
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

import json
PARSED_CHECKPOINT = "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/data/parsed_rules.json"

with open(PARSED_CHECKPOINT) as f:
    all_rules = json.load(f)

rule_id = "4210"
MODEL_NAME = "protected.gemini-2.5-pro" # "protected.o3", "protected.Claude Opus 4.7", "protected.gpt-5", "protected.gemini-2.5-pro"
all_rules = {key:val for key, val in all_rules.items() if key in {rule_id}}

raw_clause = all_rules[rule_id]['clauses']["FINRA-4210(a)(9)"]
rule_meta = all_rules[rule_id]['meta']
clauses_dict = all_rules[rule_id]['clauses']

print("Raw clause text:\n", raw_clause)


prompt = build_normalisation_prompt(
    target      = raw_clause,
    rule_id     = rule_id,      
    rule_name   = rule_meta["name"],
    all_clauses = clauses_dict,
)

model = _TAMUBackend()
result = normalize_clause(model, prompt)
print("Normalized clause data:\n", json.dumps(result, indent=2))