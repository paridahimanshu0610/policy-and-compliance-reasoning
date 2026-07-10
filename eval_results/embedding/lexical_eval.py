import json
import numpy as np
from pathlib import Path

# set_a has lexically similar clause pairs that are semantically different while set_b has lexically different clause pairs that are semantically similar. These sets are used for evaluating the quality of embeddings in capturing semantic similarity beyond lexical similarity.
set_a = [
    (("FINRA-2030(c)(1)", "FINRA-2030(c)(3)(A)(ii)"),
     "Both provisions reference the $350 threshold, but FINRA-2030(c)(1) is the permanent de minimis exception allowing a covered associate to make small contributions to officials they can vote for, while FINRA-2030(c)(3)(A)(ii) is a condition of the separate, temporary 'returned contribution' cure that excepts a firm from the two-year ban if the contribution is refunded in time."),
    (("FINRA-2030(f)(2)(C)(i)", "FINRA-2030(c)(3)(A)(iii)"),
     "Both discuss 'obtaining a return of the contribution,' but FINRA-2030(f)(2)(C)(i) is merely one discretionary factor FINRA considers when deciding whether to grant an exemption from the ban, whereas FINRA-2030(c)(3)(A)(iii) is a mandatory, self-executing 60-day deadline that must be met for the automatic exception to apply."),
    (("FINRA-2030(g)(6)(A)", "FINRA-2030(g)(6)(C)"),
     "Both are sub-parts of the 'government entity' definition and share similar drafting, but FINRA-2030(g)(6)(A) extends the definition to agencies/instrumentalities of a state or subdivision, while FINRA-2030(g)(6)(C) extends it to a 'plan or program' such as a tuition or retirement plan, a substantively different category of entity."),
    (("FINRA-3220(a)", "FINRA-2070(c)"),
     "Both rules use near-identical 'give... anything of value' language and reference dollar-figure limits, but FINRA-3220(a) sets a general $300/year gratuity cap for all outside persons, while FINRA-2070(c) imposes a stricter 'nominal value' standard (overriding the 3220 dollar cap) specifically for FINRA employees with authority over a regulatory matter involving the member."),
    (("FINRA-3220-SM.04", "FINRA-3220-SM.06(a)"),
     "The supplementary materials use identical boilerplate exempting certain gifts from paragraph (a)'s restrictions and paragraph (c)'s recordkeeping duty, but SM.04 exempts personal/life-event gifts (weddings, births) while SM.06(a) exempts de minimis/promotional items (pens, tote bags) - different categories carved out by the same template language."),
    (("FINRA-2080(b)(1)", "FINRA-2080(b)(2)"),
     "Both provisions let a member skip naming FINRA as a party in an expungement proceeding, but FINRA-2080(b)(1) applies automatically upon specific enumerated affirmative findings (claim is false, factually impossible, etc.), while FINRA-2080(b)(2) is a discretionary, 'extraordinary circumstances' waiver for findings that don't fit those categories."),
    (("FINRA-3110(b)(1)", "FINRA-3120(a)(1)"),
     "Both use nearly identical 'reasonably designed to achieve compliance... with applicable FINRA rules' language, but FINRA-3110(b)(1) requires a member to establish and enforce written supervisory procedures generally, while FINRA-3120(a)(1) requires a separately designated principal to test and verify that those very procedures are adequate - a distinct, higher-level control function."),
    (("FINRA-3110(b)(6)(C)(i)", "FINRA-3110(b)(6)(C)(ii)"),
     "Both are parallel prohibitions using the phrase 'procedures prohibiting associated persons who perform a supervisory function from,' but FINRA-3110(b)(6)(C)(i) bars a supervisor from supervising their own activities, while FINRA-3110(b)(6)(C)(ii) bars a supervisor from reporting to or being compensated by the person they supervise - different conflict-of-interest scenarios."),
    (("FINRA-3110(c)(1)(A)", "FINRA-3110(c)(1)(B)"),
     "Both use the same 'shall inspect' inspection-cycle template, but FINRA-3110(c)(1)(A) requires annual inspection of OSJs and branches supervising other locations, while FINRA-3110(c)(1)(B) permits up to a three-year cycle for non-supervisory branch offices."),
    (("FINRA-3110(f)(2)(A)(ii)", "FINRA-3110(f)(2)(A)(v)"),
     "Both are exclusions from the definition of 'branch office' sharing the same introductory clause, but FINRA-3110(f)(2)(A)(ii) exempts an associated person's primary residence subject to nine listed conditions, while FINRA-3110(f)(2)(A)(v) exempts locations used primarily for non-securities activity generating fewer than 25 transactions/year - entirely different qualifying tests."),
    (("FINRA-3130(c)1.(A)", "FINRA-3130(c)1.(C)"),
     "Both use the identical stem 'The Member has in place processes to:' within the CEO certification text, but FINRA-3130(c)1.(A) covers establishing/maintaining/reviewing compliance policies, while FINRA-3130(c)1.(C) covers periodically testing the effectiveness of those same policies - different stages of the compliance lifecycle."),
    (("FINRA-3210-SM.02(a)", "FINRA-3210-SM.02(c)"),
     "Both create a presumption of 'beneficial interest' in an outside account for purposes of Rule 3210, but FINRA-3210-SM.02(a) is a status-based presumption (the associated person's spouse), while FINRA-3210-SM.02(c) is a control-based presumption (any related individual over whose account the person has control) - different triggering facts."),
    (("FINRA-3230(j)(2)(B)", "FINRA-3230(k)(1)(B)"),
     "The identical phrase '15 seconds or four rings' appears in both, but FINRA-3230(j)(2)(B) uses it as a condition of the call-abandonment safe harbor, while FINRA-3230(k)(1)(B) uses the same ring-duration standard as a condition for lawfully delivering a prerecorded message - different compliance regimes triggered by the same numeric standard."),
    (("FINRA-3240(a)(2)(D)", "FINRA-3240(a)(2)(E)"),
     "Both use nearly identical 'bona fide... relationship... maintained outside of, and formed prior to, the broker-customer relationship' language, but FINRA-3240(a)(2)(D) covers close personal relationships while FINRA-3240(a)(2)(E) covers bona fide business relationships - different exception bases to the borrowing/lending ban."),
    (("FINRA-3241(a)(1)(A)", "FINRA-3241(a)(2)(A)"),
     "Both use the identical carve-out phrase 'member of the registered person's immediate family,' but FINRA-3241(a)(1)(A) applies it to the prohibition on being named an estate beneficiary, while FINRA-3241(a)(2)(A) applies the same carve-out to the separate prohibition on serving as executor/trustee/power-of-attorney."),
    (("FINRA-4210(a)(3)", "FINRA-3230(m)(11)"),
     "Both formally define the term 'customer,' but FINRA-4210(a)(3) defines it in the margin-account/credit-extension context (anyone for whom securities are bought/sold/carried), while FINRA-3230(m)(11) defines it in the telemarketing context (anyone who may be required to pay for telemarketed goods or services) - unrelated substantive meanings sharing the same defined term."),
    (("FINRA-4210(a)(9)", "FINRA-4210(a)(10)"),
     "Both use an identical definitional template referencing 'assigned a rating (implicitly or explicitly) in one of the top [X] rating categories,' but FINRA-4210(a)(9) sets the bar at the top two categories ('highly rated foreign sovereign debt'), while FINRA-4210(a)(10) sets it at the top four ('investment grade debt') - a materially different credit-quality threshold."),
    (("FINRA-4210(a)(13)(A)", "FINRA-4210(a)(13)(B)(i)"),
     "Both are alternative branches of the 'exempt account' definition joined by 'or,' but FINRA-4210(a)(13)(A) qualifies accounts by entity type (member, broker-dealer, designated account), while FINRA-4210(a)(13)(B)(i) qualifies any person by a $45 million net worth / $40 million financial assets wealth test - completely different qualifying mechanisms."),
    (("FINRA-4210(a)(16)(A)", "FINRA-4210(a)(16)(B)"),
     "Both use the '$25 million' principal-amount threshold in defining 'other marginable non-equity securities,' but FINRA-4210(a)(16)(A) applies it to debt securities not traded on an exchange, while FINRA-4210(a)(16)(B) applies the identical threshold to private pass-through securities - different security types under a shared dollar test."),
    (("FINRA-4210(c)(2)", "FINRA-4210(c)(3)"),
     "Both use an identical maintenance-margin formula structure ('$X per share or Y percent of current market value, whichever is greater') for stock sold short, but FINRA-4210(c)(2) applies 100%/$2.50 below the $5.00 price break while FINRA-4210(c)(3) applies 30%/$5.00 at or above it."),
    (("FINRA-4210(e)(2)(A)(i)", "FINRA-4210(e)(2)(A)(iii)"),
     "Both use the identical template 'On net long or short positions in obligations... issued or guaranteed... by the United States Government,' but FINRA-4210(e)(2)(A)(i) sets 1% margin for under-one-year maturities while FINRA-4210(e)(2)(A)(iii) sets 3% for three-to-five-year maturities - same sentence structure, different maturity bands and percentages."),
    (("FINRA-4210(e)(2)(C)(i)", "FINRA-4210(e)(2)(C)(ii)"),
     "Both margin 'long' or 'short' positions in non-equity securities under the same introductory clause, but FINRA-4210(e)(2)(C)(i) sets 10% for investment grade debt while FINRA-4210(e)(2)(C)(ii) sets 20% of market value (or 7% of principal) for other listed non-equity securities."),
    (("FINRA-4210(f)(2)(E)(i)", "FINRA-4210(f)(2)(E)(iii)"),
     "Both are rows in the same four-column options margin table ('Type of Option / Initial and/or Maintenance Margin Required / Minimum Margin Required / Underlying Component Value'), but FINRA-4210(f)(2)(E)(i) covers listed puts/calls while FINRA-4210(f)(2)(E)(iii) covers OTC puts/calls, with materially different percentage requirements."),
    (("FINRA-4210(f)(8)(A)(i)", "FINRA-4210(f)(8)(A)(ii)"),
     "Both use the identical FINRA rate-setting authority clause ('FINRA may, whenever it shall determine that market conditions so warrant, prescribe...'), but FINRA-4210(f)(8)(A)(i) empowers FINRA to raise initial margin requirements while FINRA-4210(f)(8)(A)(ii) empowers it to raise maintenance margin requirements - distinct margin concepts."),
    (("FINRA-4210(f)(10)(B)(iii)(1)", "FINRA-4210(f)(10)(B)(iii)(4)"),
     "Both are rows in the security futures offset table sharing the phrase 'security future... and... option on the same underlying security,' but FINRA-4210(f)(10)(B)(iii)(1) covers a long future paired with a long protective put while FINRA-4210(f)(10)(B)(iii)(4) covers a long future paired with a short covered call - opposite option positions with different margin formulas."),
    (("FINRA-4210(f)(10)(B)(iii)(12)", "FINRA-4210(f)(10)(B)(iii)(13)"),
     "Both use near-identical wording ('short security future and long position in... underlying the security future'), but FINRA-4210(f)(10)(B)(iii)(12) sets 5% maintenance margin for a direct long stock offset while FINRA-4210(f)(10)(B)(iii)(13) sets 10% for a convertible-security offset - same template, different offset instrument and margin rate."),
    (("FINRA-4210(g)(9)(A)", "FINRA-4210(g)(10)(A)"),
     "Both use the same 'three business days... new opening orders' cure-period mechanism, but FINRA-4210(g)(9)(A) applies it specifically to a shortfall below the $5 million minimum equity required of Category (C) eligible participants, while FINRA-4210(g)(10)(A) applies the analogous mechanism to a general portfolio margin deficiency for any eligible participant."),
    (("FINRA-4230(a)", "FINRA-4230(b)"),
     "Both concern Regulation T extension-of-time requests submitted to FINRA, but FINRA-4230(a) requires case-by-case pre-submission of individual extension requests for FINRA approval, while FINRA-4230(b) requires a separate monthly aggregate report of a firm's extension-request ratio across all its introduced broker-dealers."),
    (("FINRA-4320(a)", "FINRA-4320(a)(1)"),
     "Both use nearly identical language requiring a firm to 'immediately thereafter close out the fail to deliver position... by purchasing securities of like kind and quantity,' but FINRA-4320(a) sets the general 13-consecutive-settlement-day trigger while FINRA-4320(a)(1) sets a 35-day trigger specifically for securities sold under SEC Rule 144."),
    (("FINRA-2030(g)(11)(A)", "FINRA-2030(g)(11)(B)"),
     "Both use the identical definitional template 'to communicate, directly or indirectly, for the purpose of...' to define 'solicit,' but FINRA-2030(g)(11)(A) defines it in terms of soliciting an investment-advisory client while FINRA-2030(g)(11)(B) defines it in terms of soliciting a political contribution or payment - two distinct meanings of the same defined term within one rule."),
]

set_b = [
    (("FINRA-4240(b)(2)(A)(ii)", "FINRA-4210(b)"),
     "FINRA-4240(b)(2)(A)(ii) explicitly defines the SBS 'Initial Margin Requirement' as 'the margin that Rule 4210 would require to be maintained on the Equivalent Margin Account,' directly incorporating FINRA-4210(b)'s initial margin standard by cross-reference despite using entirely different SBS-specific vocabulary."),
    (("FINRA-4240(b)(1)", "FINRA-4210(a)(5)"),
     "FINRA-4240(b)(1)'s 'Current Exposure' calculation (net SBS value plus margin collected minus margin delivered) performs the same net-exposure function as FINRA-4210(a)(5)'s 'Equity' definition (long value plus credit balance minus short value minus debit balance), despite using unrelated terminology for margin accounts versus SBS accounts."),
    (("FINRA-4240(c)", "FINRA-4210(g)(1)"),
     "FINRA-4240(c) requires SBS members to 'monitor the risk of any Uncleared SBS Accounts and... maintain a comprehensive written risk analysis methodology for assessing the potential risk to the member's capital over a specified range of possible market movements,' which is nearly verbatim the same risk-monitoring mandate imposed on portfolio margin accounts under FINRA-4210(g)(1)."),
    (("FINRA-3240(c)", "FINRA-3241(c)"),
     "The definition of 'immediate family' (parents, grandparents, in-laws, spouse/domestic partner, siblings, children, etc.) is copied essentially verbatim into two unrelated rules - the borrowing/lending restrictions of FINRA-3240(c) and the beneficiary/executor restrictions of FINRA-3241(c) - serving the identical conflict-of-interest carve-out purpose in different contexts."),
    (("FINRA-2090-intro", "FINRA-4330(b)(2)(A)"),
     "FINRA-2090-intro's 'reasonable diligence... to know... the essential facts concerning every customer' KYC duty is echoed almost word-for-word in FINRA-4330(b)(2)(A)'s requirement that a member have 'reasonable grounds for believing that the customer's loan(s) of securities are appropriate,' extending the same diligence concept with additional securities-lending-specific factors."),
    (("FINRA-3110(a)(4)", "FINRA-3110-SM.03"),
     "FINRA-3110(a)(4) requires designation of a registered principal in each OSJ, and FINRA-3110-SM.03 restates and substantially extends that requirement by introducing the 'on-site principal' concept and a physical-presence standard not found in the rule text itself."),
    (("FINRA-3110(a)(7)", "FINRA-3110-SM.04"),
     "FINRA-3110(a)(7)'s bare requirement of an annual 'interview or meeting' on compliance matters is given concrete operational meaning by FINRA-3110-SM.04, which enumerates acceptable formats (on-demand webcast, video conference, telephone) and specific completion-tracking requirements."),
    (("FINRA-3110(b)(2)", "FINRA-3110-SM.05"),
     "FINRA-3110(b)(2)'s requirement that a principal review 'all transactions... evidenced in writing' is restated by FINRA-3110-SM.05 as satisfiable through a 'reasonably designed risk-based review system,' meaning firms need not review every transaction individually - the same underlying review duty, differently operationalized."),
    (("FINRA-3110(b)(4)(A)", "FINRA-3110-SM.06(a)"),
     "FINRA-3110(b)(4)(A)'s correspondence-review mandate is elaborated by FINRA-3110-SM.06(a), which permits 'risk-based principles' to govern review of correspondence types falling outside the specific subject matters enumerated in the rule - same duty, expanded flexibility."),
    (("FINRA-3110(b)(4)(B)", "FINRA-3110-SM.07"),
     "FINRA-3110(b)(4)(B)'s requirement that reviews be 'evidenced in writing' is given substantive content by FINRA-3110-SM.07, which specifies that evidence must identify the reviewer, item reviewed, date, and actions taken, and clarifies that 'merely opening a communication is not sufficient review.'"),
    (("FINRA-3130(b)", "FINRA-3130-SM.04"),
     "FINRA-3130(b)'s requirement that the CEO and CCO 'conduct... meetings... to discuss such processes' is broken down by FINRA-3130-SM.04 into three specific discussion topics (review the certification's subject matter, review compliance efforts, and identify emerging compliance problems)."),
    (("FINRA-3120(a)(1)", "FINRA-3130-SM.03"),
     "FINRA-3120(a)(1)'s mandate that a designated principal 'test and verify' supervisory procedures is echoed in FINRA-3130-SM.03's description of the compliance process as one requiring firms to 'establish, maintain, review, test and modify' those same procedures - overlapping quality-assurance functions described in different rules."),
    (("FINRA-3220-SM.02", "FINRA-3220(a)"),
     "FINRA-3220(a)'s $300 gift cap is operationally meaningless without FINRA-3220-SM.02's valuation methodology, which specifies that gifts are valued at cost (excluding tax/delivery) except for event tickets, valued at the higher of cost or face value."),
    (("FINRA-3220-SM.03", "FINRA-3220(a)"),
     "FINRA-3220(a)'s per-recipient $300 cap is enforced in practice through FINRA-3220-SM.03's aggregation rule, which requires firms to combine all gifts from the member and its associated persons to a single recipient over a chosen accounting period."),
    (("FINRA-3220(c)", "FINRA-3220-SM.08"),
     "FINRA-3220(c)'s bare instruction to keep 'a separate record of all payments or gratuities' is expanded by FINRA-3220-SM.08 into a full supervisory framework requiring that gifts be reported, reviewed for compliance, and maintained in records, tied back to the general supervisory system required by Rule 3110."),
    (("FINRA-3230(a)(1)(A)", "FINRA-3230(m)(12)(A)"),
     "The 'established business relationship' exception invoked in FINRA-3230(a)(1)(A) has no independent meaning without the detailed 18-month/3-month relationship definition supplied separately in FINRA-3230(m)(12)(A) - the operative exception and its defining test live in different subsections."),
    (("FINRA-3230(a)(2)", "FINRA-3230(d)(3)"),
     "FINRA-3230(a)(2)'s prohibition on calling anyone on the firm's do-not-call list is given practical effect only through FINRA-3230(d)(3)'s companion duty to record such requests and honor them within 30 days - the prohibition and its implementing mechanism are stated separately."),
    (("FINRA-3230(g)(1)", "FINRA-3230(g)(3)"),
     "FINRA-3230(g)(1)'s affirmative duty to transmit caller-ID information and FINRA-3230(g)(3)'s prohibition on blocking that same information are two different sentences achieving the identical substantive outcome: ensuring the called party can identify the caller."),
    (("FINRA-3160(a)(3)(A)", "FINRA-3160(a)(4)(B)"),
     "The three written disclosures required at account opening under FINRA-3160(a)(3)(A) (not FDIC insured, not a bank obligation/guarantee, subject to investment risk) are the same substantive content compressed into the standardized advertising legend permitted under FINRA-3160(a)(4)(B) ('Not FDIC Insured, No Bank Guarantee, May Lose Value')."),
    (("FINRA-4360(b)(1)", "FINRA-4360(d)(2)"),
     "FINRA-4360(b)(1)'s static net-capital-to-minimum-coverage table is made dynamic by FINRA-4360(d)(2), which specifies that a firm's highest net capital requirement over the preceding 12 months (not just its current requirement) determines which coverage tier applies."),
    (("FINRA-4360(a)(3)", "FINRA-4360-SM.02"),
     "FINRA-4360(a)(3)'s blanket fidelity bond 'per loss coverage' mandate is restated for firms that cannot obtain such coverage by FINRA-4360-SM.02, which requires 'substantially similar' alternative coverage upon documented proof from two insurers that blanket coverage is unavailable."),
    (("FINRA-4311(b)(3)", "FINRA-4311-SM.02"),
     "FINRA-4311(b)(3)'s 10-business-day advance notice duty for new introducing-firm relationships is operationalized by FINRA-4311-SM.02, which specifies that notice must take the form of a particular FINRA-prescribed questionnaire."),
    (("FINRA-4311(b)(4)", "FINRA-4311-SM.03"),
     "FINRA-4311(b)(4)'s general 'due diligence' duty regarding new introducing firms is given concrete content by FINRA-4311-SM.03's illustrative list (business model, product mix, FOCUS reports, audited financials, complaint/disciplinary history)."),
    (("FINRA-4311(c)(2)", "FINRA-4311-SM.04"),
     "FINRA-4311(c)(2)'s allocation of fund-safeguarding responsibility to the carrying firm is restated by FINRA-4311-SM.04 as a reminder that such safeguarding must independently comply with SEC Rule 15c3-3 and related SEC guidance - the same substantive obligation cross-referenced to its source rule."),
    (("FINRA-2040(a)(1)", "FINRA-2040-SM.01"),
     "FINRA-2040(a)(1)'s prohibition on paying compensation to unregistered persons who should be broker-dealers is operationalized by FINRA-2040-SM.01, which describes concrete methods (no-action letters, legal opinions from licensed counsel) a firm can use to reasonably support its determination that registration isn't required."),
    (("FINRA-3210(a)", "FINRA-3210-SM.04"),
     "FINRA-3210(a)'s written-consent requirement for outside accounts is given substantive content for accounts at non-member institutions by FINRA-3210-SM.04, which ties the consent decision to whether the firm can obtain duplicate confirmations/statements from that institution."),
    (("FINRA-3210(a)", "FINRA-3210-SM.01"),
     "FINRA-3210(a)'s prior-consent requirement is restated by FINRA-3210-SM.01 for the specific scenario of an account opened before the person's association with the employer member, imposing the same substantive consent obligation with a 30-day compliance window."),
    (("FINRA-4370(a)", "FINRA-4370(c)(7)"),
     "FINRA-4370(a)'s general instruction that a business continuity plan address the firm's 'existing relationships with other broker-dealers and counter-parties' is restated as the specific checklist item 'Critical business constituent, bank, and counter-party impact' in FINRA-4370(c)(7)."),
    (("FINRA-4370(g)(1)", "FINRA-4370(c)(2)"),
     "FINRA-4370(g)(1)'s detailed definition of 'mission critical system' (order taking, execution, comparison, clearance, settlement, customer account access, etc.) is the same concept referenced tersely as a required plan element, 'All mission critical systems,' in FINRA-4370(c)(2)."),
    (("FINRA-3170(a)(4)", "FINRA-3170(b)(3)"),
     "FINRA-3170(a)(4)'s definition of 'tape recording' (including electronic/digital recordings) exists solely to give operative meaning to the substantive duty in FINRA-3170(b)(3) requiring taping firms to 'tape record all telephone conversations... and for reviewing the tape recordings' - definition and operative duty split across subsections."),
]

def combine_pairs(pairs = [set_a, set_b]):
    combined_pairs = {"lexically_similar_substantively_different": [], "lexically_different_substantively_similar": []}
    for (pair, reason) in pairs[0]:
        obj = combined_pairs["lexically_similar_substantively_different"].append({"clause_pair": tuple(pair), "reason": reason})
    for (pair, reason) in pairs[1]:
        obj = combined_pairs["lexically_different_substantively_similar"].append({"clause_pair": tuple(pair), "reason": reason})

    return combined_pairs

# -----------------------------------------------------------------------
# STEP 1: Load embeddings from file into a clause_ref -> embedding lookup
# -----------------------------------------------------------------------
def load_embeddings(file_path):
    """
    Loads embeddings from either .json (single array) or .jsonl (one object
    per line) files. Returns a dict: {clause_ref: np.array(embedding)}
    """
    file_path = Path(file_path)
    records = []

    if file_path.suffix == ".jsonl":
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    elif file_path.suffix == ".json":
        with open(file_path, "r") as f:
            data = json.load(f)
            # Handle both a top-level list, or a dict with a list under some key
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict):
                # fallback: find the first list value in the dict
                for v in data.values():
                    if isinstance(v, list):
                        records = v
                        break
    else:
        raise ValueError(f"Unsupported file extension: {file_path.suffix}")

    lookup = {}
    for r in records:
        clause_ref = r["clause_ref"]
        embedding = np.array(r["embedding"], dtype=np.float64)
        lookup[clause_ref] = embedding

    return lookup


# -----------------------------------------------------------------------
# STEP 2: Cosine similarity
# -----------------------------------------------------------------------
def cosine_similarity(vec1, vec2):
    dot = np.dot(vec1, vec2)
    norm1 = np.linalg.norm(vec1)
    norm2 = np.linalg.norm(vec2)
    if norm1 == 0 or norm2 == 0:
        return None
    return dot / (norm1 * norm2)


# -----------------------------------------------------------------------
# STEP 3: Compute pairwise similarities for one category of pairs
# -----------------------------------------------------------------------
def compute_pair_similarities(pairs_list, embedding_lookup, category_name):
    """
    pairs_list: list of dicts like {"clause_pair": (ref1, ref2), "reason": "..."}
    embedding_lookup: dict from load_embeddings()

    Returns a list of dicts with similarity scores added, and flags any
    clause_ref not found in the embedding file.
    """
    results = []

    for item in pairs_list:
        ref1, ref2 = item["clause_pair"]
        reason = item.get("reason", "")

        emb1 = embedding_lookup.get(ref1)
        emb2 = embedding_lookup.get(ref2)

        missing = []
        if emb1 is None:
            missing.append(ref1)
        if emb2 is None:
            missing.append(ref2)

        if missing:
            results.append({
                "clause_pair": (ref1, ref2),
                "reason": reason,
                "category": category_name,
                "cosine_similarity": None,
                "error": f"Missing embedding(s) for: {missing}",
            })
            continue

        sim = cosine_similarity(emb1, emb2)
        results.append({
            "clause_pair": (ref1, ref2),
            "reason": reason,
            "category": category_name,
            "cosine_similarity": round(float(sim), 4),
            "error": None,
        })

    return results


# -----------------------------------------------------------------------
# STEP 4: Aggregate stats for a category's similarity results
# -----------------------------------------------------------------------
def aggregate_category_stats(pair_results):
    valid_sims = [r["cosine_similarity"] for r in pair_results if r["cosine_similarity"] is not None]

    if not valid_sims:
        return {
            "num_pairs": len(pair_results),
            "num_valid": 0,
            "num_missing": len(pair_results),
            "mean_similarity": None,
            "median_similarity": None,
            "std_similarity": None,
            "min_similarity": None,
            "max_similarity": None,
        }

    return {
        "num_pairs": len(pair_results),
        "num_valid": len(valid_sims),
        "num_missing": len(pair_results) - len(valid_sims),
        "mean_similarity": round(float(np.mean(valid_sims)), 4),
        "median_similarity": round(float(np.median(valid_sims)), 4),
        "std_similarity": round(float(np.std(valid_sims)), 4),
        "min_similarity": round(float(np.min(valid_sims)), 4),
        "max_similarity": round(float(np.max(valid_sims)), 4),
    }


# -----------------------------------------------------------------------
# STEP 5: Full evaluation for ONE model
# -----------------------------------------------------------------------
def evaluate_discrimination_test(embedding_file_path, pairs_dict):
    """
    pairs_dict: {
        "lexically_similar_substantively_different": [...],
        "lexically_different_substantively_similar": [...]
    }

    Returns a dict with per-pair results, per-category aggregates,
    and a separation margin summary.
    """
    embedding_lookup = load_embeddings(embedding_file_path)

    category_results = {}
    category_aggregates = {}

    for category_name, pairs_list in pairs_dict.items():
        pair_results = compute_pair_similarities(pairs_list, embedding_lookup, category_name)
        category_results[category_name] = pair_results
        category_aggregates[category_name] = aggregate_category_stats(pair_results)

    # Separation margin: how much higher is "different-wording-same-meaning"
    # similarity compared to "same-wording-different-meaning" similarity.
    # A well-discriminating embedding should have a LARGE positive margin.
    mean_lex_sim_subst_diff = category_aggregates.get(
        "lexically_similar_substantively_different", {}
    ).get("mean_similarity")
    mean_lex_diff_subst_sim = category_aggregates.get(
        "lexically_different_substantively_similar", {}
    ).get("mean_similarity")

    separation_margin = None
    if mean_lex_sim_subst_diff is not None and mean_lex_diff_subst_sim is not None:
        separation_margin = round(mean_lex_diff_subst_sim - mean_lex_sim_subst_diff, 4)

    return {
        "embedding_file": str(embedding_file_path),
        "per_pair_results": category_results,
        "category_aggregates": category_aggregates,
        "separation_margin": separation_margin,
    }


# -----------------------------------------------------------------------
# STEP 6: Run across all models and compile a comparison table
# -----------------------------------------------------------------------
def compare_models_discrimination(model_files, pairs_dict):
    """
    model_files: dict of {model_name: embedding_file_path}

    Returns:
      - full_results: dict of {model_name: evaluate_discrimination_test output}
      - comparison_df: pandas DataFrame summarizing key numbers side by side
    """
    import pandas as pd

    full_results = {}
    comparison_rows = []

    for model_name, file_path in model_files.items():
        print(f"Evaluating: {model_name} ...")
        result = evaluate_discrimination_test(file_path, pairs_dict)
        full_results[model_name] = result

        lex_sim_agg = result["category_aggregates"].get(
            "lexically_similar_substantively_different", {}
        )
        lex_diff_agg = result["category_aggregates"].get(
            "lexically_different_substantively_similar", {}
        )

        comparison_rows.append({
            "model": model_name,
            "mean_sim_lexSimilar_substDifferent (want LOW)": lex_sim_agg.get("mean_similarity"),
            "mean_sim_lexDifferent_substSimilar (want HIGH)": lex_diff_agg.get("mean_similarity"),
            "separation_margin (want HIGH)": result["separation_margin"],
            "num_missing_lexSimilar": lex_sim_agg.get("num_missing"),
            "num_missing_lexDifferent": lex_diff_agg.get("num_missing"),
        })

    comparison_df = pd.DataFrame(comparison_rows).sort_values(
        "separation_margin (want HIGH)", ascending=False
    )

    return full_results, comparison_df


# -----------------------------------------------------------------------
# USAGE
# -----------------------------------------------------------------------
BASE_DIR = "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/data/embedded_clauses"

model_files = {
    "text-embedding-3-small": f"{BASE_DIR}/finra_clauses_embedded__text-embedding-3-small.jsonl",
    "Euler-Legal-Embedding-V1": f"{BASE_DIR}/finra_clauses_embedded__Mira190__Euler-Legal-Embedding-V1.jsonl",
    "Octen-Embedding-8B": f"{BASE_DIR}/finra_clauses_embedded__Octen__Octen-Embedding-8B.jsonl",
    "voyage-law-2": f"{BASE_DIR}/finra_clauses_embedded__voyage-law-2.jsonl",
}

pairs_dict = combine_pairs([set_a, set_b])

full_results, comparison_df = compare_models_discrimination(model_files, pairs_dict)

OUTPUT_DIR = Path("/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/eval_results/embedding")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Save full per-pair + aggregate results (all models) as JSON
with open(OUTPUT_DIR / "discrimination_test_full_results.json", "w") as f:
    json.dump(full_results, f, indent=2, default=str)

# Save comparison table as both CSV and JSON
# comparison_df.to_csv(OUTPUT_DIR / "discrimination_test_comparison.csv", index=False)
comparison_df.to_json(OUTPUT_DIR / "discrimination_test_comparison.json", orient="records", indent=2)