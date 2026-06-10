"""Builds the always-on context block prepended to every LLM turn.

Behavior:
- If state has no cached design graph, surface the "call read_active_schematic"
  nudge. First contact stays explicit so the model speaks before we shell
  out to kicad-cli.
- If state has a graph, silently refresh from disk if the .kicad_sch mtime
  has advanced (the user saved in KiCad between turns), then render the
  digest. mtime-cheap in the common case (~one stat per turn).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pinflow_api.agent.schematic_sync import refresh_if_stale
from pinflow_api.digest import build_digest

if TYPE_CHECKING:
    from pinflow_api.agent.state import ConversationState


def build_context_block(state: "ConversationState | None" = None) -> str:
    if state is None or state.design_graph is None:
        return (
            "Active schematic: (unknown — call `read_active_schematic` to load the "
            "current project, build the design graph, and refresh this digest).\n"
        )

    refresh_if_stale(state)

    basename = state.active_sch_path.name if state.active_sch_path else None
    return build_digest(
        state.design_graph,
        state.profiles_by_mpn,
        project_name=state.project_name,
        schematic_basename=basename,
        stage_stale=state.stage_stale,
    )
