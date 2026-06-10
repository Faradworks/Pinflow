"""Tool: discard_edit.

Drops the staged working copy without writing to disk. Used when the user
rejects the proposed edit via the UI.
"""

from __future__ import annotations

from pinflow_api import staging

SCHEMA = {
    "name": "discard_edit",
    "description": (
        "Drop the staged schematic without writing to disk. Reverts the agent's "
        "in-progress edit. Used when the user rejects the proposed change."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def run(state, **_) -> dict:
    if state.active_sch_path is None:
        return {
            "status": "no_active_schematic",
            "hint": "Call read_active_schematic before discard_edit.",
        }

    dropped = staging.discard(state.active_sch_path)
    state.stage_stale = False
    if not dropped:
        return {"status": "no_stage", "hint": "Nothing was staged."}
    return {"status": "ok"}
