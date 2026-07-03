import re as _re
import os
import time
from openai import max_retries
from collections import Counter
from typing import Any

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
    // HOW TO DECIDE: Find the party who is explicitly required
    // to DO something under the governing obligation of this clause.
    // IMPORTANT — Do not confuse a role or entity that appears
    // in a descriptive or qualifying phrase with the obligated
    // actor. For example, "the person associated with the
    // member" in the phrase "over whose account the person
    // associated with the member has control" is describing a
    // relationship, not bearing an obligation. The obligated
    // actor must be the party explicitly required to DO
    // something, not a party mentioned in passing. 
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
    //                                   obligation exists but its
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
    // HOW TO DECIDE: Identify the central object or entity that the
    // clause's obligation, definition, or scoping language acts upon.
    // Ask — what is being created, restricted, reviewed, protected,
    // measured, or defined HERE? The answer is the regulated_subject.
    // Ignore objects that appear only as side effects, downstream
    // consequences, or incidental references — the regulated_subject
    // must be what the clause is directly and primarily about.
    // Choose exactly ONE value from this list:
    //
    // "associated_person_account"  → use when the clause acts upon an
    //                               account held by an associated
    //                               person, particularly at a firm
    //                               other than their employer (rule 3210)
    // "customer_account"           → use when the clause acts upon a
    //                               customer's account as a structure
    //                               (opening, designation, discretionary
    //                               authority over it)
    // "customer_securities"        → use when the clause acts upon
    //                               securities belonging to a customer
    //                               specifically (lending, holding,
    //                               protecting) rather than the account
    //                               as a whole (rules 4330, 4340)
    // "margin_account"             → use when the clause acts upon a
    //                               margin account specifically,
    //                               distinct from a customer account
    //                               generally
    // "short_position"             → use when the clause acts upon a
    //                               short position specifically,
    //                               including fail-to-deliver
    // "government_securities"      → use when the clause acts upon
    //                               government securities specifically
    // "swap_position"               → use when the clause acts upon a
    //                               security-based swap position
    //                               specifically
    // "carrying_agreement"         → use when the clause acts upon the
    //                               carrying agreement document or
    //                               arrangement itself (rule 4311)
    // "business_continuity_plan"   → use when the clause acts upon the
    //                               BCP document itself — its creation,
    //                               content, or testing
    // "fidelity_bond"              → use when the clause acts upon the
    //                               fidelity bond coverage itself — its
    //                               existence or amount
    // "payment_or_gratuity"        → use when the clause acts upon a
    //                               payment, gift, or compensation
    //                               arrangement as the thing being
    //                               restricted or permitted (rules
    //                               3220, 2040)
    // "CRD_record"                 → use when the clause acts upon
    //                               information recorded in the CRD
    //                               system (rules 2080, 2081)
    // "written_procedures"         → use when the clause acts upon the
    //                               procedures document itself (its
    //                               existence, content, or review)
    //                               rather than on the activity the
    //                               procedures govern
    // "business_clock"             → use when the clause acts upon the
    //                               synchronization of business clocks
    //                               used for recordkeeping
    // "capital_position"           → use when the clause acts upon a
    //                               firm's net capital or financial
    //                               condition
    // "OSJ"                        → use when the clause acts upon an
    //                               Office of Supervisory Jurisdiction
    //                               as a location/structure
    // "branch_office"              → use when the clause acts upon a
    //                               branch office as a location/
    //                               structure
    // "non_branch_location"        → use when the clause acts upon a
    //                               location explicitly classified as
    //                               non-branch
    // "supervisory_personnel"      → use when the clause acts upon who
    //                               is designated, qualified, or
    //                               assigned as a supervisor
    // "recommendation"              → use when the clause acts upon the
    //                               act of recommending a security or
    //                               strategy to a customer
    // "communication"              → use when the clause acts upon a
    //                               communication (its content,
    //                               approval, filing, or review) sent
    //                               to or received from any party
    // "registered_person"          → use when the clause acts upon a
    //                               registered person's conduct,
    //                               registration, or status
    // "associated_person"          → use when the clause acts upon an
    //                               associated person's conduct or
    //                               status, with no specific account
    //                               or registration category implicated
    // "customer"                   → use when the clause acts upon a
    //                               customer directly (their interests,
    //                               notifications to them, or
    //                               protections owed to them) rather
    //                               than their account or assets
    //                               specifically
    // "member_firm"                → use when the clause acts upon the
    //                               member firm itself as an entity —
    //                               its status, registration, or
    //                               existence — distinct from its
    //                               capital position or records
    // "books_and_records"          → use when the clause acts upon
    //                               records or documentation generally
    //                               required to be kept, distinct from
    //                               a specific document type already
    //                               listed above (e.g. not specifically
    //                               written_procedures or a BCP)
    // "security_position"          → use when the clause acts upon a
    //                               position in a security generally,
    //                               with no more specific value
    //                               applicable (not short, not swap)
    // "transaction"                → use when the clause acts upon a
    //                               transaction generally, with no more
    //                               specific value applicable
    // "other"                      → use when the clause clearly acts
    //                               upon something, but that something
    //                               does not match any value above
    // "null"                       → use when the clause acts upon
    //                               nothing identifiable — no party,
    //                               object, document, or status is
    //                               being established, restricted,
    //                               reviewed, protected, or defined
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
    // HOW TO DECIDE: Ask — what is the member or person
    // actually required to DO under the governing obligation
    // this clause belongs to? Match that action to the closest
    // value in the list above.
    // IMPORTANT — If the clause is definitional, a sub-element,
    // or a list item, do not attempt to derive an activity from
    // verbs used in a descriptive or scoping context (e.g.
    // "has control", "shall include", "is held by" are
    // structural phrases, not regulated activities). Always
    // match to the activity of the governing obligation, not
    // to incidental verbs within the clause text.
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

    "applies_to_firm_type": [],
    // List ALL firm types this rule text applies to.
    // A rule text may apply to more than one firm type — for example,
    // a carrying agreement provision may apply to both "carrying_firm"
    // AND "introducing_firm". Include every value that applies.
    //
    // HOW TO DECIDE: Ask — which firm type does this rule text
    // concern? If the rule text imposes an obligation, identify
    // which firm type bears that obligation. If the rule text is
    // definitional or descriptive, identify which firm type the
    // definition or description is scoping or qualifying. The
    // firm type must be the one the rule text is fundamentally
    // ABOUT — not merely one that appears in passing or as
    // background context.
    // If the rule text concerns a specific firm role, always
    // prefer that specific value over "broker_dealer".
    //
    // Choose one or more values from this list, in order of
    // specificity (most specific first):
    //
    // "carrying_firm"               → the rule text fundamentally
    //                                 concerns a firm in its capacity
    //                                 as the party that carries or
    //                                 clears customer or broker-dealer
    //                                 accounts — i.e., the firm that
    //                                 holds the assets, computes margin,
    //                                 or maintains net capital against
    //                                 those accounts. Common in rules
    //                                 4210, 4220, 4230, 4311, 4314.
    //
    // "introducing_firm"            → the rule text fundamentally
    //                                 concerns a firm in its capacity
    //                                 as the party that introduces
    //                                 customer accounts to a carrying
    //                                 firm, where the carrying firm
    //                                 executes or settles transactions
    //                                 on its behalf. Common in
    //                                 rule 4311.
    //
    // "clearing_agency_participant" → the rule text fundamentally
    //                                 concerns a firm in its capacity
    //                                 as a participant of a registered
    //                                 clearing agency, particularly
    //                                 with respect to close-out or
    //                                 delivery obligations arising
    //                                 from that participation.
    //                                 Common in rule 4320.
    //
    // "tape_recording_firm"         → the rule text fundamentally
    //                                 concerns a firm in its capacity
    //                                 as one that has been identified
    //                                 by FINRA as subject to tape
    //                                 recording requirements based
    //                                 on its hiring history or prior
    //                                 disciplinary record. Common in
    //                                 rule 3170.
    //
    // "financial_institution"       → the rule text fundamentally
    //                                 concerns a bank, thrift, or
    //                                 credit union in its capacity
    //                                 as the premises provider in a
    //                                 networking arrangement with a
    //                                 broker-dealer. Common in
    //                                 rule 3160.
    //
    // "section_15C_member"          → the rule text fundamentally
    //                                 concerns a firm in its capacity
    //                                 as a registered government
    //                                 securities dealer or broker
    //                                 under Section 15C of the
    //                                 Exchange Act.
    //
    // "restricted_firm"             → the rule text fundamentally
    //                                 concerns a firm in its capacity
    //                                 as one subject to heightened
    //                                 obligations under Rule 4111
    //                                 based on its concentration of
    //                                 disciplinary history.
    //
    // "ATS_operator"                → the rule text fundamentally
    //                                 concerns a firm in its capacity
    //                                 as the operator of an alternative
    //                                 trading system.
    //
    // "broker_dealer"               → the rule text concerns member
    //                                 firms generally, with no specific
    //                                 firm role as its focus. This is
    //                                 the default and fallback value.
    //                                 Always include "broker_dealer"
    //                                 alongside any specific firm type
    //                                 when the rule text operates within
    //                                 the broader broker-dealer
    //                                 regulatory framework (e.g. a
    //                                 carrying firm provision under
    //                                 rule 4210 still applies to a firm
    //                                 that is also a broker-dealer, so
    //                                 include both ["carrying_firm",
    //                                 "broker_dealer"]). If uncertain,
    //                                 use ["broker_dealer"].

    "involves_customer": false,
    // DEFINITIONAL TEST: A clause involves_customer ONLY if the party
    // being acted upon, protected, notified, or transacted with is a
    // person or account that holds securities or funds with, or
    // receives services from, the member firm in a non-employment,
    // non-regulatory capacity. This includes any case where the
    // clause's obligation exists specifically to protect, inform, or
    // govern dealings with such a person, regardless of the specific
    // word used to refer to them (customer, client, accountholder).
    // It does NOT include a person or account that holds that
    // relationship with an entity OTHER than the member firm itself
    // (e.g. a customer of another broker-dealer), and it does NOT
    // include the mere appearance of the word "customer" inside a
    // definition that this specific clause does not itself rely on
    // to impose its own obligation.
    // Otherwise, set to false.

    "involves_third_party": false,
    // DEFINITIONAL TEST: A clause involves_third_party ONLY if the
    // governing obligation requires, restricts, or describes
    // an interaction with a party that is BOTH (a) legally and
    // organizationally distinct from the member firm itself and from
    // the member firm's own associated/registered persons, AND (b)
    // not a customer of the member firm as defined for
    // involves_customer above. This covers any such party regardless
    // of its specific type — another firm, an institution, a
    // regulator acting as a counterparty to an obligation (not merely
    // as the rule's enforcing authority), an individual intermediary,
    // an issuer, a guarantor, or any other organizationally separate
    // party — as long as that party is the one the obligation is
    // ABOUT or BETWEEN, not merely named in passing or in a
    // definitional aside the clause does not rely on.
    // Otherwise, set to false.

    "has_financial_threshold": false,
    // DEFINITIONAL TEST: A clause has_financial_threshold ONLY if whether
    // or how the obligation applies is conditioned on a specific,
    // quantifiable financial value — a dollar figure, a percentage,
    // a ratio, or an explicit reference to such a value defined
    // elsewhere that this clause's applicability depends on. The
    // threshold must be a CONDITION on the obligation (a gate that
    // determines applicability or a quantity the obligation must
    // satisfy), not merely a topic the clause discusses. If this
    // clause is a fragment, resolve the threshold from the nearest
    // complete governing obligation it belongs to.
    // A clause that discusses capital, margin, or value in purely
    // qualitative terms — with no specific figure stated or
    // referenced as a condition — is false. A clause whose
    // applicability or required action changes at a specific
    // quantified point is true.
    // Otherwise, set to false.

    "documentation_required": false,
    // DEFINITIONAL TEST: A clause documentation_required ONLY if its own
    // governing obligation imposes a duty to bring a written
    // artifact into existence, retain it, submit it, or provide it to
    // another party, as a condition of compliance. The artifact can
    // be any form of record, notice, plan, authorization, agreement,
    // or filing, regardless of the specific word used to describe it.
    // It does NOT include a clause that merely defines the contents
    // or required elements OF a document that some OTHER clause is
    // the one imposing the duty to create — only the clause that
    // itself imposes the creation/retention/submission duty is true.
    // Otherwise, set to false.

    "frequency": null,
    // How often the obligation must be performed.
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
    // Choose exactly ONE value or null:
    //
    // "ongoing"       → the obligation exists as a permanent state
    //                   that the actor must maintain without
    //                   interruption. The compliance question is
    //                   "are you in compliance right now?" — not
    //                   "has the triggering event occurred?" A
    //                   clause is ongoing even if it contains
    //                   conditional language, as long as the
    //                   underlying duty is continuous once the
    //                   arrangement or relationship it governs
    //                   is in place
    // "annual"        → once per calendar year
    // "triennial"     → once every three years
    // "quarterly"     → once per quarter
    // "monthly"       → once per month
    // "daily"         → every business day
    // "semi_annual"   → twice per year
    // "upon_trigger"  → the obligation is dormant by default and
    //                   activates only when a discrete, external
    //                   event occurs that would not occur in the
    //                   normal course of the arrangement. The
    //                   compliance question is "has the event
    //                   happened?" — not "are you maintaining
    //                   the required state?" Once the triggering
    //                   event resolves, the obligation returns
    //                   to dormant
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
    // If a frequency is clearly stated but does not match
    // any known value, use "other". 
    // When in doubt, use null. null is the safe default.

    "reporting_recipient": null,
    // If the clause requires submitting a report or filing,
    // identify who receives it. 
    // HOW TO DECIDE: Identify whether the governing obligation
    // requires submitting a report or filing, and if so, who
    // receives it.
    // Choose ONE value or null:
    //
    // "FINRA"                         → report goes to FINRA
    // "SEC"                           → report goes to the SEC
    // "self_regulatory_organization"  → report goes to an SRO
    // "designated_examining_authority" → report or request goes to
    //                                   the member's DEA (rule 4230)
    // "senior_management"             → report goes to firm leadership
    // "customer"                      → notification goes to customer
    // "other"                         → report goes to a recipient
    //                                   not listed above
    // null                            → no reporting required
    //
    // If no reporting obligation is stated, use null. 
    // If a reporting obligation exists but no recipient
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
    def __init__(self, model_name):
        from openai import OpenAI
        self._client = OpenAI(
            api_key  = os.getenv("TAMUS_AI_CHAT_API_KEY"),
            base_url = "https://chat-api.tamu.ai/api/v1",
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

all_rules = {key:val for key, val in all_rules.items() if key in {rule_id}}

raw_clause = all_rules[rule_id]['clauses']["FINRA-4210(f)(2)(A)(ii)b.2.A."]
rule_meta = all_rules[rule_id]['meta']
clauses_dict = all_rules[rule_id]['clauses']

print("Raw clause text:\n", raw_clause)


prompt = build_normalisation_prompt(
    target      = raw_clause,
    rule_id     = rule_id,      
    rule_name   = rule_meta["name"],
    all_clauses = clauses_dict,
)

models = ["protected.o3", "protected.Claude Opus 4.7", "protected.gpt-5", "protected.gemini-2.5-pro"]
all_results = []

for model_name in models:
    model = _TAMUBackend(model_name=model_name)
    result = normalize_clause(model, prompt)
    all_results.append(result)
    print("Normalized clause data obtained with " + model_name + ":\n", json.dumps(result, indent=2))
    time.sleep(5)

aggregate_result = aggregate_json_objects(all_results)
print("Aggregate result:\n", json.dumps(aggregate_result, indent=2))