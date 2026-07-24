"""
agent/pii.py

Input guardrail: mask PII (email addresses, phone numbers, SSNs, card/account
numbers) out of user text BEFORE it ever reaches an LLM prompt or gets
written into situation_summary / the message transcript, and unmask it again
right before anything is shown back to the user (or emailed to a compliance
agent during human handoff).

Deliberately regex-only, not NER -- fast, dependency-free, and deterministic,
which matters for a compliance tool where you want the same input masked the
same way every time. The tradeoff is that free-text PII with no fixed format
(a name mentioned in prose, a home address) isn't caught; this is a
first-line input guardrail, not a replacement for not sending genuinely
sensitive files/data to a third-party model in the first place.

The masking is reversible via a token -> original-value map that gets
threaded through AgentState.pii_map and persists for the whole conversation
(same token is reused if the same value appears again, so the LLM sees a
stable placeholder across turns rather than a new one each time).
"""

import re

# Ordered by specificity so more distinctive formats are matched before
# generic ones. This is a single combined pattern (rather than four separate
# sequential passes) so a value only gets matched once, by the first
# alternative that fits at that position, instead of risking a phone-number
# regex re-matching digits already claimed by the card-number pattern.
_COMBINED_PII_PATTERN = re.compile(
    r"(?P<EMAIL>[\w.+-]+@[\w-]+\.[\w.-]+)"
    r"|(?P<SSN>\b\d{3}-\d{2}-\d{4}\b)"
    r"|(?P<CARD>\b(?:\d[ -]?){13,19}\d\b)"
    r"|(?P<PHONE>\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b)"
)


def _next_index(pii_map: dict[str, str], kind: str) -> int:
    prefix = f"[[{kind}_"
    existing = [
        int(token[len(prefix):-2])
        for token in pii_map
        if token.startswith(prefix) and token.endswith("]]")
    ]
    return max(existing, default=0) + 1


def mask_pii(text: str, pii_map: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """
    Replace PII in `text` with stable, reversible tokens like "[[EMAIL_1]]".

    `pii_map` is the map accumulated so far in the conversation (pass
    state.get("pii_map") in) -- reusing it means the same email/phone number
    mentioned again in a later turn gets the same token instead of a new one,
    which keeps the masked situation_summary coherent across turns.

    Returns (masked_text, updated_pii_map). The updated map is a new dict;
    the input map is not mutated in place.
    """
    if not text:
        return text, dict(pii_map or {})

    updated_map = dict(pii_map or {})
    reverse_lookup = {original: token for token, original in updated_map.items()}

    def _replace(match: re.Match) -> str:
        kind = match.lastgroup
        value = match.group()
        if value in reverse_lookup:
            return reverse_lookup[value]
        token = f"[[{kind}_{_next_index(updated_map, kind)}]]"
        updated_map[token] = value
        reverse_lookup[value] = token
        return token

    masked_text = _COMBINED_PII_PATTERN.sub(_replace, text)
    return masked_text, updated_map


def unmask_pii(text: str | None, pii_map: dict[str, str] | None) -> str | None:
    """
    Replace any mask tokens in `text` back with their original values.
    Safe to call on text with no tokens in it (returns it unchanged) and on
    None (returns None) -- both come up often enough (e.g. an out-of-scope
    message never touched PII at all) that callers shouldn't need to guard
    against them themselves.
    """
    if not text or not pii_map:
        return text
    for token, original in pii_map.items():
        text = text.replace(token, original)
    return text
