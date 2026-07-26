"""
eval/user_simulator.py

Plays the role of the user for the duration of one eval question's
conversation. Strictly grounded to `full_situation` -- see the
USER_SIMULATOR_SYSTEM_PROMPT in config/prompts.py for the exact rules
(answer only from full_situation, say "not sure" rather than inventing
anything, don't volunteer unasked info).

Kept as its own tiny module (not folded into run_eval.py) so it can be
unit-tested / spot-checked independently: you can hand it a full_situation
and a fake assistant message and read back exactly what a real user's turn
would have been, without running the whole graph.
"""

from langchain_core.messages import SystemMessage, HumanMessage

from agent.llm import get_chat_model
from config import prompts
from config.settings import USER_SIMULATOR_TEMPERATURE


def simulate_user_turn(full_situation: str, assistant_message: str) -> str:
    """Given the ground-truth situation and the assistant's latest message
    (a clarifying question, an ambiguity question, or a human-handoff
    consent/name/email/note prompt), return the one-turn reply a real user
    grounded in that situation would give."""
    llm = get_chat_model("user_simulator", temperature=USER_SIMULATOR_TEMPERATURE)
    response = llm.invoke([
        SystemMessage(content=prompts.USER_SIMULATOR_SYSTEM_PROMPT),
        HumanMessage(content=prompts.USER_SIMULATOR_TASK_TEMPLATE.format(
            full_situation=full_situation,
            assistant_message=assistant_message,
        )),
    ])
    return response.content.strip()
