"""
api/content.py

Every user-facing string that isn't a live agent answer lives here, so you
can tweak copy without touching api/main.py or the frontend JS/HTML at all.
The frontend fetches this via GET /api/ui-config on load.

Edit freely -- these are plain Python values, no special syntax required.
"""

# Shown centered on the empty-state screen, before the first message is
# sent. Disappears as soon as the conversation starts.
GREETING_MESSAGE = "Hello, I'm here to assist you."

# Words cycled through (in order) while a response is being generated, each
# paired with an animated "..." in the UI. Add/remove/reorder freely.
THINKING_WORDS = [
    # "Thinking",
    "Cogitating",
    "Sleuthing",
    "Parsing clauses",
    # "Cross-referencing",
    "Deliberating",
]

# How long each thinking word stays on screen before cycling to the next,
# in milliseconds.
THINKING_WORD_INTERVAL_MS = 1400

# Guidelines page content. Each entry becomes one section: a title and a
# body paragraph. Add as many as you like -- the guidelines page just
# renders this list in order.
GUIDELINES = [
    {
        "title": "This assistant answers FINRA compliance questions",
        "body": (
            "Ask about specific rules, obligations, or scenarios. The more "
            "specific your question, the more precisely it can point to the "
            "relevant clauses."
        ),
    },
    {
        "title": "Answers include citations",
        "body": (
            "Where relevant, responses reference the specific FINRA rule "
            "clauses behind the answer, with a link to the source rule. "
            "Treat these citations as a starting point for your own review, "
            "not a final legal determination."
        ),
    },
]

# Banner text shown at the very top of the guidelines page, making it clear
# to anyone who lands there (e.g. via a shared link) what page this is.
GUIDELINES_PAGE_TITLE = "This is the Guidelines page for the FINRA Compliance Assistant"
