"""
embed_clauses.py

Pipeline for embedding clauses from aggregate_normalized_clauses.jsonl using
whichever embedding backend is configured in the main idiom below (closed-source
API model or open-source local model — see embedders.py).

To switch models, change MODEL_NAME in `if __name__ == "__main__"`.
"""

import json
import logging
from pathlib import Path

from dotenv import load_dotenv

from .embedders import get_embedder

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Data loading / text selection
# --------------------------------------------------------------------------- #

def load_clauses(filepath: str) -> list[dict]:
    """Load clauses from a JSONL file."""
    clauses = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                clauses.append(json.loads(line))
    logger.info(f"Loaded {len(clauses)} clauses from {filepath}")
    return clauses


def get_text_to_embed(clause: dict) -> str | None:
    """
    Select the best text to embed for a clause.
    Prefer merged_clause, fall back to original_clause.
    Returns None if both are empty — caller should skip this clause.
    """
    merged = clause.get("merged_clause", "").strip()
    original = clause.get("original_clause", "").strip()

    if merged:
        return merged
    if original:
        return original

    return None  # Nothing to embed


# --------------------------------------------------------------------------- #
# Embedding generation (model-agnostic — works with any BaseEmbedder)
# --------------------------------------------------------------------------- #

def load_already_embedded_refs(output_path: str) -> set[str]:
    """
    Read an existing (possibly partial) output file and return the set of
    clause_refs already embedded, so a re-run can skip them instead of
    re-embedding and re-paying for work already done.
    """
    path = Path(output_path)
    if not path.exists():
        return set()

    refs = set()
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                refs.add(json.loads(line)["clause_ref"])
    if refs:
        logger.info(f"Resuming: {len(refs)} clauses already embedded in {output_path}")
    return refs


def generate_embeddings(
    clauses: list[dict],
    embedder,
    output_path: str,
    input_type: str = "document",
) -> list[dict]:
    """
    Generate embeddings for all clauses with explicit ID tracking, writing each
    embedded clause to `output_path` AS SOON AS its batch comes back — so a
    crash/rate-limit failure partway through only loses the in-flight batch,
    not everything computed so far. Re-running with the same output_path skips
    clauses that are already present (resume-on-failure).

    Embeddings are matched back to clauses via clause_ref, not list position.
    Returns the list of clauses that had no text to embed (skipped).
    """
    already_done = load_already_embedded_refs(output_path)
    skipped = []

    # Build a list of (clause_ref, text) pairs — identity travels with the text
    tagged_texts: list[tuple[str, str]] = []

    for clause in clauses:
        ref = clause["clause_ref"]
        if ref in already_done:
            continue

        text = get_text_to_embed(clause)
        if text is None:
            logger.warning(f"Skipping clause '{ref}' — both text fields are empty.")
            skipped.append(clause)
        else:
            tagged_texts.append((ref, text))

    logger.info(f"{len(tagged_texts)} clauses to embed, {len(skipped)} skipped.")

    if not tagged_texts:
        return skipped

    refs = [ref for ref, _ in tagged_texts]
    texts = [text for _, text in tagged_texts]
    clauses_by_ref = {c["clause_ref"]: c for c in clauses}

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    out_file = open(output_path, "a")  # append: preserves anything from a prior partial run
    written_count = 0

    def _write_batch(start_index: int, batch_embeddings: list[list[float]]):
        nonlocal written_count
        for offset, embedding in enumerate(batch_embeddings):
            ref = refs[start_index + offset]
            clause_copy = clauses_by_ref[ref].copy()
            clause_copy["embedding"] = embedding
            out_file.write(json.dumps(clause_copy) + "\n")
        out_file.flush()  # make sure it actually hits disk, not just a buffer
        written_count += len(batch_embeddings)
        logger.info(f"Checkpointed {written_count}/{len(texts)} embeddings to {output_path}")

    try:
        embedder.embed(texts, input_type=input_type, on_batch_complete=_write_batch)
    finally:
        out_file.close()

    logger.info(f"Embedding complete. {written_count} clauses written to {output_path}.")
    return skipped


def save_embedded_clauses(clauses: list[dict], output_path: str):
    """Save clauses (e.g. the skipped list) to a JSONL file."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for clause in clauses:
            f.write(json.dumps(clause) + "\n")
    logger.info(f"Saved {len(clauses)} clauses to {output_path}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    # ----- Pick your model here -------------------------------------------
    # Closed-source: "voyage-law-2", "text-embedding-3-small"
    # Open-source:   "Mira190/Euler-Legal-Embedding-V1",
    #                "Octen/Octen-Embedding-8B",
    #                "Qwen/Qwen3-Embedding-8B"
    MODEL_NAME = "text-embedding-3-small"
    # -------------------------------------------------------------------

    # --- Quick smoke test for a newly wired-up model. Uncomment to test a
    # --- single sentence end-to-end before running the full pipeline. ---
    # embedder = get_embedder(MODEL_NAME)
    # test_embedding = embedder.embed(
    #     ["This is a test sentence for embedding."], input_type="document"
    # )[0]
    # print(f"Embedding length: {len(test_embedding)}")
    # print(f"First 5 values: {test_embedding[:5]}")
    # raise SystemExit  # bail out before running the full pipeline below

    project_dir = "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"

    # Load your raw clauses
    clauses = load_clauses(f"{project_dir}/data/aggregate_normalized_clauses.jsonl")
    clauses = [c for c in clauses] # if (c["rule_id"].startswith("3") or c["rule_id"].startswith("4"))

    # Build the embedder for the configured model.
    # NOTE: Voyage's free tier caps you at 3 RPM *and* 10K TPM — batch_size alone
    # doesn't protect you from the token cap, so max_tokens_per_batch does the
    # real work here. Once you add a payment method these limits relax and you
    # can raise both.
    embedder = get_embedder(
        MODEL_NAME,
        batch_size=32,
        max_tokens_per_batch=9000,   # stay under 10K TPM with margin
        sleep_between_batches=60,    # 3 RPM => floor at 20s, +1s buffer
    )
    # embedder = get_embedder(
    #     MODEL_NAME,
    #     batch_size=16,        # GPU/CPU memory + throughput per model.encode() call
    #     checkpoint_size=25,  # how often to checkpoint to disk (crash safety)
    # )

    model_tag = MODEL_NAME.replace("/", "__")  # filesystem-safe
    output_path = f"{project_dir}/data/finra_clauses_embedded__{model_tag}.jsonl"

    # # Generate embeddings — written to output_path incrementally as each batch
    # # completes. Safe to re-run: already-embedded clauses are skipped.
    skipped_clauses = generate_embeddings(clauses, embedder, output_path)

    if skipped_clauses:
        save_embedded_clauses(
            skipped_clauses, f"{project_dir}/data/finra_clauses_skipped__{model_tag}.jsonl"
        )
        logger.warning(f"{len(skipped_clauses)} clauses had no text and were skipped.")