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

    return ChatOpenAI(
        model=cfg["model"],
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        temperature=temperature,
    )
