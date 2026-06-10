"""Tool stub: select_part."""

SCHEMA = {
    "name": "select_part",
    "description": (
        "Lock in a choice from search_parts candidates for a given role. Caches "
        "the selection for the project so subsequent edits in the same role reuse it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {"type": "string"},
            "candidates": {
                "type": "array",
                "description": "Candidate list from search_parts; selection is by index or MPN.",
            },
            "selected_mpn": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["role", "selected_mpn"],
    },
}


def run(state, **inputs) -> dict:
    return {
        "status": "not_implemented",
        "hint": "Tier 3: pairs with search_parts.",
    }
