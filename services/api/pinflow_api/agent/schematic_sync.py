"""Load / refresh the active schematic's design graph into conversation state.

Shared between the `read_active_schematic` tool (explicit call by the model)
and `build_context_block` (auto-refresh when the file's mtime advances —
i.e. the user saved in KiCad between turns). Keeping the heavy lifting
here means there's a single code path that knows how to read the schematic.

Refresh policy:
- First contact (no `active_sch_path` on state): do nothing here; let the
  agent decide to call `read_active_schematic`. That keeps "open KiCad,
  start chatting" deterministic — the first turn surfaces a stub digest
  with the nudge to call the tool, instead of silently shelling out before
  the model has even spoken.
- Subsequent turns: if the real file's mtime advanced past what we
  recorded, reload — silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pinflow_api import staging
from pinflow_api.graph import build_design_graph
from pinflow_api.kicad_cli import export_netlist
from pinflow_api.kicad_detect import detect
from pinflow_api.netlist import parse_kicadsexpr
from pinflow_api.profile import load_cached
from pinflow_api.sch_properties import get_mpn, parse_properties

if TYPE_CHECKING:
    from pinflow_api.agent.state import ConversationState


def load_active_schematic(state: "ConversationState") -> dict:
    """Detect the active project, build the design graph, stash it on state.

    Returns a status dict suitable for direct tool-result use:
    `{status: "ok", ...}` or `{status: "no_project" | "no_schematic" | ...}`.
    On non-`ok` outcomes, state is NOT modified — callers that care can
    surface the hint to the user.
    """
    # Pinned path (trace_chat --sch): sandbox the run to an explicit file and
    # SKIP live detection. Critical for safety — otherwise detect() resolves to
    # whatever KiCad has open and the edit/commit path would mutate the user's
    # real project during a debug run. See state.forced_sch_path.
    if state.forced_sch_path is not None:
        real_path = Path(state.forced_sch_path)
        if not real_path.is_file():
            return {"status": "not_found", "hint": f"pinned schematic file missing: {real_path}"}
        proj_name = real_path.stem
    else:
        proj = detect()
        if proj is None:
            return {
                "status": "no_project",
                "hint": "KiCad does not appear to be running, or no project is open.",
            }
        if not proj.path or not proj.schematic:
            return {
                "status": "no_schematic",
                "hint": (
                    f"Detected project '{proj.name or '?'}' but could not resolve the "
                    "active .kicad_sch path. Open the schematic in KiCad and retry."
                ),
            }

        real_path = Path(proj.path).parent / proj.schematic
        if not real_path.is_file():
            return {"status": "not_found", "hint": f"schematic file missing: {real_path}"}
        proj_name = proj.name

    stage = staging.get(real_path)
    read_path = stage.temp_path if (stage is not None and stage.temp_path.is_file()) else real_path

    try:
        netlist_text = export_netlist(read_path)
    except Exception as e:
        return {"status": "netlist_failed", "error": str(e)}

    netlist = parse_kicadsexpr(netlist_text)

    try:
        props_by_ref = parse_properties(read_path)
        prop_warning = None
    except Exception as e:
        props_by_ref = {}
        prop_warning = f"property parse failed: {e}"

    profiles_by_mpn: dict = {}
    for refdes, props in props_by_ref.items():
        mpn = get_mpn(props)
        if not mpn or mpn in profiles_by_mpn:
            continue
        cached = load_cached(mpn)
        if cached is not None:
            profiles_by_mpn[mpn] = cached

    graph = build_design_graph(netlist, props_by_ref, profiles_by_mpn)

    state.active_sch_path = real_path
    state.project_name = proj_name
    state.design_graph = graph
    state.profiles_by_mpn = profiles_by_mpn
    # Record the *real* file's mtime — that's what changes when the user
    # saves in KiCad. The staged read_path mtime would only track our
    # own writes, which isn't the freshness signal we want.
    try:
        state.schematic_mtime = real_path.stat().st_mtime
    except OSError:
        state.schematic_mtime = None
    state.stage_stale = bool(stage and stage.is_stale())

    result = {
        "status": "ok",
        "project": proj_name,
        "schematic_path": str(real_path),
        "reading_from": "stage" if stage is not None else "real",
        "stage_stale": state.stage_stale,
        "components": len(graph.components),
        "nets": len(graph.nets),
        "profiles_loaded": list(profiles_by_mpn.keys()),
    }
    if prop_warning:
        result["warning"] = prop_warning
    return result


def refresh_if_stale(state: "ConversationState") -> bool:
    """Silent reload if the active .kicad_sch has changed since last load.

    Returns True when a reload happened. No-op (returns False) if:
      - state has no cached graph yet (first contact — let the model call
        `read_active_schematic` explicitly),
      - the file is missing,
      - mtime hasn't advanced.
    """
    if state.design_graph is None or state.active_sch_path is None:
        return False
    try:
        cur_mtime = state.active_sch_path.stat().st_mtime
    except OSError:
        return False
    if state.schematic_mtime is not None and cur_mtime <= state.schematic_mtime:
        # Even when the file is unchanged, the stage may have become stale
        # under us (kicad-cli on disk hasn't changed, but the staged temp
        # is now older than something else). is_stale() reads the real
        # file's mtime directly so it's correct either way; re-evaluate
        # cheaply so the context block reflects current truth.
        stage = staging.get(state.active_sch_path)
        state.stage_stale = bool(stage and stage.is_stale())
        return False
    # Mtime advanced — full reload.
    load_active_schematic(state)
    return True
