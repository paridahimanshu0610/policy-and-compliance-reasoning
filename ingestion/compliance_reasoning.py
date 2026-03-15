"""
compliance_reasoning.py
=======================
Step 7 — Compliance reasoning layer.

Takes the situation summary and the retrieved FINRA clause documents
and produces a structured compliance answer via the local LLM.

The reasoning prompt is deliberately concise to stay within the reliable
attention window of a 7-8B quantised model. It instructs the model to:
    1. Identify which retrieved clauses actually apply to the situation.
    2. State a clear compliance determination.
    3. Explain the reasoning with specific clause citations.
    4. Flag any conditions or caveats the user must be aware of.

Usage:
    from compliance_reasoning import load_reasoning_model, run_compliance_reasoning

    model   = load_reasoning_model("qwen")   # or "llama"
    answer  = run_compliance_reasoning(model, situation_summary, retrieved_clauses)
    print(answer)
"""

import re
from llama_cpp import Llama

# ── Model Configuration ───────────────────────────────────────────────────────

MODEL_CONFIGS = {
    "qwen": (
        "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
        "/models/qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf"
    ),
    "llama": (
        "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
        "/models/Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"
    ),
}


def load_reasoning_model(model_name: str = "qwen") -> Llama:
    """
    Loads the specified quantised GGUF model for compliance reasoning.

    Uses a 8192-token context window. The reasoning prompt includes
    multiple clause texts which can be long, so a larger context than
    the intent pipeline is safer here.

    Parameters
    ----------
    model_name : "qwen" or "llama"

    Returns
    -------
    A loaded Llama instance ready for inference.
    """
    if model_name not in MODEL_CONFIGS:
        raise ValueError(
            f"Unknown model '{model_name}'. "
            f"Choose from: {list(MODEL_CONFIGS)}"
        )
    path = MODEL_CONFIGS[model_name]
    print(f"  Loading reasoning model: {model_name}")
    print(f"  Path: {path}")
    return Llama(
        model_path   = path,
        n_ctx        = 8192,
        n_gpu_layers = -1,
        verbose      = False,
    )


# ── Prompt Template ───────────────────────────────────────────────────────────

COMPLIANCE_REASONING_PROMPT = """You are a FINRA compliance analyst. A compliance \
situation has been described to you. You have been given a set of retrieved \
FINRA rule clauses that are potentially relevant. Your job is to reason over \
the clauses and produce a clear, well-supported compliance answer.

CRITICAL RULES
==============
1. Base your answer ONLY on the provided clauses. Do not cite rules or \
obligations that are not present in the retrieved clauses below.
2. If none of the retrieved clauses are directly applicable, say so clearly \
and explain why.
3. Always cite the specific clause reference (e.g. FINRA-3110(a)(1)) when \
making a compliance statement.
4. Keep your answer focused. Do not repeat clause text verbatim at length — \
paraphrase and cite.
5. If the situation involves conditions or triggers, identify them explicitly.
6. Do not give legal advice. State what the rules require, not what the \
user should do strategically.

OUTPUT FORMAT
=============
Structure your answer in exactly these four sections.
Use the exact section headers shown below.

DETERMINATION
State clearly whether the situation triggers a compliance obligation, \
is permitted, requires action, or is ambiguous based on the retrieved clauses.
Keep this to 2-3 sentences.

APPLICABLE CLAUSES
List each directly applicable clause reference and one sentence explaining \
why it applies. Use this format for each entry:
- [clause_ref]: explanation

REASONING
Explain step by step how the applicable clauses lead to your determination. \
Reference specific clause requirements. Note any conditions, thresholds, or \
exceptions that affect the answer.

CAVEATS
List any important limitations of this answer — for example, clauses that \
could not be retrieved, conditions that depend on facts not provided, or \
situations where a different rule series might also apply.
If there are no caveats, write "None."

SITUATION
=========
{situation_summary}

RETRIEVED CLAUSES
=================
{formatted_clauses}

Now produce your compliance analysis following the output format above."""


# ── Clause Formatter ──────────────────────────────────────────────────────────

def _format_clauses_for_prompt(retrieved_clauses: list[dict]) -> str:
    """
    Formats the list of retrieved clause dicts into a readable block
    for inclusion in the reasoning prompt.

    Each clause is presented with its reference, rule name, activity
    type, and document text. The document text is the merged clause
    text stored at ingestion time, which is self-contained.

    To keep the prompt within the model's reliable attention window,
    each clause's document text is truncated at 600 characters. This
    preserves the governing obligation context while preventing a single
    very long clause from consuming too much of the context budget.

    Parameters
    ----------
    retrieved_clauses : list of result dicts from retrieve_clauses()

    Returns
    -------
    A formatted multi-clause string ready for prompt insertion.
    """
    MAX_CLAUSE_CHARS = 1000  # Truncate clause text to this length for the prompt
    sections: list[str] = []

    for i, clause in enumerate(retrieved_clauses, 1):
        ref          = clause.get("clause_ref", "unknown")
        rule_name    = clause.get("rule_name",  "")
        activity     = clause.get("activity_type", "")
        doc_text     = clause.get("document", "")
        distance     = clause.get("distance", "")

        # Truncate long clause texts to preserve context budget
        if len(doc_text) > MAX_CLAUSE_CHARS:
            doc_text = "..." + doc_text[-MAX_CLAUSE_CHARS:].rstrip()

        header = f"[{i}] {ref}"
        if rule_name:
            header += f"  ({rule_name})"
        if activity:
            header += f"  — {activity}"

        sections.append(f"{header}\n{doc_text}")

    return "\n\n".join(sections)


# ── LLM Call ──────────────────────────────────────────────────────────────────

def _call_reasoning_model(
    model:   Llama,
    prompt:  str,
) -> str:
    """
    Calls the local LLM with the compliance reasoning prompt.

    Uses a slightly higher temperature (0.1) than normalisation to
    allow the model some flexibility in phrasing its explanation,
    while keeping it close to deterministic for the structured sections.

    Strips <think>...</think> blocks emitted by Qwen models before
    returning the content.

    Parameters
    ----------
    model  : loaded Llama instance
    prompt : fully built reasoning prompt

    Returns
    -------
    The model's response string with think-tags removed.
    """
    response = model.create_chat_completion(
        messages    = [{"role": "user", "content": prompt}],
        temperature = 0.1,
        max_tokens  = 1024,
    )
    raw = response["choices"][0]["message"]["content"].strip()

    # Strip Qwen reasoning traces
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    return raw


# ── Section Parser ────────────────────────────────────────────────────────────

def _parse_sections(raw_answer: str) -> dict:
    """
    Parses the model's response into the four expected sections:
    DETERMINATION, APPLICABLE CLAUSES, REASONING, CAVEATS.

    Returns a dict with those four keys. If a section is missing from
    the model's output, its value is an empty string. The raw full
    answer is also included under the key "raw".

    This structured breakdown is used by the chatbot loop to display
    the answer with clear section headers rather than as a wall of text.

    Parameters
    ----------
    raw_answer : the full string returned by the LLM

    Returns
    -------
    Dict with keys: determination, applicable_clauses, reasoning,
                    caveats, raw.
    """
    section_keys = [
        "DETERMINATION",
        "APPLICABLE CLAUSES",
        "REASONING",
        "CAVEATS",
    ]

    result = {k.lower().replace(" ", "_"): "" for k in section_keys}
    result["raw"] = raw_answer

    # Split on section headers — case-insensitive, tolerates extra whitespace
    pattern = re.compile(
        r"(?i)^\s*(" + "|".join(re.escape(k) for k in section_keys) + r")\s*$",
        re.MULTILINE,
    )

    parts  = pattern.split(raw_answer)
    # parts alternates: [pre-text, HEADER, content, HEADER, content, ...]
    i = 1
    while i < len(parts) - 1:
        header  = parts[i].strip().upper()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        key     = header.lower().replace(" ", "_")
        if key in result:
            result[key] = content
        i += 2

    return result


# ── Main Reasoning Function ───────────────────────────────────────────────────

def run_compliance_reasoning(
    model:             Llama,
    situation_summary: str,
    retrieved_clauses: list[dict],
) -> dict:
    """
    Runs the compliance reasoning step and returns a structured answer.

    If retrieved_clauses is empty, returns a no-clauses-found response
    immediately without calling the LLM.

    Parameters
    ----------
    model             : loaded Llama instance from load_reasoning_model()
    situation_summary : situation summary string from the clarification agent
    retrieved_clauses : list of result dicts from retrieve_clauses()

    Returns
    -------
    Dict with keys:
        determination      : str — the core compliance determination
        applicable_clauses : str — list of applicable clause refs + reasons
        reasoning          : str — step-by-step reasoning
        caveats            : str — limitations and conditions
        raw                : str — full unstructured model output
    """
    if not retrieved_clauses:
        empty = (
            "No relevant FINRA clauses were retrieved for this situation. "
            "This may indicate the situation falls outside the scope of the "
            "rules currently in the knowledge base, or that the retrieval "
            "filters were too narrow. Consider rephrasing the query."
        )
        return {
            "determination":      empty,
            "applicable_clauses": "",
            "reasoning":          "",
            "caveats":            "No clauses were retrieved to reason over.",
            "raw":                empty,
        }

    formatted_clauses = _format_clauses_for_prompt(retrieved_clauses)

    prompt = COMPLIANCE_REASONING_PROMPT.format(
        situation_summary = situation_summary,
        formatted_clauses = formatted_clauses,
    )

    raw_answer = _call_reasoning_model(model, prompt)
    return _parse_sections(raw_answer)
