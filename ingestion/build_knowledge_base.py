"""
build_knowledge_base.py
=======================
Orchestrates the full knowledge base construction pipeline:

    Step 1 — Scrape FINRA rule pages
    Step 2 — Parse scraped text into structured clause dicts
    Step 3 — Normalise each clause via local LLM (structured JSON metadata)
    Step 4 — Set up ChromaDB persistent collection
    Step 5 — Ingest assembled documents into ChromaDB

Run once to build the knowledge base. Re-run safely at any point —
both the scraping and normalisation checkpoints prevent redundant work.
The normalisation pipeline also supports mid-run resumption: if it
crashes, re-running will skip already-normalised clauses.

Usage:
    # Full pipeline using qwen (default)
    python build_knowledge_base.py

    # Use llama model instead
    python build_knowledge_base.py --model llama

    # Skip scraping, use existing parsed checkpoint
    python build_knowledge_base.py --skip-scraping

    # Skip both scraping and normalisation, ingest from existing JSONL
    python build_knowledge_base.py --skip-scraping --skip-normalizing

Dependencies:
    pip install chromadb sentence-transformers
"""

import argparse
import json
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from parse_finra import (
    load_normalizer_model,
    run_scraping_pipeline,
    run_normalization_pipeline,
    PARSED_CHECKPOINT,
    NORMALIZED_CHECKPOINT,
)

# ── Configuration ─────────────────────────────────────────────────────────────

CHROMA_PATH     = "data/chromadb"       # persistent storage location
COLLECTION_NAME = "finra_clauses"       # ChromaDB collection name
EMBEDDING_MODEL = "all-MiniLM-L6-v2"   # lightweight, works well on CPU
BATCH_SIZE      = 50                    # documents per ChromaDB write


# ── Step 4: ChromaDB Setup ────────────────────────────────────────────────────

def setup_chromadb(path: str, collection_name: str) -> chromadb.Collection:
    """
    Initialises a persistent ChromaDB client and returns the clause collection.

    Creates the collection if it does not exist. Uses cosine similarity
    with SentenceTransformer embeddings. The embedding model is downloaded
    automatically on first use by the sentence-transformers library.

    Parameters
    ----------
    path            : directory where ChromaDB stores its files
    collection_name : name of the clause collection

    Returns
    -------
    A chromadb.Collection instance ready for querying and ingestion.
    """
    print(f"\nSetting up ChromaDB at: {path}")
    client = chromadb.PersistentClient(path=path)

    ef = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)

    collection = client.get_or_create_collection(
        name               = collection_name,
        embedding_function = ef,
        metadata           = {"hnsw:space": "cosine"},
    )
    print(f"  ✓ Collection '{collection_name}' ready  "
          f"({collection.count()} existing documents)")
    return collection


# ── Step 5: Ingestion ─────────────────────────────────────────────────────────

def ingest_documents(
    collection: chromadb.Collection,
    documents:  list[dict],
    batch_size: int = BATCH_SIZE,
) -> None:
    """
    Ingests assembled clause documents into ChromaDB in batches.

    Skips documents whose IDs already exist in the collection so that
    re-ingestion is safe and idempotent.

    Each document dict must contain:
        id        : str  — unique ChromaDB document ID (clause_ref)
        document  : str  — text to embed (merged clause text)
        All other keys become ChromaDB metadata fields. Values must be
        str, int, float, or bool — no None, no lists. The assembly step
        in parse_finra.py guarantees this constraint is met.

    Parameters
    ----------
    collection  : the ChromaDB collection to write into
    documents   : list of assembled document dicts
    batch_size  : number of documents per ChromaDB add() call
    """
    # ── Identify already-ingested document IDs ───────────────────────────
    existing_ids: set[str] = set()
    if collection.count() > 0:
        result      = collection.get(include=[])   # fetch IDs only, no vectors
        existing_ids = set(result["ids"])
        print(f"  Skipping {len(existing_ids)} already-ingested documents")

    new_docs = [d for d in documents if d["id"] not in existing_ids]

    if not new_docs:
        print("  ✓ Nothing new to ingest — collection is already up to date.")
        return

    print(f"  Ingesting {len(new_docs)} new documents "
          f"in batches of {batch_size} ...")

    total_batches = (len(new_docs) + batch_size - 1) // batch_size

    for i in range(0, len(new_docs), batch_size):
        batch = new_docs[i : i + batch_size]

        ids       = [d["id"] for d in batch]
        texts     = [d["document"] for d in batch]

        # Strip the reserved keys before passing the rest as metadata
        metadatas = [
            {k: v for k, v in d.items() if k not in ("id", "document")}
            for d in batch
        ]

        collection.add(
            ids        = ids,
            documents  = texts,
            metadatas  = metadatas,
        )

        batch_num = i // batch_size + 1
        end_idx   = min(i + batch_size, len(new_docs))
        print(f"    Batch {batch_num}/{total_batches}: "
              f"documents {i + 1}–{end_idx}  ✓")

    print(f"\n  ✓ Ingestion complete. "
          f"Collection now contains {collection.count()} documents.")


# ── CLI Argument Parser ───────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description = "Build the FINRA compliance knowledge base.",
        formatter_class = argparse.RawDescriptionHelpFormatter,
        epilog = """
Examples:
  python build_knowledge_base.py
  python build_knowledge_base.py --model llama
  python build_knowledge_base.py --skip-scraping
  python build_knowledge_base.py --skip-scraping --skip-normalizing
        """,
    )
    parser.add_argument(
        "--model",
        choices = ["qwen", "llama"],
        default = "qwen",
        help    = "Local model to use for clause normalisation  (default: qwen)",
    )
    parser.add_argument(
        "--skip-scraping",
        action = "store_true",
        help   = (
            "Skip scraping and load parsed rules from the checkpoint file. "
            f"Expects: {PARSED_CHECKPOINT}"
        ),
    )
    parser.add_argument(
        "--skip-normalizing",
        action = "store_true",
        help   = (
            "Skip normalisation and load assembled documents from the JSONL "
            f"checkpoint. Expects: {NORMALIZED_CHECKPOINT}"
        ),
    )
    return parser.parse_args()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()

    # ── Steps 1 & 2: Scraping and Clause Parsing ─────────────────────────
    if args.skip_scraping:
        print(f"\nLoading parsed rules from checkpoint: {PARSED_CHECKPOINT}")
        if not Path(PARSED_CHECKPOINT).exists():
            raise FileNotFoundError(
                f"Checkpoint not found: {PARSED_CHECKPOINT}\n"
                "Run without --skip-scraping to generate it first."
            )
        with open(PARSED_CHECKPOINT) as f:
            all_rules = json.load(f)

        total_clauses = sum(len(r["clauses"]) for r in all_rules.values())
        print(f"  ✓ Loaded {len(all_rules)} rules, {total_clauses} clauses")
    else:
        print("\n── Steps 1 & 2: Scraping and parsing FINRA rules ─────────────")
        all_rules = run_scraping_pipeline()

        if not all_rules:
            print("\n✗ Scraping returned no rules. Exiting.")
            raise SystemExit(1)

    # ── Step 3: Clause Normalisation ─────────────────────────────────────
    if args.skip_normalizing:
        print(f"\nLoading normalised documents from checkpoint: "
              f"{NORMALIZED_CHECKPOINT}")
        if not Path(NORMALIZED_CHECKPOINT).exists():
            raise FileNotFoundError(
                f"Checkpoint not found: {NORMALIZED_CHECKPOINT}\n"
                "Run without --skip-normalizing to generate it first."
            )
        documents: list[dict] = []
        with open(NORMALIZED_CHECKPOINT) as f:
            for line in f:
                line = line.strip()
                if line:
                    documents.append(json.loads(line))
        print(f"  ✓ Loaded {len(documents)} normalised documents")
    else:
        print(f"\n── Step 3: Normalising clauses  (model: {args.model}) ────────")
        model     = load_normalizer_model(args.model)
        documents = run_normalization_pipeline(model, all_rules)

    if not documents:
        print("\n✗ No documents available for ingestion. Exiting.")
        raise SystemExit(1)

    # ── Steps 4 & 5: ChromaDB Setup and Ingestion ─────────────────────────
    print("\n── Steps 4 & 5: ChromaDB setup and ingestion ─────────────────")
    collection = setup_chromadb(CHROMA_PATH, COLLECTION_NAME)
    ingest_documents(collection, documents)

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("✓ Knowledge base construction complete.")
    print(f"  ChromaDB path    : {CHROMA_PATH}")
    print(f"  Collection       : {COLLECTION_NAME}")
    print(f"  Total documents  : {collection.count()}")
    print("=" * 60)
