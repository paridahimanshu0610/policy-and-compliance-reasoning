"""
api/main.py

FastAPI wrapper around agent.graph.run_turn().

Run with:
    uvicorn api.main:app --reload --port 8000

(run from the project root, i.e. the directory that contains both `agent/`
and `api/`, so the `agent` and `config` packages import cleanly.)
"""

import logging
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from agent.graph import run_turn
from api.content import (
    GREETING_MESSAGE,
    THINKING_WORDS,
    THINKING_WORD_INTERVAL_MS,
    GUIDELINES_PAGE_TITLE,
    GUIDELINES,
)
from api.schemas import (
    ChatRequest,
    ChatResponse,
    ClauseCitation,
    GuidelineItem,
    GuidelinesResponse,
    NewSessionResponse,
    UIConfigResponse,
)
from api.settings import ALLOWED_ORIGINS

logger = logging.getLogger("finra_compliance_api")

app = FastAPI(
    title="FINRA Compliance Assistant API",
    description="HTTP interface over the agent.graph LangGraph agent.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_trace(raw_trace: list[dict] | None) -> list[ClauseCitation] | None:
    """Reshape run_turn()'s raw trace (list of clause dicts) into
    ClauseCitation models. Only clause_ref + rule_url are surfaced.

    Tries a couple of plausible key names for the rule's source URL since
    the exact field name in clause_graph wasn't confirmed against
    agent/reasoner.py -- if none match, rule_url is simply omitted rather
    than raising, so a schema mismatch degrades gracefully instead of
    breaking the whole response.
    """
    if not raw_trace:
        return None

    def _first_present(d: dict, keys: list[str]) -> str | None:
        for k in keys:
            if d.get(k):
                return d[k]
        return None

    citations = []
    for clause in raw_trace:
        citations.append(
            ClauseCitation(
                clause_ref=clause.get("clause_ref", "unknown"),
                rule_url=_first_present(clause, ["rule_url", "url", "source_url"]),
            )
        )
    return citations


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/ui-config", response_model=UIConfigResponse)
def ui_config() -> UIConfigResponse:
    """Copy the frontend needs before any conversation happens: the
    empty-state greeting and the words to cycle through while thinking.
    Backed entirely by api/content.py -- edit that file, not this route."""
    return UIConfigResponse(
        greeting_message=GREETING_MESSAGE,
        thinking_words=THINKING_WORDS,
        thinking_word_interval_ms=THINKING_WORD_INTERVAL_MS,
    )


@app.get("/api/guidelines", response_model=GuidelinesResponse)
def guidelines() -> GuidelinesResponse:
    """Content for the standalone guidelines page. Backed by
    api/content.py's GUIDELINES list -- add/edit entries there."""
    return GuidelinesResponse(guidelines=[GuidelineItem(**g) for g in GUIDELINES])


@app.get("/api/guidelines-page-title")
def guidelines_page_title() -> dict:
    return {"title": GUIDELINES_PAGE_TITLE}


@app.post("/api/session", response_model=NewSessionResponse)
def new_session() -> NewSessionResponse:
    """Mint a fresh thread_id for a brand-new conversation. The frontend
    calls this once, then reuses the same thread_id on every /api/chat
    call in that conversation."""
    return NewSessionResponse(thread_id=str(uuid.uuid4()))


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    """One turn of the conversation. If the graph is paused mid
    human-handoff Q&A for this thread_id, run_turn() resumes it
    automatically -- the caller doesn't need to know or care."""
    thread_id = payload.thread_id or str(uuid.uuid4())

    try:
        # run_turn() drives a synchronous graph.invoke() under the hood.
        # Leaving this endpoint as a plain `def` (not `async def`) lets
        # FastAPI dispatch it to its threadpool automatically, so one slow
        # turn doesn't block the event loop for other conversations.
        result = run_turn(user_message=payload.message, thread_id=thread_id)
    except Exception:
        logger.exception("run_turn failed for thread_id=%s", thread_id)
        raise HTTPException(status_code=500, detail="The assistant hit an internal error processing that message.")

    return ChatResponse(
        type=result["type"],
        thread_id=thread_id,
        content=result["content"],
        trace=_build_trace(result.get("trace")) if result.get("type") == "answer" else None,
        conflicts=result.get("conflicts") if result.get("type") == "answer" else None,
    )
