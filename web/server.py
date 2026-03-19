"""
server.py
=========
FastAPI backend for the FINRA Compliance Reasoning chatbot UI.

Session lifecycle:
    clarifying  → user is answering clarification questions
    processing  → server-side only: retrieval + reasoning in progress
    followup    → compliance answer delivered, user may ask follow-ups
    ended       → context limit reached, no further input accepted

Each session stores its full conversation state in memory. Sessions are
isolated — a new conversation always starts a fresh session.

Since llama_cpp inference is synchronous and not thread-safe, all LLM
calls are dispatched through a single-worker ThreadPoolExecutor so the
FastAPI event loop is never blocked.

Usage:
    pip install fastapi uvicorn
    python server.py                   # qwen, default settings
    python server.py --model llama
    python server.py --model qwen --top-k 8 --port 8000
"""

import argparse
import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from config.settings import (
    MODEL_CONFIGS, MAX_CLARIFY_QUESTIONS,
    CONTEXT_WARN_THRESHOLD, CONTEXT_HARD_LIMIT_PCT,
    DEFAULT_TOP_K, MAX_REASONING_CHARS, MAX_CLAUSE_CHARS,
)
from contextlib import asynccontextmanager

from pipeline.compliance_reasoning import run_compliance_reasoning, run_followup_reasoning
from pipeline.intent_pipeline import (
    CLARIFICATION_SYSTEM_PROMPT,
    extract_structured_intent,
    process_clarification_turn,
)
from pipeline.retrieval import load_collection, retrieve_clauses

# ── Configuration ─────────────────────────────────────────────────────────────

# MODEL_CONFIGS = {
#     "qwen": {
#         "path": (
#             "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
#             "/models/qwen2.5-7b-instruct-q8_0-00001-of-00003.gguf"
#         ),
#         # Practical context limit — leave headroom below max_position_embeddings
#         # Qwen max_position_embeddings = 32768; use 28000 to stay safe
#         "max_context_tokens": 28000,
#         "n_ctx": 8192,
#     },
#     "llama": {
#         "path": (
#             "/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning"
#             "/models/Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"
#         ),
#         # Llama max_position_embeddings = 131072; 8B quality degrades well
#         # before the hard limit — use 80000 as a conservative practical cap
#         "max_context_tokens": 80000,
#         "n_ctx": 16384,
#     },
# }

# MAX_CLARIFY_QUESTIONS  = 10
# # Warn user when context drops below this percentage
# CONTEXT_WARN_THRESHOLD = 20
# # Hard disable follow-ups below this percentage
# CONTEXT_HARD_LIMIT_PCT = 5

# ── Globals (set at startup) ──────────────────────────────────────────────────

_model       = None
_collection  = None
_model_name  = "llama"
_top_k       = 5
_max_ctx     = MODEL_CONFIGS["llama"]["max_context_tokens"]
# llama_cpp is not fork-safe, so we use a single worker for all LLM calls
# Explanation: https://chatgpt.com/s/t_69bb329487288191a7add2f55fba9fcf
_executor    = ThreadPoolExecutor(max_workers=1)

# In-memory session store  { session_id: session_dict }
_sessions: dict[str, dict] = {}

# ── FastAPI App ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model, _collection, _max_ctx
    print("\nStarting FINRA Compliance Reasoning System...")
    print(f"  Model  : {_model_name}")
    print(f"  Top-k  : {_top_k}")

    print("\nLoading model (this may take 30–60 seconds)...")
    _model = await _run_in_executor(_load_model_sync, _model_name)
    print("  ✓ Model loaded")

    print("Loading ChromaDB collection...")
    _collection = load_collection()
    print(f"  ✓ Collection loaded  ({_collection.count()} clauses)")

    _max_ctx = MODEL_CONFIGS[_model_name]["max_context_tokens"]
    print(f"\n  Context budget : {_max_ctx:,} tokens")
    print("  Ready.\n")

    yield


app = FastAPI(title="FINRA Compliance Reasoning System", lifespan=lifespan)

# Serve static files (index.html) from ./static
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Pydantic Models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str | None = None
    message:    str


class ChatResponse(BaseModel):
    session_id:          str
    role:                str   # "assistant"
    content:             str
    phase:               str   # "clarifying" | "followup" | "ended"
    context_used_pct:    int   # 0–100
    context_remaining_pct: int
    context_warning:     bool
    retrieved_clauses:   list[dict] | None = None   # sent once when reasoning begins


# ── Session Helpers ───────────────────────────────────────────────────────────

def _new_session() -> dict:
    return {
        "session_id":       str(uuid.uuid4()),
        "phase":            "clarifying",
        # Clarification state
        "conversation":     [],        # list of {role, content} dicts
        "questions_asked":  0,
        # Retrieved context (set once, reused for all follow-ups)
        "situation_summary":  None,
        "intent":             None,
        "retrieved_clauses":  None,
        "reasoning_answer":   None,    # dict from run_compliance_reasoning
        # Follow-up conversation history
        "followup_history":   [],      # list of {role, content} dicts
        # Token tracking (character-based approximation: 4 chars ≈ 1 token)
        "tokens_used_clarification": 0,   # clarification phase only
        "tokens_used_followup":      0,   # follow-up phase only
    }


def _approx_tokens(text: str) -> int:
    """Approximate token count: 4 characters ≈ 1 token."""
    return max(1, len(text) // 4)


def _context_pcts(session: dict) -> tuple[int, int]:
    if session["phase"] == "followup" or session["phase"] == "ended":
        used = session["tokens_used_followup"]
    else:
        used = session["tokens_used_clarification"]
    pct_used = min(100, int(used / _max_ctx * 100))
    return pct_used, 100 - pct_used


def _session_response(
    session:           dict,
    content:           str,
    retrieved_clauses: list[dict] | None = None,
) -> dict:
    used_pct, remaining_pct = _context_pcts(session)
    return {
        "session_id":           session["session_id"],
        "role":                 "assistant",
        "content":              content,
        "phase":                session["phase"],
        "context_used_pct":     used_pct,
        "context_remaining_pct": remaining_pct,
        "context_warning":      remaining_pct <= CONTEXT_WARN_THRESHOLD,
        "retrieved_clauses":    retrieved_clauses,
    }


# ── LLM Dispatch (runs blocking calls off the event loop) ────────────────────

async def _run_in_executor(fn, *args):
    """Runs a synchronous (blocking) function in the thread executor."""
    loop = asyncio.get_event_loop()
    # await on the main thread's event loop while the worker thread runs the blocking function.
    # This allows the FastAPI server to remain responsive even while the model is running inference.
    return await loop.run_in_executor(_executor, fn, *args)


# ── Clarification Phase Handler ───────────────────────────────────────────────

def _do_clarification_turn(session: dict, user_message: str) -> dict:
    """
    Synchronous worker: runs one clarification turn and mutates the session.

    If the clarification agent signals [READY_TO_STRUCTURE], this function
    immediately continues through intent extraction, retrieval, and reasoning
    before returning, so the caller receives the full compliance answer in a
    single HTTP response.

    Returns a response dict ready to send to the client.
    """
    result = process_clarification_turn(
        model           = _model,
        user_message    = user_message,
        conversation    = session["conversation"],
        questions_asked = session["questions_asked"],
        max_questions   = MAX_CLARIFY_QUESTIONS,
    )

    session["conversation"]    = result["conversation"]
    session["questions_asked"] = result["questions_asked"]
    session["tokens_used_clarification"]   += _approx_tokens(user_message) + _approx_tokens(result["content"])

    if result["type"] == "question":
        # ── CHECK 1: after every clarification question ───────────────────
        _, remaining_pct = _context_pcts(session)
        if remaining_pct <= CONTEXT_HARD_LIMIT_PCT:
            session["phase"] = "ended"
            return _session_response(
                session,
                result["content"] + (
                    "\n\n---\n*Context limit reached. "
                    "Please start a new conversation to continue.*"
                ),
            )
        return _session_response(session, result["content"])

    # ── [READY_TO_STRUCTURE] received — run the full pipeline ─────────────
    situation_summary = result["content"]
    session["situation_summary"] = situation_summary

    # Intent extraction
    intent = extract_structured_intent(_model, situation_summary)
    if intent is None:
        session["phase"] = "ended"
        msg = (
            "I was unable to structure the compliance intent from our "
            "conversation. Please start a new session and try rephrasing "
            "your question."
        )
        session["tokens_used_clarification"] += _approx_tokens(msg)
        return _session_response(session, msg)

    intent["situation_summary"] = situation_summary
    session["intent"] = intent

    # Retrieval
    retrieved = retrieve_clauses(intent, _collection, top_k=_top_k)
    session["retrieved_clauses"] = retrieved
    session["tokens_used_clarification"] += sum(
        _approx_tokens(c.get("document", "")) for c in retrieved
    )

    # ── CHECK 2: after retrieval, before the reasoning LLM call ──────────
    _, remaining_pct = _context_pcts(session)
    if remaining_pct <= CONTEXT_HARD_LIMIT_PCT:
        session["phase"] = "ended"
        return _session_response(
            session,
            "Context limit reached before reasoning could complete. "
            "Please start a new conversation.",
        )

    # Compliance reasoning
    answer = run_compliance_reasoning(_model, situation_summary, retrieved)
    session["reasoning_answer"] = answer
    session["tokens_used_followup"]     += _approx_tokens(answer.get("raw", ""))

    system_prompt_approx = (
        _approx_tokens(situation_summary) +
        sum(_approx_tokens(c.get("document", "")[:MAX_CLAUSE_CHARS]) for c in retrieved) +
        _approx_tokens(answer.get("raw", "")[:MAX_REASONING_CHARS])
    )
    session["tokens_used_followup"] = system_prompt_approx
    session["phase"] = "followup"

    # Format the answer as a single readable string for the UI
    formatted = _format_reasoning_for_ui(answer)
    session["tokens_used_followup"] += _approx_tokens(formatted)

    return _session_response(session, formatted, retrieved_clauses=retrieved)


def _format_reasoning_for_ui(answer: dict) -> str:
    """
    Converts a structured compliance answer dict into a single markdown-
    friendly string for display in the chat UI.
    """
    sections = [
        ("DETERMINATION",      answer.get("determination", "")),
        ("APPLICABLE CLAUSES", answer.get("applicable_clauses", "")),
        ("REASONING",          answer.get("reasoning", "")),
        ("CAVEATS",            answer.get("caveats", "")),
    ]
    parts = []
    for header, content in sections:
        if content.strip():
            parts.append(f"**{header}**\n{content.strip()}")
    return "\n\n".join(parts) if parts else answer.get("raw", "No analysis available.")


# ── Follow-up Phase Handler ───────────────────────────────────────────────────

def _do_followup_turn(session: dict, user_message: str) -> dict:
    """
    Synchronous worker: runs one follow-up reasoning turn.

    Uses the already-retrieved clauses and the initial reasoning answer
    as fixed context. No new retrieval is performed.
    """
    session["tokens_used_followup"] += _approx_tokens(user_message)

    response_text = run_followup_reasoning(
        model                 = _model,
        situation_summary     = session["situation_summary"],
        retrieved_clauses     = session["retrieved_clauses"],
        initial_reasoning_raw = session["reasoning_answer"].get("raw", ""),
        followup_history      = session["followup_history"],
        new_question          = user_message,
    )

    session["followup_history"].append({"role": "user",      "content": user_message})
    session["followup_history"].append({"role": "assistant", "content": response_text})
    session["tokens_used_followup"] += _approx_tokens(response_text)

    # Check whether context is exhausted after this turn
    _, remaining_pct = _context_pcts(session)
    if remaining_pct <= CONTEXT_HARD_LIMIT_PCT:
        session["phase"] = "ended"
        response_text += (
            "\n\n---\n*Context limit reached. "
            "Please start a new conversation to continue.*"
        )

    return _session_response(session, response_text)


# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Serve the chat UI."""
    return FileResponse(str(static_dir / "index.html"))


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Main chat endpoint. Handles both clarification and follow-up turns.

    If session_id is null or not found, a new session is created and
    the first clarification turn is run immediately.
    """
    # ── Resolve or create session ─────────────────────────────────────────
    session_id = request.session_id
    if not session_id or session_id not in _sessions:
        session    = _new_session()
        session_id = session["session_id"]
        _sessions[session_id] = session
    else:
        session = _sessions[session_id]

    # ── Guard: session already ended ─────────────────────────────────────
    if session["phase"] == "ended":
        return _session_response(
            session,
            "This conversation has ended. Please start a new one.",
        )

    # ── Dispatch to correct phase handler ─────────────────────────────────
    if session["phase"] == "clarifying":
        response = await _run_in_executor(_do_clarification_turn, session, request.message)
    elif session["phase"] == "followup":
        # Check context before accepting follow-up
        _, remaining_pct = _context_pcts(session)
        if remaining_pct <= CONTEXT_HARD_LIMIT_PCT:
            session["phase"] = "ended"
            return _session_response(
                session,
                "Context limit reached. Please start a new conversation.",
            )
        response = await _run_in_executor(_do_followup_turn, session, request.message)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown phase: {session['phase']}")

    return response


@app.post("/api/new-session")
async def new_session_endpoint():
    """Creates a new session and returns its ID."""
    session    = _new_session()
    _sessions[session["session_id"]] = session
    return {"session_id": session["session_id"]}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    """Returns the current state of a session (for debugging)."""
    if session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    s = _sessions[session_id]
    return {
        "session_id":      s["session_id"],
        "phase":           s["phase"],
        "questions_asked": s["questions_asked"],
        "tokens_used":     s["tokens_used"],
        "max_tokens":      _max_ctx,
    }


@app.get("/api/health")
async def health():
    return {
        "status":     "ok",
        "model":      _model_name,
        "collection": _collection.count() if _collection else 0,
    }


# ── Startup ───────────────────────────────────────────────────────────────────

def _load_model_sync(model_name: str):
    from llama_cpp import Llama
    cfg  = MODEL_CONFIGS[model_name]
    print(f"  Loading model '{model_name}' from:\n  {cfg['path']}")
    return Llama(
        model_path   = cfg["path"],
        n_ctx        = cfg["n_ctx"],
        n_gpu_layers = -1,
        verbose      = False,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="FINRA Compliance Chatbot Server")
    parser.add_argument("--model",  choices=["qwen", "llama"], default="llama")
    parser.add_argument("--top-k",  type=int, default=5)
    parser.add_argument("--port",   type=int, default=8000)
    parser.add_argument("--host",   type=str, default="127.0.0.1")
    return parser.parse_args()


if __name__ == "__main__":
    args       = parse_args()
    _model_name = args.model
    _top_k      = args.top_k

    uvicorn.run(
        "web.server:app",
        host    = args.host,
        port    = args.port,
        reload  = False,
        workers = 1,    # must be 1 — model is not fork-safe
    )
