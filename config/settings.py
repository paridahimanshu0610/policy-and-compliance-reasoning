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

# ── Project root ──────────────────────────────────────────────────────────────
# Resolved from this file's location: config/ is one level below root
BASE_DIR = Path(__file__).resolve().parent.parent

# ── Data paths ────────────────────────────────────────────────────────────────
DATA_DIR              = BASE_DIR / "data"
CHROMA_PATH           = DATA_DIR / "chromadb"
PARSED_CHECKPOINT     = DATA_DIR / "parsed_rules.json"
NORMALIZED_CHECKPOINT = DATA_DIR / "normalized_documents.jsonl"

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

# ── Retrieval ─────────────────────────────────────────────────────────────────
DEFAULT_TOP_K = 5

# ── Clarification pipeline ────────────────────────────────────────────────────
MAX_CLARIFY_QUESTIONS = 10   # one per field at most

# ── Context window management (web server) ────────────────────────────────────
# Warn user when context remaining drops below this percentage
CONTEXT_WARN_THRESHOLD = 20
# Hard-disable follow-up input below this percentage
CONTEXT_HARD_LIMIT_PCT = 5

# Truncate clauses at a shorter limit than the main reasoning prompt
# because the follow-up prompt also includes the initial analysis,
# making the context budget tighter.
MAX_CLAUSE_CHARS = 1000
# Truncate initial reasoning to prevent the context budget from being
# dominated by the previous answer on long reasoning outputs.
MAX_REASONING_CHARS = 5000

# ── FINRA scraper ─────────────────────────────────────────────────────────────
TARGET_RULES = [
    # ── Supervisory Responsibilities ──────────────────────────────────────
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
    # {
    #     "rule_id":  "3150",
    #     "name":     "Holding of Customer Mail",
    #     "category": "customer_communication",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3150",
    # },
    # {
    #     "rule_id":  "3160",
    #     "name":     "Networking Arrangements Between Members and Financial Institutions",
    #     "category": "customer_communication",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3160",
    # },
    # {
    #     "rule_id":  "3170",
    #     "name":     "Tape Recording of Registered Persons by Certain Firms",
    #     "category": "customer_communication",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3170",
    # },

    # ── Responsibilities Relating to Associated Persons ───────────────────
    # {
    #     "rule_id":  "3210",
    #     "name":     "Accounts At Other Broker-Dealers and Financial Institutions",
    #     "category": "associated_person_conduct",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3210",
    # },
    # {
    #     "rule_id":  "3220",
    #     "name":     "Influencing or Rewarding Employees of Others",
    #     "category": "associated_person_conduct",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3220",
    # },
    # {
    #     "rule_id":  "3230",
    #     "name":     "Telemarketing",
    #     "category": "telemarketing",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3230",
    # },
    {
        "rule_id":  "3240",
        "name":     "Borrowing From or Lending to Customers",
        "category": "associated_person_conduct",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3240",
    },
    # {
    #     "rule_id":  "3241",
    #     "name":     "Registered Person Being Named a Customer's Beneficiary or Holding a Position of Trust for a Customer",
    #     "category": "associated_person_conduct",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3241",
    # },
    # {
    #     "rule_id":  "3250",
    #     "name":     "Designation of Accounts",
    #     "category": "account_management",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3250",
    # },
    # {
    #     "rule_id":  "3260",
    #     "name":     "Discretionary Accounts",
    #     "category": "account_management",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3260",
    # },
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
    # ── Anti-Money Laundering ─────────────────────────────────────────────
    # {
    #     "rule_id":  "3310",
    #     "name":     "Anti-Money Laundering Compliance Program",
    #     "category": "AML",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/3310",
    # },

    # ── Books and Records ─────────────────────────────────────────────────
    {
        "rule_id":  "4511",
        "name":     "General Requirements for Books and Records",
        "category": "books_and_records",
        "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4511",
    },
    # {
    #     "rule_id":  "4512",
    #     "name":     "Customer Account Information",
    #     "category": "books_and_records",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4512",
    # },
    # {
    #     "rule_id":  "4513",
    #     "name":     "Records of Written Customer Complaints",
    #     "category": "books_and_records",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4513",
    # },
    # {
    #     "rule_id":  "4514",
    #     "name":     "Authorization Records for Negotiable Instruments Drawn From a Customer's Account",
    #     "category": "books_and_records",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4514",
    # },
    # {
    #     "rule_id":  "4515",
    #     "name":     "Approval and Documentation of Changes in Account Name or Designation",
    #     "category": "books_and_records",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4515",
    # },
    # {
    #     "rule_id":  "4517",
    #     "name":     "Member Filing and Contact Information Requirements",
    #     "category": "books_and_records",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4517",
    # },
    # {
    #     "rule_id":  "4518",
    #     "name":     "Notification to FINRA in Connection with the JOBS Act",
    #     "category": "books_and_records",
    #     "url":      "https://www.finra.org/rules-guidance/rulebooks/finra-rules/4518",
    # },
]

SCRAPER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}