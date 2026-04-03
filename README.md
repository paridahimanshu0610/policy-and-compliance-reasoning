# FINRA Compliance Reasoning System

An end-to-end compliance decision-support system for FINRA regulations. Scrapes live FINRA rule pages, parses them into structured clause-level documents, stores them in a vector database, and serves hybrid semantic and metadata-filtered retrieval through both a conversational web interface and an MCP server consumable by any MCP-compatible client.

---

## Table of Contents

- [Problem](#problem)
- [Solution Overview](#solution-overview)
- [Architecture](#architecture)
- [Components](#components)
  - [Data Ingestion Pipeline](#1-data-ingestion-pipeline)
  - [Knowledge Base](#2-knowledge-base)
  - [Intent Pipeline](#3-intent-pipeline)
  - [Retrieval Layer](#4-retrieval-layer)
  - [Compliance Reasoning](#5-compliance-reasoning)
  - [Web Interface](#6-web-interface)
  - [MCP Server](#7-mcp-server)
- [Project Structure](#project-structure)
- [Setup and Installation](#setup-and-installation)
- [Usage](#usage)
  - [Build the Knowledge Base](#build-the-knowledge-base)
  - [Run the Web Chatbot](#run-the-web-chatbot)
  - [Run the MCP Server](#run-the-mcp-server)
  - [Connect to Claude Desktop](#connect-to-claude-desktop)
- [FINRA Rules Covered](#finra-rules-covered)
- [Design Decisions](#design-decisions)
- [Limitations](#limitations)

---

## Problem

Financial institutions operating under FINRA regulations face a persistent compliance challenge: determining whether a specific activity triggers a regulatory obligation requires reasoning across multiple rule clauses, conditions, and exceptions scattered across long, unstructured regulatory documents.

Employees — particularly at smaller broker-dealers — often need answers to questions such as:

- Can a registered representative maintain a personal brokerage account at another firm without notifying us?
- Does a branch office inspection require a written report, and who must sign it?
- Under what conditions can an associated person borrow money from a customer?
- What recordkeeping obligations apply to a discretionary account?

Traditional approaches to answering these questions are inadequate:

**Document search** retrieves text passages but cannot evaluate conditions, identify the obligated party, or determine whether an exception applies. Finding the right paragraph in a 40-page rule document is not the same as understanding what the rule requires.

**Simple RAG systems** retrieve semantically similar text but treat all retrieved passages equally. A clause that defines a term and a clause that imposes an obligation look similar to an embedding model but carry entirely different legal weight.

**Manual lookup** is slow, error-prone, and relies on the individual's familiarity with the rulebook — a significant risk in firms with high staff turnover or limited compliance resources.

The result is that employees either spend excessive time researching routine questions, or make compliance decisions based on incomplete understanding of the applicable rules.

---

## Solution Overview

This system addresses the problem through four interlocking components:

1. **Structured knowledge base** — FINRA rules are not stored as raw text. They are parsed at the clause level, with each clause normalized into a structured document containing LLM-extracted metadata fields including the regulated activity, obligated actor, regulated subject, boolean flags for customer and third-party involvement, and topic tags. This transforms unstructured regulatory text into a queryable, filterable dataset.

2. **Intent-driven retrieval** — rather than issuing a raw keyword search, the system conducts a multi-turn clarification conversation with the user to identify five key fields about their situation. This structured intent drives a hybrid retrieval strategy: ChromaDB metadata filters narrow the candidate clause set, and semantic similarity ranking within that set surfaces the most relevant clauses.

3. **Clause-backed reasoning** — a local LLM reads the retrieved clauses and produces a structured compliance analysis citing specific clause references, not general knowledge. The model cannot cite rules that were not retrieved, which grounds the output in the actual regulatory text.

4. **MCP server** — the knowledge base and retrieval capability are exposed as a Model Context Protocol server, enabling any MCP-compatible client — Claude Desktop, Cursor, or a custom agent — to perform structured FINRA clause retrieval without knowledge of the underlying data model.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DATA INGESTION                               │
│                                                                     │
│  FINRA Rule Pages  →  Scraper  →  Clause Parser  →  LLM            │
│  (live HTML)           (BS4)      (hierarchical)    Normaliser      │
│                                                         │           │
│                                                         ▼           │
│                                                   ChromaDB          │
│                                                  (187 clauses)      │
└─────────────────────────────────────────────────────────────────────┘
                                │
                ┌───────────────┴───────────────┐
                │                               │
                ▼                               ▼
┌───────────────────────────┐   ┌───────────────────────────────────┐
│     WEB CHATBOT           │   │         MCP SERVER                │
│                           │   │                                   │
│  User question            │   │  retrieve_clauses (tool)          │
│       │                   │   │  extract_intent   (tool)          │
│       ▼                   │   │  finra://rules/index (resource)   │
│  Clarification agent      │   │  finra://rules/{id}  (resource)   │
│  (local LLM, multi-turn)  │   │  finra://clauses/{ref}(resource)  │
│       │                   │   │  clarification_prompt  (prompt)   │
│       ▼                   │   │  compliance_reasoning  (prompt)   │
│  Intent extraction        │   │                                   │
│       │                   │   │  Consumed by: Claude Desktop,     │
│       ▼                   │   │  Cursor, custom agents            │
│  ChromaDB retrieval       │   └───────────────────────────────────┘
│  (filter + semantic)      │
│       │                   │
│       ▼                   │
│  Compliance reasoning     │
│  (local LLM)              │
│       │                   │
│       ▼                   │
│  Cited compliance answer  │
│  + follow-up conversation │
└───────────────────────────┘
```

---

## Components

### 1. Data Ingestion Pipeline

**File:** `ingestion/parse_finra.py`, `ingestion/build_knowledge_base.py`

The ingestion pipeline runs once to build the knowledge base and supports resumption if interrupted.

**Scraping** — rule pages are fetched from `finra.org` using BeautifulSoup. The scraper targets the rule body content specifically, removes footnote tables, and flattens bold/strong tags into marked text nodes before extraction. A one-second delay between requests is maintained as a courtesy.

**Clause parsing** — raw rule text is split into clause-level units using a custom hierarchical parser that understands FINRA's nested numbering scheme. Parenthetical markers `(a)`, `(a)(1)`, `(a)(1)(A)` and supplementary material decimal markers `.01`, `.02` are both handled. Each clause is assigned:
- A structured `clause_ref` identifier (e.g. `FINRA-3110(c)(3)(C)`)
- A `parent_clause` reference linking it to its governing obligation
- An optional `clause_heading` extracted from bold text

**Fragment merging** — clauses that cannot stand alone — list items ending with semicolons, sentence continuations, or clauses shorter than 20 words — are merged upward through the ancestor chain until a self-contained obligation is found. The merged text is what gets embedded into ChromaDB. This is critical: a clause reading "unless otherwise provided" is meaningless in isolation but meaningful when merged with its parent obligation.

**LLM normalisation** — each clause is sent to a local LLM with a detailed normalisation prompt that extracts structured metadata fields:

| Field | Type | Example |
|---|---|---|
| `category` | string | `"supervision"` |
| `activity_type` | string | `"inspection"` |
| `obligated_actor` | string | `"member"` |
| `regulated_subject` | string | `"branch_office"` |
| `involves_customer` | bool | `false` |
| `involves_third_party` | bool | `false` |
| `has_financial_threshold` | bool | `false` |
| `documentation_required` | bool | `true` |
| `frequency` | string | `"annual"` |
| `reporting_recipient` | string | `null` |
| `subject_matter` | list | `["annual_inspection", "OSJ"]` |
| `keywords` | list | `["inspect at least annually"]` |

Normalisation runs incrementally — each completed clause is written to `data/normalized_documents.jsonl` immediately. If the process is killed, re-running resumes from where it stopped.

**ChromaDB ingestion** — assembled documents are written to a persistent ChromaDB collection with `all-MiniLM-L6-v2` sentence-transformer embeddings and cosine similarity. Ingestion is idempotent — re-running skips already-present document IDs.

---

### 2. Knowledge Base

**Location:** `data/chromadb/`

The knowledge base contains **187 clauses** across 7 FINRA rules stored as normalized documents. Each document has:
- **Embedding text** — the merged clause text (self-contained, includes governing obligation context)
- **Metadata** — all normalized fields listed above, usable as hard filters in ChromaDB queries
- **Provenance** — `rule_id`, `rule_name`, `clause_ref`, `parent_clause`, `merged_up_to`

---

### 3. Intent Pipeline

**File:** `pipeline/intent_pipeline.py`

The intent pipeline converts a free-form user question into a structured query object through two stages.

**Stage 1 — Clarification agent**

A multi-turn conversational agent that gathers five key fields before retrieval:

1. `activity` — what situation is the user asking about
2. `actor` — who is involved and in what role
3. `involves_customer` — whether a customer or their account is affected
4. `involves_third_party` — whether an outside party is involved
5. `has_financial_threshold` — whether a specific financial figure is relevant

The agent asks one question at a time in strict priority order, stopping at the first missing field. Fields 3, 4, and 5 are marked CLEAR after any user response — including expressions of uncertainty — so the conversation does not loop. When all fields are clear, the agent produces a formal third-person situation summary and signals `[READY_TO_STRUCTURE]`.

The prompt underwent significant iteration to address specific failure modes including wrong question priority, field assessment leaking into visible output, and repeated questions on uncertain answers.

**Stage 2 — Intent extraction**

The situation summary is passed to a single-turn LLM call with a structured extraction prompt that maps the situation to the ChromaDB metadata schema. The output is a JSON object with fields including `activity_type`, `category`, `obligated_actor`, `regulated_subject`, and boolean flags. This JSON drives the retrieval filter.

---

### 4. Retrieval Layer

**File:** `pipeline/retrieval.py`

Retrieval uses a two-stage strategy:

**Stage 1 — Metadata filtering** — a `where` filter is constructed from the intent JSON. Only fields whose values are known with high confidence are used as hard filters. Null values and `false` boolean values are never filtered on — a missing filter widens the search safely, while a wrong filter silently excludes correct clauses.

**Stage 2 — Semantic similarity** — within the filtered candidate set, ChromaDB ranks documents by cosine similarity against the situation summary. The situation summary — not keyword fragments — is used as the query string, which matches the natural language embedding space of the stored merged clause texts.

**Fallback** — if the filtered search returns no results, the system automatically retries with no filter and pure semantic search. This prevents the retrieval layer from returning nothing due to imperfect upstream intent extraction.

**Post-filtering** — `applies_to_firm_type` is applied as a soft post-filter: non-matching documents are moved to the end of results rather than excluded, preserving completeness.

---

### 5. Compliance Reasoning

**File:** `pipeline/compliance_reasoning.py`

A final LLM call takes the situation summary and retrieved clauses and produces a structured compliance analysis with four mandatory sections:

- **DETERMINATION** — a 2-3 sentence statement of whether the situation triggers an obligation
- **APPLICABLE CLAUSES** — a list of directly applicable clause references with one-sentence explanations
- **REASONING** — step-by-step reasoning from retrieved clauses to determination
- **CAVEATS** — limitations, conditions, and situations where additional rules may apply

Clause texts are truncated at 600 characters each in the prompt to preserve context budget while retaining the governing obligation. The model is explicitly instructed not to cite rules that are not in the retrieved set.

**Follow-up reasoning** uses a separate prompt that anchors subsequent questions to the already-retrieved clauses and initial analysis. No new retrieval is performed during follow-up turns.

---

### 6. Web Interface

**Files:** `web/server.py`, `web/static/index.html`

A FastAPI backend with a single-page HTML chat UI running entirely on localhost.

**Session lifecycle:**

```
clarifying  →  followup  →  ended
```

- `clarifying` — user is answering clarification questions
- `followup` — compliance answer delivered, follow-up questions accepted
- `ended` — context limit reached, input disabled

**Key design decisions:**
- All LLM calls are dispatched through a `ThreadPoolExecutor` with `max_workers=1` so the async event loop is never blocked. The worker count is fixed at 1 because llama_cpp is not thread-safe with a shared model instance.
- Context usage is tracked per-phase. Clarification tokens and follow-up tokens are counted in separate counters because each phase constructs entirely independent message lists — the follow-up LLM call has no memory of the clarification conversation.
- A context meter displays remaining context as a percentage, with a warning at 20% and hard disable at 5%.

**Run:**
```bash
python -m web.server --model qwen        # default
python -m web.server --model llama
python -m web.server --model qwen --top-k 8 --port 8000
```

---

### 7. MCP Server

**File:** `mcp_server.py`

Exposes the FINRA knowledge base as a Model Context Protocol server. Any MCP-compatible client — Claude Desktop, Cursor, or a custom agent — can consume it without knowledge of the underlying data model or retrieval logic.

**Tools:**

| Tool | Description |
|---|---|
| `retrieve_clauses` | Searches the knowledge base using semantic similarity and optional metadata filtering. Accepts structured intent fields to improve precision. |
| `extract_intent` | Converts a plain-language situation description into structured intent fields matching the ChromaDB schema. Uses the Anthropic API. |

**Resources:**

| Resource | Description |
|---|---|
| `finra://rules/index` | Lists all rules in the knowledge base with clause counts |
| `finra://rules/{rule_id}` | All clauses for a specific rule |
| `finra://clauses/{clause_ref}` | Complete detail record for a single clause |

**Prompts:**

| Prompt | Description |
|---|---|
| `clarification_prompt` | The multi-turn clarification prompt template |
| `compliance_reasoning_prompt` | The reasoning prompt template for analysing retrieved clauses |

**Recommended agent flow:**
1. Read `finra://rules/index` to understand available rules
2. Use `clarification_prompt` to conduct a focused clarification conversation
3. Call `extract_intent` with the situation summary
4. Call `retrieve_clauses` with the situation and intent fields
5. Use `compliance_reasoning_prompt` to reason over retrieved clauses

---

## Project Structure

```
policy-and-compliance-reasoning/
├── config/
│   ├── __init__.py
│   ├── settings.py          # all constants, paths, model configs
│   └── prompts.py           # all prompt templates
├── ingestion/
│   ├── __init__.py
│   ├── parse_finra.py       # scraper, clause parser, normaliser
│   └── build_knowledge_base.py  # orchestration + ChromaDB ingestion
├── pipeline/
│   ├── __init__.py
│   ├── intent_pipeline.py   # clarification agent + intent extraction
│   ├── retrieval.py         # ChromaDB retrieval layer
│   └── compliance_reasoning.py  # reasoning + follow-up
├── web/
│   ├── __init__.py
│   ├── server.py            # FastAPI backend
│   └── static/
│       └── index.html       # chat UI
├── mcp_server.py            # MCP server
├── app.py                   # terminal chatbot (development use)
├── data/
│   ├── chromadb/            # persistent ChromaDB storage
│   ├── normalized_documents.jsonl  # normalisation checkpoint
│   └── parsed_rules.json    # scraping checkpoint
└── models/
    ├── qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf
    └── Meta-Llama-3.1-8B-Instruct-Q8_0.gguf
```

---

## Setup and Installation

**Prerequisites**
- Python 3.11+
- 16GB RAM minimum (for 8B quantized models)
- macOS or Linux (llama_cpp GPU offload requires Metal on Mac or CUDA on Linux)

**Install dependencies**
```bash
pip install requests beautifulsoup4 llama-cpp-python \
            chromadb sentence-transformers \
            fastapi uvicorn mcp anthropic
```

**Download models**

Place GGUF model files in `models/`:
- `qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf`
- `Meta-Llama-3.1-8B-Instruct-Q8_0.gguf`

**Set environment variable (required for MCP server only)**
```bash
export ANTHROPIC_API_KEY=your_key_here
```

---

## Usage

### Build the Knowledge Base

Run once before using any other component. Supports `--skip-scraping` and `--skip-normalizing` flags to resume from checkpoints if interrupted.

```bash
# Full pipeline
python -m ingestion.build_knowledge_base --model qwen

# Resume normalisation after a crash
python -m ingestion.build_knowledge_base --model qwen --skip-scraping

# Re-ingest from existing JSONL checkpoint
python -m ingestion.build_knowledge_base --skip-scraping --skip-normalizing
```

Expected output: `187 clauses` ingested into `data/chromadb/`.

### Run the Web Chatbot

```bash
python -m web.server --model qwen
```

Open `http://127.0.0.1:8000` in your browser. The assistant will ask clarifying questions, retrieve the relevant FINRA clauses, and return a cited compliance analysis. Follow-up questions are supported within the same session.

### Run the MCP Server

```bash
python mcp_server.py
```

The server communicates over stdio and is designed to be launched by an MCP client, not run interactively.

### Connect to Claude Desktop

Add the following to your Claude Desktop configuration file:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "finra-compliance": {
      "command": "python",
      "args": ["/full/path/to/policy-and-compliance-reasoning/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop. A hammer icon in the chat input confirms the server is connected. You can then ask Claude Desktop FINRA compliance questions and it will call your local MCP server to retrieve the relevant rule clauses.

---

## FINRA Rules Covered

| Rule | Name | Category |
|---|---|---|
| 3110 | Supervision | supervision |
| 3120 | Supervisory Control System | supervision |
| 3130 | Annual Certification of Compliance and Supervisory Processes | supervision |
| 3240 | Borrowing From or Lending to Customers | associated_person_conduct |
| 3270 | Outside Business Activities of Registered Persons | associated_person_conduct |
| 3280 | Private Securities Transactions of an Associated Person | associated_person_conduct |
| 4511 | General Requirements for Books and Records | books_and_records |

---

## Design Decisions

**Why clause-level parsing rather than chunk-level splitting?**

Standard RAG systems split documents into fixed-size chunks of 200-500 tokens. FINRA rules are structured as deeply nested legal obligations where meaning depends on hierarchy — `(c)(3)(C)` is an exception to `(c)(3)` which is a sub-requirement of `(c)` which is part of the main obligation in `(a)`. Fixed-size chunking destroys this structure. Clause-level parsing preserves it, and fragment merging ensures every stored unit is self-contained.

**Why structured metadata filtering alongside semantic search?**

Semantic search alone retrieves clauses that are topically similar but may be from the wrong rule category or govern the wrong actor. A question about what a registered representative must do should not retrieve clauses that govern what the member firm must do, even if they are semantically similar. Metadata filtering enforces these distinctions that embedding similarity cannot.

**Why a multi-turn clarification conversation rather than direct retrieval?**

A user asking "do we need to inspect every year?" provides no information about whether they are asking about a branch office, an OSJ, or a non-branch location — three different inspection regimes with different frequency requirements. Clarification surfaces the specifics that determine which clauses are relevant. Without it, retrieval either returns too many clauses or the wrong ones.

**Why local quantized models rather than API calls for the chatbot?**

The clarification conversation involves multiple LLM calls per session — one per clarification turn plus intent extraction plus reasoning. Using API calls for all of these would introduce per-session latency and cost that makes interactive use impractical. Local quantized models run inference in 1-3 seconds per call on consumer hardware.

**Why the MCP server uses the Anthropic API for `extract_intent`?**

The local model is not a dependency that can be shipped with the server. The MCP server is intended to be portable — any developer who clones the repository and builds the knowledge base should be able to run it. The knowledge base and retrieval layer have no local model dependency (ChromaDB and sentence-transformers are lightweight). Only the `extract_intent` tool requires an LLM call, and the Anthropic API provides that without requiring the consumer to download multi-gigabyte model files.

---

## Limitations

- **Knowledge base coverage** — only 7 FINRA rules are currently ingested. Questions about rules outside this set will return no relevant results or misleading results from the closest available clauses.
- **Normalisation quality** — LLM-extracted metadata fields are imperfect. A wrongly classified `activity_type` will cause the metadata filter to exclude correct clauses. The fallback to unfiltered semantic search partially mitigates this.
- **Local model capability** — 7-8B quantized models follow complex multi-constraint instructions less reliably than frontier models. The clarification prompt and reasoning prompt were both extensively tuned to compensate, but edge cases remain.
- **No real-time rule updates** — the knowledge base is built from a static scrape. FINRA rule amendments are not automatically reflected. The ingestion pipeline must be re-run to incorporate updates.
- **Context window limits** — follow-up conversations are bounded by the model's context window. Long clarification conversations or very long retrieved clause texts reduce the available context for follow-up reasoning.
- **Not legal advice** — this system is a decision-support tool. It states what rules require based on retrieved clauses. It is not a substitute for qualified legal counsel.