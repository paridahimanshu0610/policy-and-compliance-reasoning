"""
agent/llm.py

One function, get_chat_model(role), used everywhere a node needs an LLM.
It looks up which model that role should use from config.settings.ACTIVE_LLM,
then builds a ChatOpenAI instance pointed at the TAMU gateway (which speaks
the OpenAI-compatible API, so ChatOpenAI works even though it's not actually
OpenAI).

Centralizing this means: to change which model "clarify" uses, you edit one
line in config/settings.py -- nothing in agent/nodes.py or agent/reasoner.py
needs to change.
"""

from langchain_openai import ChatOpenAI

from config.settings import LLM_MODELS, ACTIVE_LLM

import json
import httpx
class NullContentFixTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request = _patch_request(request)
        return super().handle_request(request)


class AsyncNullContentFixTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request = _patch_request(request)
        return await super().handle_async_request(request)


def _patch_request(request: httpx.Request) -> httpx.Request:
    request.read()  # ensure content is buffered and accessible
    if not request.content:
        return request
    try:
        body = json.loads(request.content)
    except (json.JSONDecodeError, TypeError):
        return request

    changed = False
    for m in body.get("messages", []):
        if m.get("content") is None:
            m["content"] = ""
            changed = True

    if not changed:
        return request

    new_content = json.dumps(body).encode("utf-8")
    headers = httpx.Headers(request.headers)
    headers["content-length"] = str(len(new_content))
    return httpx.Request(
        method=request.method,
        url=request.url,
        headers=headers,
        content=new_content,
    )


http_client = httpx.Client(transport=NullContentFixTransport())
http_async_client = httpx.AsyncClient(transport=AsyncNullContentFixTransport())

def get_chat_model(role: str, temperature: float = 0.0) -> ChatOpenAI:
    """
    Build the chat model configured for a given agent role
    (e.g. "intake", "ambiguity", "clarify", "reasoner").

    temperature defaults to 0 because this is a compliance tool -- we want
    the same situation to produce the same clause retrieval reasoning every
    time, not creative variation.
    """
    if role not in ACTIVE_LLM:
        raise ValueError(
            f"Unknown agent role '{role}'. Add it to config.settings.ACTIVE_LLM."
        )

    model_key = ACTIVE_LLM[role]
    cfg = LLM_MODELS[model_key]

    if not cfg["api_key"] or not cfg["base_url"]:
        raise RuntimeError(
            "TAMUS_AI_CHAT_API_KEY / TAMUS_AI_CHAT_API_ENDPOINT are not set. "
            "Check your .env file."
        )
 
    kwargs = {
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "api_key": cfg["api_key"],
        "http_client": httpx.Client(transport=NullContentFixTransport()),
        "http_async_client": httpx.AsyncClient(transport=AsyncNullContentFixTransport())
    }
    # Some models behind the gateway (reasoning-style ones especially) throw
    # a 400 if you send temperature at all -- only include it for models
    # config.settings marks as supporting it. See the comment on LLM_MODELS.
    if cfg.get("supports_temperature", True):
        kwargs["temperature"] = temperature
 
    return ChatOpenAI(**kwargs)