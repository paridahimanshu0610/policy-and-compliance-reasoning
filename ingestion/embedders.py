"""
embedders.py

Modular embedding backends, unified behind a single interface:

    embedder = get_embedder("voyage-law-2")
    vectors = embedder.embed(["some text", "some other text"], input_type="document")

Two families:
  - Closed-source / API embedders  (Voyage, OpenAI-compatible)   -> APIEmbedder subclasses
  - Open-source / local embedders  (Euler, Octen, Qwen3)         -> LocalEmbedder subclasses

All embedders expose the same method signature:
    embed(texts: list[str], input_type: str = "document") -> list[list[float]]

`input_type` is "document" when embedding clauses for storage, and "query" when
embedding a search query at retrieval time. Each backend decides what (if anything)
to do with that distinction (e.g. Qwen3 adds an instruction prefix for queries only;
OpenAI's text-embedding-3-small ignores it entirely).
"""

import os
import time
import logging
from abc import ABC, abstractmethod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Base interface
# --------------------------------------------------------------------------- #

class BaseEmbedder(ABC):
    """Common interface every embedder (API-based or local) must implement."""

    model_name: str

    @abstractmethod
    def embed(
        self, texts: list[str], input_type: str = "document", on_batch_complete=None
    ) -> list[list[float]]:
        """Return one embedding per input text, in the same order as `texts`."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Closed-source / API-based embedders
# --------------------------------------------------------------------------- #

class APIEmbedder(BaseEmbedder):
    """
    Shared batching + retry + rate-limit-friendly-sleep logic for any embedder
    that goes over the network. Subclasses just implement `_embed_batch_api`.

    Two independent caps decide how many texts go into a single API request:
      - `batch_size`: a hard cap on the *count* of texts per request
      - `max_tokens_per_batch`: an estimated cap on *tokens* per request
        (needed because providers rate-limit on tokens-per-minute (TPM) too,
        not just requests-per-minute (RPM) — a single oversized request can
        blow the TPM budget even if you're only sending one request every
        20+ seconds)

    Token counts are estimated with a cheap chars/4 heuristic rather than a
    model-specific tokenizer — it's approximate, but staying comfortably under
    the provider's stated TPM limit only requires a conservative estimate,
    not an exact one.
    """

    def __init__(
        self,
        batch_size: int = 64,
        max_tokens_per_batch: int = 9000,  # stay under a 10K TPM limit with margin
        sleep_between_batches: float = 25,
        retries: int = 3,
    ):
        self.batch_size = batch_size
        self.max_tokens_per_batch = max_tokens_per_batch
        self.sleep_between_batches = sleep_between_batches
        self.retries = retries

    @abstractmethod
    def _embed_batch_api(self, batch: list[str], input_type: str) -> list[list[float]]:
        """Call the provider's API for a single batch and return embeddings."""
        raise NotImplementedError

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # Conservative heuristic: ~4 characters per token for English/legal text.
        return max(1, len(text) // 4)

    def _build_request_batches(self, texts: list[str]) -> list[list[str]]:
        """
        Group texts into request-sized batches respecting BOTH batch_size
        (count) and max_tokens_per_batch (estimated tokens).
        """
        batches: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0

        for text in texts:
            text_tokens = self._estimate_tokens(text)
            exceeds_tokens = current and (current_tokens + text_tokens > self.max_tokens_per_batch)
            exceeds_count = len(current) >= self.batch_size

            if current and (exceeds_tokens or exceeds_count):
                batches.append(current)
                current = []
                current_tokens = 0

            current.append(text)
            current_tokens += text_tokens

        if current:
            batches.append(current)

        return batches

    def embed(
        self,
        texts: list[str],
        input_type: str = "document",
        on_batch_complete=None,  # Optional[Callable[[int, list[list[float]]], None]]
    ) -> list[list[float]]:
        """
        `on_batch_complete(start_index, batch_embeddings)`, if provided, is
        called immediately after each successful request — `start_index` is
        the index into the original `texts` list where this batch begins, so
        callers can checkpoint results to disk as they arrive instead of only
        at the very end.
        """
        all_embeddings: list[list[float]] = []
        request_batches = self._build_request_batches(texts)
        total_batches = len(request_batches)
        running_index = 0

        for batch_num, batch in enumerate(request_batches, start=1):
            batch_tokens = sum(self._estimate_tokens(t) for t in batch)
            logger.info(
                f"[{self.model_name}] Embedding batch {batch_num}/{total_batches} "
                f"({len(batch)} texts, ~{batch_tokens} est. tokens)"
            )

            for attempt in range(self.retries):
                try:
                    batch_embeddings = self._embed_batch_api(batch, input_type)
                    all_embeddings.extend(batch_embeddings)
                    if on_batch_complete is not None:
                        on_batch_complete(running_index, batch_embeddings)
                    break
                except Exception as e:
                    if attempt < self.retries - 1:
                        # Retries are themselves API calls and must also respect
                        # the configured rate limit, so floor the wait at
                        # sleep_between_batches rather than a fixed short backoff.
                        wait = max((attempt + 1) * 10, self.sleep_between_batches)
                        logger.warning(
                            f"[{self.model_name}] Batch {batch_num} failed "
                            f"(attempt {attempt + 1}): {e}. Retrying in {wait}s..."
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            f"[{self.model_name}] Batch {batch_num} failed after "
                            f"{self.retries} attempts: {e}"
                        )
                        raise

            running_index += len(batch)
            time.sleep(self.sleep_between_batches)

        return all_embeddings


class VoyageEmbedder(APIEmbedder):
    """voyage-law-2 (and any other Voyage model) via the voyageai SDK."""

    def __init__(self, model_name: str = "voyage-law-2", **kwargs):
        super().__init__(**kwargs)
        import voyageai  # local import so the dependency is only required if used

        self.model_name = model_name
        self.client = voyageai.Client(api_key=os.getenv("VOYAGE_API_KEY"))

    def _embed_batch_api(self, batch: list[str], input_type: str) -> list[list[float]]:
        result = self.client.embed(
            texts=batch,
            model=self.model_name,
            input_type=input_type,  # Voyage natively supports "document" / "query"
        )
        return result.embeddings


class OpenAIEmbedder(APIEmbedder):
    """
    text-embedding-3-small (or any OpenAI-compatible embedding model), routed
    through the TAMU gateway. `input_type` is accepted for interface parity but
    has no effect — OpenAI's embedding endpoint doesn't distinguish query/document.
    """

    def __init__(self, model_name: str = "protected.text-embedding-3-small", **kwargs):
        super().__init__(**kwargs)
        from openai import OpenAI  # local import

        self.model_name = model_name
        tamu_config = {
            "api_key": os.getenv("TAMUS_AI_CHAT_API_KEY"),
            "base_url": os.getenv("TAMUS_AI_CHAT_API_ENDPOINT"),
        }
        self.client = OpenAI(api_key=tamu_config["api_key"], base_url=tamu_config["base_url"])

    def _embed_batch_api(self, batch: list[str], input_type: str) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model_name, input=batch)
        # response.data is returned in the same order as the input list
        return [item.embedding for item in response.data]


# --------------------------------------------------------------------------- #
# Open-source / local embedders (sentence-transformers)
# --------------------------------------------------------------------------- #

class LocalEmbedder(BaseEmbedder):
    """
    Wraps a sentence-transformers model running locally.

    `query_prompt_name`, when set, is passed as `prompt_name` to `model.encode`
    ONLY when input_type == "query" (e.g. Qwen3-Embedding's "query" prompt).
    Document-side texts are never prefixed, matching how these models are trained.

    Two different chunk sizes are in play, and they mean different things:
      - `batch_size`: passed straight through to `model.encode(...)`. This is
        purely a GPU/CPU memory + throughput knob controlling how many texts
        get run through the model in one forward pass.
      - `checkpoint_size`: an OUTER loop around `model.encode(...)` calls.
        After every `checkpoint_size` texts are embedded, `on_batch_complete`
        fires so the caller can write results to disk. This protects against
        losing an entire run (OOM, crash, accidental interrupt) partway
        through — without it, `on_batch_complete` would only fire once, at
        the very end, after everything is already embedded.
    """

    def __init__(
        self,
        model_name: str,
        trust_remote_code: bool = True,
        max_seq_length: int | None = None,
        model_kwargs: dict | None = None,
        tokenizer_kwargs: dict | None = None,
        query_prompt_name: str | None = None,
        batch_size: int = 16,
        checkpoint_size: int = 100,
    ):
        from sentence_transformers import SentenceTransformer  # local import

        self.model_name = model_name
        self.query_prompt_name = query_prompt_name
        self.batch_size = batch_size
        self.checkpoint_size = checkpoint_size

        self.model = SentenceTransformer(
            model_name,
            trust_remote_code=trust_remote_code,
            model_kwargs=model_kwargs or {},
            tokenizer_kwargs=tokenizer_kwargs or {},
        )
        if max_seq_length is not None:
            self.model.max_seq_length = max_seq_length

    def embed(
        self,
        texts: list[str],
        input_type: str = "document",
        on_batch_complete=None,  # Optional[Callable[[int, list[list[float]]], None]]
    ) -> list[list[float]]:
        encode_kwargs = dict(
            normalize_embeddings=True,
            batch_size=self.batch_size,
            show_progress_bar=True,
        )
        if input_type == "query" and self.query_prompt_name:
            encode_kwargs["prompt_name"] = self.query_prompt_name

        all_embeddings: list[list[float]] = []
        total = len(texts)

        for start in range(0, total, self.checkpoint_size):
            chunk = texts[start : start + self.checkpoint_size]
            logger.info(
                f"[{self.model_name}] Embedding {start + 1}-{start + len(chunk)} of {total} "
                f"(input_type={input_type})"
            )
            chunk_embeddings = self.model.encode(chunk, **encode_kwargs).tolist()
            all_embeddings.extend(chunk_embeddings)

            if on_batch_complete is not None:
                on_batch_complete(start, chunk_embeddings)

        return all_embeddings


# --------------------------------------------------------------------------- #
# Registry + factory
# --------------------------------------------------------------------------- #

# Config for every supported model. Add new models here — no other code needs
# to change to support them.
_CLOSED_SOURCE_REGISTRY = {
    "voyage-law-2": lambda **kw: VoyageEmbedder(model_name="voyage-law-2", **kw),
    "text-embedding-3-small": lambda **kw: OpenAIEmbedder(
        model_name="protected.text-embedding-3-small", **kw
    ),
}

_OPEN_SOURCE_REGISTRY = {
    "Mira190/Euler-Legal-Embedding-V1": lambda **kw: LocalEmbedder(
        model_name="Mira190/Euler-Legal-Embedding-V1",
        trust_remote_code=True,
        max_seq_length=1536,
        # No torch_dtype override — loads in the model's default (full) precision.
        model_kwargs={},
        query_prompt_name=None,  # no query/document distinction documented
        **kw,
    ),
    "Octen/Octen-Embedding-8B": lambda **kw: LocalEmbedder(
        model_name="Octen/Octen-Embedding-8B",
        trust_remote_code=True,
        query_prompt_name=None,  # no query/document distinction documented
        **kw,
    ),
    "Qwen/Qwen3-Embedding-8B": lambda **kw: LocalEmbedder(
        model_name="Qwen/Qwen3-Embedding-8B",
        trust_remote_code=True,
        model_kwargs={"attn_implementation": "flash_attention_2", "device_map": "auto"},
        tokenizer_kwargs={"padding_side": "left"},
        query_prompt_name="query",  # queries get the "Instruct: ..." prompt, docs don't
        **kw,
    ),
}


def get_embedder(model_name: str, **kwargs) -> BaseEmbedder:
    """
    Factory: given a model name, return the right embedder instance.
    `**kwargs` are forwarded to the constructor (e.g. batch_size override).
    """
    if model_name in _CLOSED_SOURCE_REGISTRY:
        return _CLOSED_SOURCE_REGISTRY[model_name](**kwargs)
    if model_name in _OPEN_SOURCE_REGISTRY:
        return _OPEN_SOURCE_REGISTRY[model_name](**kwargs)

    raise ValueError(
        f"Unknown model '{model_name}'. Supported models: "
        f"{list(_CLOSED_SOURCE_REGISTRY) + list(_OPEN_SOURCE_REGISTRY)}"
    )