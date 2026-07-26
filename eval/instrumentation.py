"""
eval/instrumentation.py

Token-usage tracking for one graph.invoke() (i.e. one run_turn() call).

Uses LangChain's built-in `UsageMetadataCallbackHandler`, the standard,
provider-agnostic way to aggregate token usage across every chat-model call
made during a run (LangChain docs: "Track token usage for multiple calls").
It relies on the same ambient-contextvar callback propagation that
`get_openai_callback()` has always used -- callbacks registered via
`config={"callbacks": [...]}` on the outer `graph.invoke()` call are visible
to every ChatOpenAI invocation nested inside it (intake, ambiguity, clarify,
scope_gate, reasoner, synthesize, judge), even though individual nodes never
explicitly thread `config` through to their own `llm.invoke(...)` calls.
This is why no changes to agent/llm.py or any node are required to get this.

We do NOT sum here into a single "total_tokens" per node -- usage is
reported per underlying model name (handler.usage_metadata is keyed by
model), since ACTIVE_LLM could point different roles at different models.
Summing across models is left to the caller (run_eval.py), since collapsing
"which model did the work" loses information you may want for cost
attribution later.
"""

from contextlib import contextmanager

from langchain_core.callbacks import UsageMetadataCallbackHandler


@contextmanager
def track_usage():
    """
    Usage:
        with track_usage() as (callbacks, get_usage):
            result = graph.invoke(..., config={..., "callbacks": callbacks})
            usage = get_usage()   # {"gpt-4o-mini": {"input_tokens": .., "output_tokens": .., "total_tokens": ..}, ...}
    """
    handler = UsageMetadataCallbackHandler()
    callbacks = [handler]

    def _get_usage() -> dict:
        return dict(handler.usage_metadata)

    yield callbacks, _get_usage


def total_tokens(usage_by_model: dict) -> int:
    """Flatten the per-model usage dict down to a single total, for
    convenience in the per-question summary row."""
    return sum(m.get("total_tokens", 0) for m in usage_by_model.values())
