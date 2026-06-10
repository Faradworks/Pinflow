"""Tool: read_active_schematic — the agent's eyes.

Thin wrapper around `agent.schematic_sync.load_active_schematic`. The same
loader powers the silent mtime-based refresh in `build_context_block`, so
the read path is identical whether the model called the tool explicitly
or the file changed under us between turns.
"""

from __future__ import annotations

from pinflow_api.agent.schematic_sync import load_active_schematic
from pinflow_api.digest import build_digest

SCHEMA = {
    "name": "read_active_schematic",
    "description": (
        "Return a compact digest of the user's currently-open KiCad schematic "
        "(staged working copy if any, else the real file). Components with "
        "refdes/value/footprint/MPN, nets grouped by power vs signal, the "
        "per-IC neighborhood (decoupling caps, bridges, power-rail membership). "
        "Call this on first contact with a project; afterwards the digest "
        "auto-refreshes when the user saves in KiCad."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def run(state, **_inputs) -> dict:
    result = load_active_schematic(state)
    if result.get("status") != "ok":
        return result

    digest = build_digest(
        state.design_graph,
        state.profiles_by_mpn,
        project_name=state.project_name,
        schematic_basename=state.active_sch_path.name if state.active_sch_path else None,
        stage_stale=state.stage_stale,
    )
    return {**result, "digest": digest}
