"""Tool registry — schemas frozen here become part of the system prompt
the agent loop ships to Claude every turn.

`TOOL_SCHEMAS` is the list passed to `messages.create(tools=...)`.
`DISPATCH` maps tool name → run(state, **inputs).

`ask_user` is dispatched inline by the loop (it suspends the conversation),
but its schema is in TOOL_SCHEMAS so the model knows it exists.
"""

from __future__ import annotations

from . import (
    add_subcircuit_from_netlist,
    ask_user,
    commit_edit,
    design_spec,
    discard_edit,
    edit_property,
    extract_subgraph,
    get_component_profile,
    install_symbol_to_project,
    parse_datasheet,
    plan_block_diagram,
    read_active_schematic,
    read_datasheet_section,
    remove_components,
    resolve_mpn,
    resolve_parts,
    run_erc,
    search_parts,
    search_symbols,
    select_part,
)

_MODULES = [
    plan_block_diagram,
    add_subcircuit_from_netlist,
    extract_subgraph,
    remove_components,
    edit_property,
    read_active_schematic,
    ask_user,
    resolve_mpn,
    get_component_profile,
    parse_datasheet,
    design_spec,
    commit_edit,
    discard_edit,
    run_erc,
    search_parts,
    search_symbols,
    select_part,
    resolve_parts,
    read_datasheet_section,
    install_symbol_to_project,
]

TOOL_SCHEMAS: list[dict] = [m.SCHEMA for m in _MODULES]
DISPATCH: dict[str, callable] = {m.SCHEMA["name"]: m.run for m in _MODULES}

assert len(TOOL_SCHEMAS) == 20, (
    "expected exactly 20 tools — search_symbols was added next to search_parts: "
    "it keyword-searches the installed KiCad symbol libraries so the model can "
    "discover real lib_ids instead of guessing (the no_symbol thrash)."
)
assert len(DISPATCH) == len(TOOL_SCHEMAS), "duplicate tool name in registry"
