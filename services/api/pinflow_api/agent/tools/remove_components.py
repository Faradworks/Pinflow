"""Tool stub: remove_components."""

SCHEMA = {
    "name": "remove_components",
    "description": (
        "Delete components from the staged schematic. Orphaned labels are pruned "
        "automatically; the structural-diff validator runs after."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "refdeses": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Reference designators to delete, e.g. ['U1','C2','R3'].",
            },
        },
        "required": ["refdeses"],
    },
}


def run(state, **inputs) -> dict:
    return {
        "status": "not_implemented",
        "hint": "Will mutate the stage in place; needs staging integration + structural validator.",
    }
