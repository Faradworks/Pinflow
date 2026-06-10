"""Tool: resolve_mpn.

Two-mode tool for assigning an MPN to a component that lacks one:

- Lookup mode (no `confirmed_mpn`): inspect the design graph for the refdes,
  pull candidates from the Value field, and ask the model to confirm via
  ask_user before writing back.
- Writeback mode (`confirmed_mpn` set): write the property to the staged
  schematic via sch_properties.set_property, refresh the design graph.

Web-search candidate generation is deferred — the Value-field fallback
already covers the 2-chip cohort (TPS63020, RP2040) where users typically
put the MPN-with-suffix in the Value field.
"""

from __future__ import annotations

import re

from pinflow_api import staging
from pinflow_api.agent.schematic_sync import load_active_schematic
from pinflow_api.sch_properties import set_property

SCHEMA = {
    "name": "resolve_mpn",
    "description": (
        "Resolve an IC's MPN when its KiCad symbol doesn't carry one. "
        "Call without confirmed_mpn first to get a list of candidates from "
        "the Value field; then call ask_user to confirm; then call resolve_mpn "
        "again with the user's choice in confirmed_mpn to write it back to the "
        "staged schematic. Follow-up: ask_user(Confirm/Discard) + commit_edit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "refdes": {
                "type": "string",
                "description": "Reference designator of the IC, e.g. 'U1'.",
            },
            "confirmed_mpn": {
                "type": "string",
                "description": (
                    "Set this on the second call (after ask_user) to write the "
                    "confirmed MPN back to the staged schematic's symbol property."
                ),
            },
        },
        "required": ["refdes"],
    },
}


_MPN_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9\-]{2,}$", re.IGNORECASE)


def _candidates_from_value(value: str | None) -> list[str]:
    """Return MPN candidates derived from the component's Value field.

    Heuristics:
    - The raw Value when it looks like an MPN (alnum + hyphens, no whitespace).
    - A 'stripped' variant: drop the manufacturer package/reel suffix that
      KiCad's Digikey BOM extracts often include. e.g. 'TPS63020DSJR' has the
      cached profile keyed as 'TPS63020' — emit both so the lookup can hit.
    """
    if not value:
        return []
    raw = value.strip()
    if not raw or not _MPN_PATTERN.match(raw):
        return []
    out = [raw]
    # Strip a trailing run of letters off the digit-anchored core.
    m = re.match(r"^([A-Z]+[0-9]+)[A-Z]+$", raw, re.IGNORECASE)
    if m and m.group(1) not in out:
        out.append(m.group(1))
    return out


def run(
    state,
    refdes: str | None = None,
    confirmed_mpn: str | None = None,
    **_,
) -> dict:
    if not refdes:
        return {"status": "missing_input", "hint": "refdes is required."}
    if state.design_graph is None:
        return {
            "status": "no_design_graph",
            "hint": "Call read_active_schematic before resolve_mpn.",
        }
    comp = state.design_graph.components.get(refdes)
    if comp is None:
        return {"status": "no_such_refdes", "refdes": refdes}

    # Writeback mode --------------------------------------------------------
    if confirmed_mpn and confirmed_mpn.strip():
        confirmed = confirmed_mpn.strip()
        if state.active_sch_path is None:
            return {
                "status": "no_active_schematic",
                "hint": "Cannot write MPN: no active schematic in state.",
            }
        stage = staging.get(state.active_sch_path)
        source = (
            stage.working_copy if stage is not None
            else state.active_sch_path.read_text(encoding="utf-8")
        )
        try:
            new_source = set_property(source, refdes, "MPN", confirmed)
        except KeyError as e:
            return {"status": "no_such_refdes", "error": str(e)}
        staging.update(state.active_sch_path, new_source)
        load_active_schematic(state)
        return {
            "status": "ok",
            "refdes": refdes,
            "mpn": confirmed,
            "diff_available": True,
        }

    # Lookup mode -----------------------------------------------------------
    if comp.mpn:
        return {
            "status": "already_resolved",
            "refdes": refdes,
            "mpn": comp.mpn,
        }
    candidates = _candidates_from_value(comp.value)
    return {
        "status": "candidates" if candidates else "no_candidates",
        "refdes": refdes,
        "value": comp.value,
        "candidates": candidates,
        "hint": (
            "Call ask_user with these as options, then call resolve_mpn again "
            "with the user's choice in confirmed_mpn. If the user supplies a "
            "value not in this list, pass it as confirmed_mpn directly."
            if candidates
            else "Value field did not look like an MPN. Ask the user to supply one."
        ),
    }
