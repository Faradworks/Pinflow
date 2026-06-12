"""Tool: add_subcircuit_from_netlist.

The placer entry point for the replicate / generate flows. Takes a position-
free netlist (parts + nets), applies optional `port_bindings` to rename
boundary nets, resolves any unresolved lib_ids against the design graph,
runs the deterministic placer, validates the output, merges into the user's
active schematic via `emit.layout.merge_subcircuit`, and writes through the
staging layer. Caller invokes `commit_edit` / `discard_edit` after user
confirmation.

Pass-2 (LLM refiner) is not wired here yet — once the structural-diff
validator gates it, the refiner runs between `place(...)` and merge.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from pinflow_api import staging, symbol_resolver
from pinflow_api.agent.schematic_sync import load_active_schematic
from pinflow_api.agent.tools._subcircuit_common import load_target_schematic
from pinflow_api.builders._common import sch_to_string
from pinflow_api.emit import layout
from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.placers import PlacerError, get_placer
from pinflow_api.emit.structural_diff import validate_placer_output

SCHEMA = {
    "name": "add_subcircuit_from_netlist",
    "description": (
        "Place + stage a subcircuit given a netlist (parts + nets, no "
        "positions). Used for replicate flows (paired with extract_subgraph) "
        "and for generate flows (paired with parse_datasheet, which returns "
        "the netlist payload to feed into here). port_bindings remaps "
        "netlist port names to existing schematic nets — typical use: rename "
        "'+3V3' to '+4V5' for a duplicated regulator block. After this tool "
        "succeeds, follow up with ask_user(['Confirm','Discard']) then "
        "commit_edit / discard_edit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "netlist": {
                "type": "object",
                "description": (
                    'Netlist payload: {"parts": [...], "nets": [...]}.\n'
                    'parts[]: {"refdes": "U1"|"R3"|"J1", "lib_id": "Device:R", '
                    '"value"?: "5.1k"}. Use the lib_id EXACTLY as returned by '
                    "search_symbols / install_symbol_to_project — never guess.\n"
                    'nets[]: {"name": "GND", "endpoints": [{"ref": "<refdes>", '
                    '"pin": "<pin-number>"}, ...], "is_port"?: true}. The '
                    "endpoints ARE the wiring — a net is the set of pins tied "
                    "together. `pin` is the pin NUMBER (the token before the "
                    'parenthesised name in a search/install pin list, e.g. "A5" '
                    'for "A5 (CC1)", or "2" for a resistor). Every non-port net '
                    "MUST list ≥2 endpoints; naming a net with no endpoints "
                    "wires NOTHING and is rejected. Mark rails and exposed "
                    "boundary signals is_port:true (port_bindings can rename "
                    "them). Name power rails conventionally (GND, +5V, +3V3, "
                    "VBUS, VIN, VOUT) — they're auto-detected and drawn with "
                    "power symbols; for an unconventionally named rail set "
                    '"is_power": true on the net explicitly.\n'
                    "Example — a 5.1k pulldown from a USB-C CC1 pin to GND: "
                    '{"parts":[{"refdes":"J1","lib_id":"Connector:'
                    'USB_C_Receptacle_USB2.0_16P"},{"refdes":"R1","lib_id":'
                    '"Device:R","value":"5.1k"}],"nets":[{"name":"CC1",'
                    '"endpoints":[{"ref":"J1","pin":"A5"},{"ref":"R1","pin":'
                    '"1"}]},{"name":"GND","is_port":true,"endpoints":[{"ref":'
                    '"R1","pin":"2"}]}]}'
                ),
            },
            "port_bindings": {
                "type": "object",
                "description": (
                    "Map of netlist port name → existing schematic net name. "
                    "Renames are applied to is_port=True nets only. Internal "
                    "nets are unchanged even if their name appears as a key."
                ),
                "additionalProperties": {"type": "string"},
            },
            "label": {
                "type": "string",
                "description": (
                    "Optional label for the rectangle drawn around the "
                    "placed block. Defaults to 'Subcircuit'."
                ),
            },
        },
        "required": ["netlist"],
    },
}


def _is_generate_netlist(nl: Netlist, state) -> bool:
    """True for the datasheet-generate path, False for replicate.

    Both paths now place + wire via `place()`; this only gates whether the
    netlist is retained in `state.staged_netlists` (the generate netlist
    carries `design_spec`-baked LCSC search hints that `resolve_parts` reads
    later).

    `design_spec` is the only step that writes `state.pending_netlists`, and
    only the generate chain (parse_datasheet → design_spec → here) goes
    through it. design_spec stashes the synthesized netlist keyed by MPN and
    instructs the model to pass it here verbatim, so its IC part still carries
    that MPN. Replicate netlists come from `extract_subgraph` (the user's
    already-wired schematic) and have no `pending_netlists` entry.

    Consequence of a miss: a replicate of a block that happens to reuse an
    MPN design_spec'd earlier this conversation would also have its netlist
    retained in `staged_netlists` — harmless; `resolve_parts` simply finds no
    baked search hints and uses its fallback. Not corruption. Fine for MVP.
    """
    pending = state.pending_netlists or {}
    if not pending:
        return False
    if any(p.mpn and p.mpn in pending for p in nl.parts):
        return True
    norm = nl.model_dump()
    return any(norm == pv for pv in pending.values())


# "pin 'VBUS' not found on J1 (Connector:USB_C_Receptacle_PowerOnly_6P)"
_PIN_NOT_FOUND_RE = re.compile(r"not found on \S+ \(([^)]+)\)")

# kicad-sch-api only accepts standard KiCad references: alpha prefix + digits
# (U1, C3, J2, FB1). Descriptive refdes the model likes to emit ("U_LDO",
# "C_VIN1", "R_SCL") are rejected at add() time with "Invalid reference
# format", which surfaces as an opaque "IC <ref> failed to place".
_VALID_REFDES_RE = re.compile(r"^[A-Za-z]+[0-9]+$")


def _normalize_refdes(netlist: Netlist) -> dict[str, str]:
    """Rewrite refdes that kicad-sch-api would reject into valid standard form
    (`U_LDO`→`U1`, `C_VIN1`→`C1`), collision-free within the netlist. Returns
    old→new for logging. Endpoints reference nets by name, not refdes, so only
    `part.refdes` changes — connectivity is untouched. Schematic-level
    collisions are handled later by the merge step's renumbering.
    """
    used: dict[str, set[int]] = {}
    for p in netlist.parts:
        if _VALID_REFDES_RE.match(p.refdes):
            i = len(p.refdes)
            while i > 0 and p.refdes[i - 1].isdigit():
                i -= 1
            used.setdefault(p.refdes[:i], set()).add(int(p.refdes[i:]))

    renames: dict[str, str] = {}
    for p in netlist.parts:
        if _VALID_REFDES_RE.match(p.refdes):
            continue
        prefix = ""
        for c in p.refdes:
            if c.isalpha():
                prefix += c
            else:
                break
        prefix = prefix or "U"
        nums = used.setdefault(prefix, set())
        n = 1
        while n in nums:
            n += 1
        nums.add(n)
        new_ref = f"{prefix}{n}"
        renames[p.refdes] = new_ref
        p.refdes = new_ref

    # Net endpoints reference parts by `ref`; rewrite them so connectivity
    # survives the rename (the Netlist coercion may have populated endpoints
    # from parts[].pins using the pre-rename refdes).
    if renames:
        for net in netlist.nets:
            for ep in net.endpoints:
                if ep.ref in renames:
                    ep.ref = renames[ep.ref]
    return renames


def _placer_failed(errors: list[str], state) -> dict:
    """Build a placer_failed result, enriching pin-not-found errors with the
    symbol's actual pins so the model corrects in one retry instead of
    guessing pin names. Netlist endpoints reference pins by NUMBER (see
    `_pin_xy`), so we surface both number and name."""
    result: dict = {"status": "placer_failed", "errors": errors}

    lib_ids = {
        m.group(1) for e in errors if (m := _PIN_NOT_FOUND_RE.search(str(e)))
    }
    if not lib_ids:
        return result

    project_dir = state.active_sch_path.parent if state.active_sch_path else None
    pin_map: dict[str, list[str]] = {}
    for lib_id in lib_ids:
        resolved = symbol_resolver._locate_lib_id(lib_id, project_dir)
        if resolved is None:
            continue
        pins = symbol_resolver.get_symbol_pins(resolved)
        if pins:
            pin_map[lib_id] = [f"{p['number']} ({p['name']})" for p in pins]

    if pin_map:
        result["available_pins"] = pin_map
        result["hint"] = (
            "A pin in your netlist doesn't exist on the symbol. Netlist "
            "endpoints must reference pins by NUMBER — the value BEFORE the "
            "parenthesised name in available_pins (e.g. for an entry "
            "'A9 (VBUS)' the endpoint pin is 'A9', not 'VBUS'). Rewrite the "
            "offending endpoint pin fields using numbers from available_pins "
            "and retry. Do NOT guess pin names."
        )
    return result


def _resolve_lib_ids(netlist: Netlist, state) -> tuple[Netlist, list[str]]:
    """Confirm every part's lib_id is loadable from disk.

    Most lib_ids come from extract_subgraph (already on disk in the user's
    project). For LLM-generated netlists (future), the lib_id may need a
    re-resolve via the design graph or bundled libs. Returns the netlist
    unchanged (lib_ids are kept) plus a list of refdeses we couldn't locate.
    """
    unresolved: list[str] = []
    project_dir = (
        state.active_sch_path.parent if state.active_sch_path else None
    )
    for part in netlist.parts:
        # If the lib_id resolves directly off the design graph (already
        # placed in the active schematic), no work needed.
        located = symbol_resolver._locate_lib_id(part.lib_id, project_dir)
        if located is not None:
            continue
        # Try a name-match against placed components in the design graph
        # (handles e.g. value-based match if mpn missing).
        chip_hint = part.mpn or part.value or part.lib_id.split(":")[-1]
        resolved = symbol_resolver.resolve_from_design_graph(
            chip_name=chip_hint,
            design_graph=state.design_graph,
            project_dir=project_dir,
        )
        if resolved is not None:
            continue
        unresolved.append(part.refdes)
    return netlist, unresolved


def run(
    state,
    netlist: dict | None = None,
    port_bindings: dict | None = None,
    label: str | None = None,
    **_,
) -> dict:
    if not netlist:
        return {"status": "missing_input", "hint": "netlist is required."}
    if state.active_sch_path is None:
        # Auto-recover instead of bouncing the model back for an explicit
        # read_active_schematic: load it ourselves (same loader the read tool
        # uses, idempotent). Only fail if there genuinely is no open schematic.
        load_active_schematic(state)
    if state.active_sch_path is None:
        return {
            "status": "no_active_schematic",
            "hint": (
                "No KiCad schematic is open to stage edits into. Ask the user "
                "to open a project in KiCad, then retry."
            ),
        }

    try:
        nl = Netlist.model_validate(netlist)
    except ValidationError as e:
        errs = e.errors()
        hint = (
            "Netlist failed schema validation. Shape: "
            '{"parts":[{"refdes","lib_id","value"?}], '
            '"nets":[{"name","endpoints":[{"ref","pin"}]}]}. '
            "Each part needs `lib_id` (NOT `symbol`/`footprint`) — get it "
            "from search_symbols. Each endpoint's `pin` is the pin NUMBER. "
            "Fix the fields flagged in errors and retry."
        )
        return {"status": "bad_netlist", "errors": errs, "hint": hint}

    self_errors = nl.validate_self()
    if self_errors:
        return {
            "status": "bad_netlist",
            "errors": self_errors,
            "hint": (
                "The netlist is structurally invalid. A net listed in `nets` "
                "must have ≥2 `endpoints` that reference real part pins "
                "({ref: refdes, pin: pin-number}). 'has no endpoints' means "
                "you declared a net nothing connects to — either add the "
                "endpoints that belong on it or remove the net. Do not invent "
                "net names without wiring them. Example of a correctly wired "
                'net: {"name":"CC1","endpoints":[{"ref":"J1","pin":"A5"},'
                '{"ref":"R1","pin":"1"}]}. Fix and retry.'
            ),
        }

    # Coerce descriptive refdes (U_LDO, C_VIN1) into valid KiCad references
    # before they reach the placer — ksa rejects them at add() time, which
    # otherwise surfaces as an opaque "IC <ref> failed to place".
    _normalize_refdes(nl)

    # Both paths place + wire via `place()`. Provenance only decides whether
    # to retain the netlist in `staged_netlists` — the generate netlist
    # carries design_spec-baked LCSC search hints `resolve_parts` reads later.
    # Decide before port_bindings so the comparison is against design_spec's
    # stashed shape.
    is_generate = _is_generate_netlist(nl, state)

    nl = nl.with_port_bindings(port_bindings or {})

    _bound_netlist, unresolved = _resolve_lib_ids(nl, state)
    if unresolved:
        return {
            "status": "no_symbol",
            "refdeses": unresolved,
            "hint": (
                f"Could not locate KiCad symbols for {unresolved}. They may "
                "live only in a schematic's embedded (lib_symbols ...) — "
                "install via install_symbol_to_project, or supply LCSC codes "
                "via the next user message."
            ),
        }

    # Pass the active schematic as `source_schematic` for greedy (auto-
    # gated for dense ICs) — the engine lifts project-local lib_symbols
    # from there. cplace ignores the kwarg.
    placer_kw = {}
    if state.active_sch_path is not None:
        placer_kw["source_schematic"] = str(state.active_sch_path)
    try:
        result = get_placer()(nl, title=label or "Subcircuit", **placer_kw)
    except PlacerError as e:
        return _placer_failed(e.errors, state)
    except Exception as e:
        return _placer_failed([f"{type(e).__name__}: {e}"], state)

    vr = validate_placer_output(nl, result)
    if not vr.ok:
        return {
            "status": "validation_failed",
            "errors": vr.errors,
            "warnings": vr.warnings,
            "hint": (
                "Placer output didn't survive file round-trip. Schematic NOT "
                "staged. Two common causes — check the errors above first: "
                "(a) NON-STANDARD REFDES — every part must use a standard "
                "single-letter prefix + number (R1, R2, C1, U1, J1, D1, L1, "
                "Q1, FB1). Descriptive names like 'R_SCL', 'C_OLED', "
                "'PWR_3V3' get classified as UNKNOWN and dropped. Rename and "
                "retry. (b) NON-DETERMINISTIC PLACER — if refdeses are all "
                "standard and the error mentions a part still missing, retry "
                "the SAME call once (identical netlist + bindings + label). "
                "If it fails twice with identical valid input, surface the "
                "errors to the user via plain text and stop."
            ),
        }

    target_sch, target_source_text = load_target_schematic(state.active_sch_path)
    try:
        placement = layout.merge_subcircuit(
            target_sch=target_sch,
            new_sch_text=result.sch_text,
            label=label or "subcircuit",
        )
    except Exception as e:
        return {"status": "merge_failed", "error": f"{type(e).__name__}: {e}"}

    try:
        merged_text = sch_to_string(
            target_sch, preserve_lib_symbols_from=target_source_text
        )
    except Exception as e:
        return {"status": "serialize_failed", "error": f"{type(e).__name__}: {e}"}

    staging.update(state.active_sch_path, merged_text)
    # Record the placed block's bbox so the viewer outlines the whole new
    # block (preview-only). `skipped` placements (empty netlist) carry no bbox.
    block_bbox = placement.get("block_bbox")
    if block_bbox is not None:
        staging.add_block_region(state.active_sch_path, block_bbox)
    # Refresh the design graph so subsequent turns see the new components.
    load_active_schematic(state)

    out: dict = {
        "status": "ok",
        "mode": "wired",
        "placement": placement,
        "parts_added": [p.refdes for p in nl.parts],
        "warnings": result.issues + vr.warnings,
        "diff_available": True,
    }
    if is_generate:
        # Retain the position-free netlist so `resolve_parts` can recover the
        # design_spec-baked LCSC search hints (search_query / min_voltage)
        # carried on each part.
        state.staged_netlists[str(state.active_sch_path)] = nl.model_dump()
    return out
