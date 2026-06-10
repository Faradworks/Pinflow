"""Tool stub: ask_user.

Special — the loop in `agent/loop.py` intercepts this tool by name and
suspends the conversation. The run() here is never actually called; it's
defined so the schema is in the toolbelt.
"""

SCHEMA = {
    "name": "ask_user",
    "description": (
        "Ask the user a clarifying question. The conversation suspends; the "
        "user's answer arrives as the tool result on the next turn. Use this "
        "INSTEAD of asking questions in plain text — it renders as an interactive "
        "card and guarantees you get a structured answer. Provide 2-4 options "
        "when there's a finite choice; set allow_freeform=true for open answers."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Multiple-choice options. Empty/omitted = freeform-only.",
            },
            "allow_freeform": {
                "type": "boolean",
                "description": "Whether to accept a freeform answer in addition to options.",
            },
        },
        "required": ["question"],
    },
}


def run(state, **inputs) -> dict:
    # Should never run — loop.py handles ask_user inline by suspending.
    return {
        "status": "error",
        "hint": "ask_user should be intercepted by the loop before dispatch.",
    }
