"""Tool stub: read_datasheet_section."""

SCHEMA = {
    "name": "read_datasheet_section",
    "description": (
        "RAG over a parsed datasheet to answer follow-up questions about a part "
        "(e.g. 'output voltage divider equation', 'EN pin behavior')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mpn": {"type": "string"},
            "query": {"type": "string", "description": "Natural-language query."},
        },
        "required": ["mpn", "query"],
    },
}


def run(state, **inputs) -> dict:
    return {
        "status": "not_implemented",
        "hint": "Tier 3: needs RAG infra.",
    }
