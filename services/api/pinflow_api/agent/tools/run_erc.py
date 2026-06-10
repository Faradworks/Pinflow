"""Tool: run_erc.

Runs `kicad-cli sch erc` against the staged working copy (if any) or the
real `.kicad_sch`. Filters out the two rules that are inherent to
subcircuits-without-context — same exclusion list as `llm_emit` uses during
its repair loop — so the agent sees only actionable violations.
"""

from __future__ import annotations

from pinflow_api import kicad_cli, staging

SCHEMA = {
    "name": "run_erc",
    "description": (
        "Run KiCad ERC against the staged schematic (or the real .kicad_sch "
        "if no stage exists) and return any actionable violations. Useful "
        "after edits to verify nothing broke before commit."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

# Rules excluded from "actionable" — power-pin connectivity warnings fire
# whenever a subcircuit is viewed in isolation from upstream rails, which is
# almost always when the agent is mid-edit.
_EXTERNAL_RULES = ("power_pin_not_driven", "pin_not_driven")


def run(state, **_) -> dict:
    if state.active_sch_path is None:
        return {
            "status": "no_active_schematic",
            "hint": "Call read_active_schematic before running ERC.",
        }

    stage = staging.get(state.active_sch_path)
    read_path = (
        stage.temp_path
        if (stage is not None and stage.temp_path.is_file())
        else state.active_sch_path
    )

    try:
        sch_text = read_path.read_text(encoding="utf-8")
        report = kicad_cli.run_erc(sch_text)
    except Exception as e:
        return {"status": "erc_failed", "error": f"{type(e).__name__}: {e}"}

    actionable = report.filtered(exclude_rules=_EXTERNAL_RULES)
    return {
        "status": "ok",
        "reading_from": "stage" if stage is not None else "real",
        "total": report.total,
        "actionable": len(actionable),
        "violations": [v.model_dump() for v in actionable],
    }
