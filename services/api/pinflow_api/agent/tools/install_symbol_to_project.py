"""Tool: install_symbol_to_project.

Install a KiCad symbol that ISN'T in the bundled libraries into the user's
project, so the agent can place a part with no bundled symbol (e.g. a specific
LCSC connector/IC). Given an LCSC code, fetches the symbol via easyeda2kicad,
merges it into `<project_dir>/pinflow.kicad_sym`, and registers a `pinflow`
library in the project's `sym-lib-table`. Returns the project lib_id
(`pinflow:<symbol>`) plus the symbol's pins, so the next
`add_subcircuit_from_netlist` can reference it and wire it directly.

Distinct from `search_symbols`, which finds symbols ALREADY installed. Reach
for this only when `search_symbols` finds nothing and you have an LCSC code.

Stays local always (filesystem write) — never cloud-lifts.
"""

from __future__ import annotations

from pinflow_api import easyeda, symbol_resolver
from pinflow_api.agent.schematic_sync import load_active_schematic
from pinflow_api.sym_lib import (
    build_pinflow_lib,
    extract_symbol_text,
    merge_symbol_into_lib,
)
from pinflow_api.sym_lib_table import ensure_pinflow_entry

SCHEMA = {
    "name": "install_symbol_to_project",
    "description": (
        "Install a KiCad symbol that is NOT in the bundled libraries into the "
        "user's project so you can place it. Primary use: pass `lcsc_code` "
        "(e.g. 'C2040') — the symbol is fetched from LCSC via easyeda2kicad, "
        "merged into the project's pinflow.kicad_sym, and registered. Returns "
        "the lib_id to use in your netlist ('pinflow:<symbol>') plus its pins. "
        "Only use this when `search_symbols` finds no suitable bundled symbol "
        "AND you have an LCSC code (from search_parts/resolve_parts). For "
        "symbols that ARE bundled, just use the lib_id from search_symbols "
        "directly — no install needed. Idempotent; stays local."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lcsc_code": {
                "type": "string",
                "description": "LCSC part code to fetch + install, e.g. 'C2040'.",
            },
            "lib_id": {
                "type": "string",
                "description": (
                    "Alternative to lcsc_code: an existing on-disk lib_id "
                    "(bundled or easyeda-cached) to copy into the project. "
                    "Prefer lcsc_code."
                ),
            },
        },
        "required": [],
    },
}


def run(state, lcsc_code: str = "", lib_id: str = "", **_inputs) -> dict:
    lcsc_code = (lcsc_code or "").strip()
    lib_id = (lib_id or "").strip()
    if not lcsc_code and not lib_id:
        return {
            "status": "error",
            "error": "provide lcsc_code (preferred) or lib_id",
            "hint": (
                "Pass an LCSC code (e.g. from search_parts/resolve_parts) to "
                "fetch + install a symbol with no bundled equivalent."
            ),
        }

    # Need a project on disk to install into. Auto-recover like add_subcircuit.
    if state.active_sch_path is None:
        load_active_schematic(state)
    if state.active_sch_path is None:
        return {
            "status": "no_active_schematic",
            "hint": (
                "No KiCad project is open to install a symbol into. Ask the "
                "user to open a project in KiCad, then retry."
            ),
        }
    project_dir = state.active_sch_path.parent

    # Resolve the source: an on-disk lib_id we can extract a (symbol ...) from.
    if lcsc_code:
        try:
            fetched = easyeda.fetch_lcsc_symbol(lcsc_code)
        except RuntimeError as e:
            return {
                "status": "fetch_failed",
                "error": str(e),
                "hint": (
                    f"easyeda2kicad could not fetch {lcsc_code!r}. Verify the "
                    "LCSC code is correct (search_parts returns valid ones). "
                    "If it persists, the part may not have an EasyEDA symbol."
                ),
            }
        source_lib_id = f"{fetched.lib_path.stem}:{fetched.symbol_name}"
        symbol_name = fetched.symbol_name
    else:
        source_lib_id = lib_id
        symbol_name = lib_id.partition(":")[2] or lib_id

    try:
        symbol_text = extract_symbol_text(source_lib_id)
    except (FileNotFoundError, KeyError, ValueError) as e:
        return {
            "status": "extract_failed",
            "error": str(e),
            "hint": (
                f"Could not extract the symbol from {source_lib_id!r}. If you "
                "passed lib_id, confirm it exists (search_symbols)."
            ),
        }

    # Merge into <project_dir>/pinflow.kicad_sym and register the library.
    pinflow_lib = project_dir / "pinflow.kicad_sym"
    if pinflow_lib.is_file():
        merged = merge_symbol_into_lib(pinflow_lib.read_text(), symbol_text)
        lib_action = "merged"
    else:
        merged = build_pinflow_lib([symbol_text])
        lib_action = "created"
    pinflow_lib.write_text(merged)
    table_action = ensure_pinflow_entry(project_dir / "sym-lib-table")

    project_lib_id = f"pinflow:{symbol_name}"

    # Hand back the pins so the next add_subcircuit_from_netlist references
    # real pin numbers instead of guessing (this symbol is brand-new to the
    # model — it has no prior knowledge of its pinout).
    pins = None
    located = symbol_resolver._locate_lib_id(project_lib_id, project_dir)
    if located is not None:
        rows = symbol_resolver.get_symbol_pins(located)
        if rows:
            pins = [f"{r['number']} ({r['name']})" for r in rows]

    return {
        "status": "ok",
        "lib_id": project_lib_id,
        "symbol": symbol_name,
        "lib_action": lib_action,
        "table_action": table_action,
        "pins": pins,
        "hint": (
            f"Installed. Use lib_id {project_lib_id!r} in your netlist. "
            "Reference pins by the NUMBER before the parenthesised name in "
            "`pins` (e.g. 'A9' for 'A9 (VBUS)')."
        ),
    }
