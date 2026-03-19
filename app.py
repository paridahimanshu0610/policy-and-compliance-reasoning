"""
app.py
======
Step 8 — Main chatbot application loop.

This is the entry point for the FINRA Compliance Reasoning System.
It wires together all pipeline components into a single interactive
compliance assistant:

    User question
        │
        ▼
    Clarification agent  (intent_pipeline.py)
    asks up to 5 targeted questions
        │
        ▼
    Situation summary + Structured intent JSON
        │
        ▼
    ChromaDB retrieval  (retrieval.py)
    metadata filter + semantic search
        │
        ▼
    Compliance reasoning  (compliance_reasoning.py)
    LLM reasons over retrieved clauses
        │
        ▼
    Structured compliance answer displayed to user

A single model instance is loaded at startup and shared across both the
intent pipeline and the compliance reasoning step. This avoids the
significant RAM and load-time cost of loading two separate model instances.

Usage:
    # Run with qwen (default)
    python app.py

    # Run with llama
    python app.py --model llama

    # Adjust number of retrieval results
    python app.py --top-k 8

Dependencies:
    All pipeline modules must be on the Python path:
        intent_pipeline.py
        retrieval.py
        compliance_reasoning.py
        finra_scraper.py  (imported transitively by build pipeline)

    ChromaDB knowledge base must be built first:
        python build_knowledge_base.py
"""

import argparse
import textwrap

from llama_cpp import Llama

from pipeline.intent_pipeline       import run_intent_pipeline
from pipeline.retrieval             import load_collection, retrieve_clauses
from pipeline.compliance_reasoning  import load_reasoning_model, run_compliance_reasoning
from config.settings import MODEL_CONFIGS, DEFAULT_TOP_K, MAX_CLARIFY_QUESTIONS

# ── Configuration ─────────────────────────────────────────────────────────────

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

# DEFAULT_TOP_K         = 5
# MAX_CLARIFY_QUESTIONS = 10


# ── Model Loader ──────────────────────────────────────────────────────────────

def load_shared_model(model_name: str) -> Llama:
    """
    Loads the quantised GGUF model once and returns the instance to be
    shared across the intent pipeline and compliance reasoning step.

    Sharing a single instance avoids loading the model twice, which
    would consume double the RAM and add significant startup time.

    Parameters
    ----------
    model_name : "qwen" or "llama"

    Returns
    -------
    A loaded Llama instance.
    """
    if model_name not in MODEL_CONFIGS:
        raise ValueError(
            f"Unknown model '{model_name}'. "
            f"Choose from: {list(MODEL_CONFIGS)}"
        )
    path = MODEL_CONFIGS[model_name]
    print(f"Loading model  : {model_name}")
    print(f"Path           : {path}")
    print("Please wait — this may take 30-60 seconds on first load...\n")

    return Llama(
        model_path   = path,
        n_ctx        = 16384,   # large enough for both intent and reasoning prompts
        n_gpu_layers = -1,
        verbose      = False,
    )


# ── Display Helpers ───────────────────────────────────────────────────────────

_DIVIDER      = "─" * 60
_THICK_DIVIDER = "═" * 60
_WRAP_WIDTH   = 80


def _wrap(text: str, indent: int = 0) -> str:
    """Wraps text to _WRAP_WIDTH with optional leading indent."""
    prefix = " " * indent
    return textwrap.fill(
        text,
        width               = _WRAP_WIDTH,
        initial_indent      = prefix,
        subsequent_indent   = prefix,
    )


def _print_header() -> None:
    print()
    print(_THICK_DIVIDER)
    print("  FINRA Compliance Reasoning System")
    print("  Powered by local LLM + ChromaDB")
    print(_THICK_DIVIDER)
    print()
    print("  Ask any FINRA compliance question.")
    print("  The assistant will ask a few clarifying questions,")
    print("  then retrieve the relevant rules and give a reasoned answer.")
    print()
    print("  Type 'quit' or 'exit' at any point to leave.")
    print(_DIVIDER)
    print()


def _print_retrieval_summary(retrieved: list[dict]) -> None:
    """Prints a compact summary of which clauses were retrieved."""
    print()
    print(_DIVIDER)
    print(f"  Retrieved {len(retrieved)} clause(s):")
    for r in retrieved:
        ref      = r.get("clause_ref", "?")
        activity = r.get("activity_type", "")
        dist     = r.get("distance", "")
        line     = f"    • {ref}"
        if activity:
            line += f"  [{activity}]"
        if dist != "":
            line += f"  (similarity distance: {dist})"
        print(line)
    print(_DIVIDER)
    print()


def _print_answer(answer: dict) -> None:
    """
    Prints the structured compliance answer with clear section headers.

    Falls back to printing the raw output if section parsing produced
    empty sections, which can happen with very short model responses.
    """
    sections = {
        "DETERMINATION":      answer.get("determination", ""),
        "APPLICABLE CLAUSES": answer.get("applicable_clauses", ""),
        "REASONING":          answer.get("reasoning", ""),
        "CAVEATS":            answer.get("caveats", ""),
    }

    # Check whether section parsing worked — if all sections are empty,
    # fall back to raw output
    has_content = any(v.strip() for v in sections.values())

    print()
    print(_THICK_DIVIDER)
    print("  COMPLIANCE ANALYSIS")
    print(_THICK_DIVIDER)

    if has_content:
        for header, content in sections.items():
            if not content.strip():
                continue
            print()
            print(f"  {header}")
            print(f"  {'─' * len(header)}")
            # Wrap each paragraph within the section
            for para in content.split("\n"):
                para = para.strip()
                if not para:
                    print()
                    continue
                # Preserve bullet-point lines as-is, wrap prose paragraphs
                if para.startswith("- ") or para.startswith("• "):
                    print(_wrap(para, indent=4))
                else:
                    print(_wrap(para, indent=2))
    else:
        # Fallback: print raw output
        print()
        print(answer.get("raw", "(no output)"))

    print()
    print(_THICK_DIVIDER)
    print()


# ── Single Query Handler ──────────────────────────────────────────────────────

def handle_query(
    model:      Llama,
    collection,
    top_k:      int,
    first_query: str,
) -> None:
    """
    Runs the full pipeline for a single user query:
        intent pipeline → retrieval → compliance reasoning → display.

    Handles errors at each stage gracefully so the chatbot loop can
    continue to the next query rather than crashing.

    Parameters
    ----------
    model        : shared Llama instance
    collection   : loaded ChromaDB collection
    top_k        : number of clauses to retrieve
    first_query  : the user's initial question string
    """

    # ── Stage 1: Intent Pipeline ──────────────────────────────────────────
    print()
    result = run_intent_pipeline(
        model        = model,
        first_query  = first_query,
        max_questions = MAX_CLARIFY_QUESTIONS,
    )

    if result is None:
        print("\n  ✗ Could not structure the intent. Please try rephrasing.\n")
        return

    # run_intent_pipeline now returns (intent_dict, situation_summary)
    intent, situation_summary = result

    if not situation_summary:
        print("\n  ✗ No situation summary was produced. Please try again.\n")
        return

    # ── Stage 2: Retrieval ────────────────────────────────────────────────
    print("\n  Searching knowledge base...")

    # Pass the situation summary into the intent dict so _build_query_string
    # in retrieval.py can use it for richer semantic search
    intent["situation_summary"] = situation_summary

    retrieved = retrieve_clauses(intent, collection, top_k=top_k)

    if not retrieved:
        print(
            "\n  ✗ No relevant clauses were found in the knowledge base.\n"
            "  This may mean the knowledge base is not fully built yet,\n"
            "  or the situation falls outside the current rule set.\n"
        )
        return

    _print_retrieval_summary(retrieved)

    # ── Stage 3: Compliance Reasoning ─────────────────────────────────────
    print("  Reasoning over retrieved clauses...")
    answer = run_compliance_reasoning(model, situation_summary, retrieved)

    # ── Stage 4: Display ──────────────────────────────────────────────────
    _print_answer(answer)


# ── Main Chatbot Loop ─────────────────────────────────────────────────────────

def run_chatbot(model_name: str, top_k: int) -> None:
    """
    Runs the interactive compliance assistant loop.

    Loads the model and ChromaDB collection once, then processes queries
    in a loop until the user exits.

    Parameters
    ----------
    model_name : "qwen" or "llama"
    top_k      : number of clauses to retrieve per query
    """

    # ── Load model ────────────────────────────────────────────────────────
    try:
        model = load_shared_model(model_name)
    except Exception as e:
        print(f"\n✗ Failed to load model: {e}")
        raise SystemExit(1)
    print("✓ Model loaded\n")

    # ── Load ChromaDB collection ──────────────────────────────────────────
    try:
        collection = load_collection()
    except FileNotFoundError as e:
        print(f"\n✗ {e}")
        raise SystemExit(1)
    except Exception as e:
        print(f"\n✗ Failed to load ChromaDB collection: {e}")
        raise SystemExit(1)
    print(f"✓ Knowledge base loaded  ({collection.count()} clauses)\n")

    # ── Print welcome header ──────────────────────────────────────────────
    _print_header()

    # ── Query loop ────────────────────────────────────────────────────────
    while True:
        try:
            first_query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye.\n")
            break

        if not first_query:
            continue

        if first_query.lower() in ("quit", "exit", "q"):
            print("\nGoodbye.\n")
            break

        try:
            handle_query(
                model       = model,
                collection  = collection,
                top_k       = top_k,
                first_query = first_query,
            )
        except SystemExit:
            # Raised by run_clarification_agent when user types quit mid-clarification
            print("\nGoodbye.\n")
            break
        except Exception as e:
            print(f"\n  ✗ Unexpected error: {e}")
            print("  The assistant will continue — please try another question.\n")


# ── CLI Argument Parser ───────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description     = "FINRA Compliance Reasoning Chatbot",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog          = """
Examples:
  python app.py
  python app.py --model llama
  python app.py --model qwen --top-k 8
        """,
    )
    parser.add_argument(
        "--model",
        choices = ["qwen", "llama"],
        default = "llama",
        help    = "Local model to use  (default: llama)",
    )
    parser.add_argument(
        "--top-k",
        type    = int,
        default = DEFAULT_TOP_K,
        help    = f"Number of clauses to retrieve  (default: {DEFAULT_TOP_K})",
    )
    return parser.parse_args()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()
    run_chatbot(model_name=args.model, top_k=args.top_k)
