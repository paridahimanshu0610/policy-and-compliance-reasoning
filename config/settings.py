"""
config/settings.py
==================
Central configuration for the FINRA Compliance Reasoning System.

All paths are resolved relative to the project root so the project
is portable across machines. The only values that need changing when
moving to a new machine are MODEL_CONFIGS if the model files live in
a different location.
"""

from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv(override=True)

# ── Project root ──────────────────────────────────────────────────────────────
# Resolved from this file's location: config/ is one level below root
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Data paths ────────────────────────────────────────────────────────────────
DATA_DIR              = BASE_DIR / "data"
CHROMA_PATH           = DATA_DIR / "chromadb"
PARSED_CHECKPOINT     = DATA_DIR / "parsed_rules.json"
NORMALIZED_CHECKPOINT = DATA_DIR / "aggregate_normalized_clauses.jsonl"
HTML_DIR = DATA_DIR / "FINRA_Rules"

# ── ChromaDB ──────────────────────────────────────────────────────────────────
COLLECTION_NAME = "finra_clauses"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
BATCH_SIZE      = 50        # documents per ChromaDB write call

# ── Model paths ───────────────────────────────────────────────────────────────
MODELS_DIR = BASE_DIR / "models"

MODEL_CONFIGS = {
    "qwen": {
        "path": str(
            MODELS_DIR / "qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf"
        ),
        # Practical context limit — leave headroom below max_position_embeddings
        # Qwen max_position_embeddings = 32768; use 28000 to stay safe
        "max_context_tokens": 28000,
        "n_ctx": 8192,
    },
    "llama": {
        "path": str(
            MODELS_DIR / "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"
        ),
        # Llama max_position_embeddings = 131072
        # 8B quality degrades well before the hard limit — use 80000
        "max_context_tokens": 80000,
        "n_ctx": 16384,
    },
}

DEFAULT_MODEL = "llama"

# ── Inference Backend ─────────────────────────────────────────────────────────
# Set to "local" or "tamu"
INFERENCE_BACKEND = "tamu"

TAMU_CONFIG = {
    "api_key":   os.getenv("TAMUS_AI_CHAT_API_KEY"),
    "base_url":  os.getenv("TAMUS_AI_CHAT_API_ENDPOINT"),
    "model":     "protected.gpt-5-nano",
}

LLM_MODELS = {
    "o3":          {**TAMU_CONFIG, "model": "protected.o3", "supports_temperature": False},
    "claude_opus": {**TAMU_CONFIG, "model": "protected.Claude Opus 4.7", "supports_temperature": True},
    "gpt5":        {**TAMU_CONFIG, "model": "protected.gpt-5", "supports_temperature": True},
    "gemini":      {**TAMU_CONFIG, "model": "protected.gemini-2.5-pro", "supports_temperature": True},
}

# Which model each agent "role" uses. The role names match the node/agent
# that calls get_chat_model(role) in agent/llm.py. Change the value on the
# right to swap models for that role without touching any node code.
#
#   intake       -- turns the user's plain-language message into normalized
#                   fields (does not need the strongest model; cheap + frequent)
#   ambiguity    -- decides if the query is genuinely ambiguous
#   clarify      -- phrases the one clarifying question to ask
#   reasoner     -- the deep agent: assigns clause roles, checks conflicts,
#                   decides scope, writes the final answer (wants the strongest
#                   reasoning model since mistakes here are the costly ones)
#   scope_guard  -- runs on every turn before anything else: decides whether
#                   the message is in-scope for FINRA compliance help at all,
#                   and whether the user is explicitly asking for a human agent
ACTIVE_LLM = {
    "intake":      "o3",
    "ambiguity":   "o3",
    "clarify":     "o3",
    "reasoner":    "o3",
    "scope_guard": "o3",
}

# ---------------------------------------------------------------------------
# Embedding / vector DB configuration
# ---------------------------------------------------------------------------
 
# Maps each embedding model name to the Qdrant collection that was built
# with it. Must match ingestion/build_vector_db.py's COLLECTION_NAME comment
# block exactly.
EMBEDDING_MODELS = {
    "voyage-law-2":                     "voyage_embedded_clauses_new",
    "Mira190/Euler-Legal-Embedding-V1":  "euler_embedded_clauses",
    "text-embedding-3-small":           "text_embedded_clauses",
    "Octen/Octen-Embedding-8B":         "octen_embedded_clauses",
    "Qwen/Qwen3-Embedding-8B":          "qwen_embedded_clauses",
}
 
# The one embedding model / collection the agent actually queries against
# right now. Change this single line to switch which vector database the
# whole agent uses -- every retrieval call reads these two values from here.
ACTIVE_EMBEDDING_MODEL = "voyage-law-2"
ACTIVE_COLLECTION_NAME = EMBEDDING_MODELS[ACTIVE_EMBEDDING_MODEL]
 
# How many candidate clauses to pull back per vector search call.
RETRIEVAL_TOP_K = 10
 
# Safety cap: max clarifying questions asked before the agent gives its best
# answer anyway (with caveats), so a confused user never gets stuck in a loop.
MAX_CLARIFICATION_TURNS = 5
 
# Safety cap: max retrieve -> reason cycles within a single turn, in case the
# reasoner keeps asking for "just one more search".
MAX_REASONING_CYCLES = 5

# ---------------------------------------------------------------------------
# Human-in-the-loop escalation / compliance-agent handoff
# ---------------------------------------------------------------------------

# Where the summarized situation + the user's contact details get emailed
# when a human handoff is triggered (either the user asked directly, or one
# of the safety caps above was exceeded). Override via .env in production.
EMAIL_ID = os.getenv("COMPLIANCE_TEAM_EMAIL")

# SMTP settings for actually sending that email. All read from the
# environment -- nothing here should be a real credential in source control.
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").strip().lower() in ("1", "true", "yes")
# Falls back to SMTP_USERNAME since most providers require the From address
# to match the authenticated account.
SMTP_FROM_ADDRESS = os.getenv("SMTP_FROM_ADDRESS", SMTP_USERNAME)


SERIES_MAP = {
    "2010": "2000", "2020": "2000", "2030": "2000", "2040": "2000",
    "2060": "2000", "2070": "2000", "2080": "2000", "2081": "2000",
    "2090": "2000",
    
    "3110": "3000", "3120": "3000", "3130": "3000", "3150": "3000",
    "3160": "3000", "3170": "3000", "3210": "3000", "3220": "3000",
    "3230": "3000", "3240": "3000", "3241": "3000", "3250": "3000",
    "3260": "3000", "3270": "3000", "3280": "3000", "3310": "3000",

    "4210": "4000", "4220": "4000", "4230": "4000", "4240": "4000",
    "4311": "4000", "4314": "4000", "4320": "4000", "4330": "4000",
    "4340": "4000", "4360": "4000", "4370": "4000", "4380": "4000",
}

# ── FINRA scraper ─────────────────────────────────────────────────────────────
TARGET_RULES = [
    # ============================================================
    # DUTIES AND CONFLICTS (2000 series)
    # Justification: I included this to test semantic ambiguity, proving the embedding model can handle abstract, subjective legal concepts like 'suitability' rather than relying on exact keyword matches. 
    # ============================================================
    {
        "rule_id":  "2010",
        "name":     "Standards of Commercial Honor and Principles of Trade",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2010",
    },
    {
        "rule_id":  "2020",
        "name":     "Use of Manipulative, Deceptive or Other Fraudulent Devices",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2020",
    },
    {
        "rule_id":  "2030",
        "name":     "Engaging in Distribution and Solicitation Activities with Government Entities",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2030",
    },
    {
        "rule_id":  "2040",
        "name":     "Payments to Unregistered Persons",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2040",
    },
    {
        "rule_id":  "2060",
        "name":     "Use of Information Obtained in Fiduciary Capacity",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2060",
    },
    {
        "rule_id":  "2070",
        "name":     "Transactions Involving FINRA Employees",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2070",
    },
    {
        "rule_id":  "2080",
        "name":     "Obtaining an Order of Expungement of Customer Dispute Information from the Central Registration Depository (CRD) System",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2080",
    },
    {
        "rule_id":  "2081",
        "name":     "Prohibited Conditions Relating to Expungement of Customer Dispute",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2081",
    },
    {
        "rule_id":  "2090",
        "name":     "Know Your Customer",
        "category": "duties_and_conflicts",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2090",
    },

    # ============================================================
    # SUPERVISION AND RESPONSIBILITIES RELATING TO ASSOCIATED PERSONS (3000 series)
    # Justification: I included this to test relational logic, validating that the metadata pipeline can accurately extract and filter based on hierarchical roles, actors, and procedural workflows. 
    # ============================================================
    # --- Supervision ---
    {
        "rule_id":  "3110",
        "name":     "Supervision",
        "category": "supervision",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3110",
    },
    {
        "rule_id":  "3120",
        "name":     "Supervisory Control System",
        "category": "supervision",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3120",
    },
    {
        "rule_id":  "3130",
        "name":     "Annual Certification of Compliance and Supervisory Processes",
        "category": "supervision",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3130",
    },
    {
        "rule_id":  "3150",
        "name":     "Holding of Customer Mail",
        "category": "supervision",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3150",
    },
    {
        "rule_id":  "3160",
        "name":     "Networking Arrangements Between Members and Financial Institutions",
        "category": "supervision",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3160",
    },
    {
        "rule_id":  "3170",
        "name":     "Tape Recording of Registered Persons by Certain Firms",
        "category": "supervision",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3170",
    },

    # --- Responsibilities Relating to Associated Persons ---
    {
        "rule_id":  "3210",
        "name":     "Accounts At Other Broker-Dealers and Financial Institutions",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3210",
    },
    {
        "rule_id":  "3220",
        "name":     "Influencing or Rewarding Employees of Others",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3220",
    },
    {
        "rule_id":  "3230",
        "name":     "Telemarketing",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3230",
    },
    {
        "rule_id":  "3240",
        "name":     "Borrowing From or Lending to Customers",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3240",
    },
    {
        "rule_id":  "3241",
        "name":     "Registered Person Being Named a Customer's Beneficiary or Holding a Position of Trust for a Customer",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3241",
    },
    {
        "rule_id":  "3250",
        "name":     "Designation of Accounts",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3250",
    },
    {
        "rule_id":  "3260",
        "name":     "Discretionary Accounts",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3260",
    },
    {
        "rule_id":  "3270",
        "name":     "Outside Business Activities of Registered Persons",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3270",
    },
    {
        "rule_id":  "3280",
        "name":     "Private Securities Transactions of an Associated Person",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3280",
    },

    # --- Anti-Money Laundering ---
    {
        "rule_id":  "3310",
        "name":     "Anti-Money Laundering Compliance Program",
        "category": "anti_money_laundering",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3310",
    },

    # ============================================================
    # FINANCIAL AND OPERATIONAL RULES (4000 series)
    # Justification: I included this to test quantitative rigidity, ensuring the architecture can strictly enforce mathematical thresholds and percentages without blurring them in the vector space.
    # ============================================================
    # --- Margin ---
    {
        "rule_id":  "4210",
        "name":     "Margin Requirements",
        "category": "margin",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210",
    },
    {
        "rule_id":  "4220",
        "name":     "Daily Record of Required Margin",
        "category": "margin",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4220",
    },
    {
        "rule_id":  "4230",
        "name":     "Required Submissions for Requests for Extensions of Time Under Regulation T and SEA Rule 15c3-3",
        "category": "margin",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4230",
    },
    {
        "rule_id":  "4240",
        "name":     "Security-Based Swap Margin Requirements",
        "category": "margin",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4240",
    },

    # --- Operations ---
    {
        "rule_id":  "4311",
        "name":     "Carrying Agreements",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4311",
    },
    {
        "rule_id":  "4314",
        "name":     "Securities Loans and Borrowings",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4314",
    },
    {
        "rule_id":  "4320",
        "name":     "Short Sale Delivery Requirements",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4320",
    },
    {
        "rule_id":  "4330",
        "name":     "Customer Protection — Permissible Use of Customers' Securities",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4330",
    },
    {
        "rule_id":  "4340",
        "name":     "Callable Securities",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4340",
    },
    {
        "rule_id":  "4360",
        "name":     "Fidelity Bonds",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4360",
    },
    {
        "rule_id":  "4370",
        "name":     "Business Continuity Plans and Emergency Contact Information",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4370",
    },
    {
        "rule_id":  "4380",
        "name":     "Mandatory Participation in FINRA BC/DR Testing Under Regulation SCI",
        "category": "operations",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4380",
    },
]

SCRAPER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.finra.org/rules-guidance/rulebooks/finra-rules",
}