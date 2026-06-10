"""LLM-driven netlist synthesis from a cached MPN profile.

Given a chosen variant's pintable + the resolved symbol's actual pin shape +
recommended-application passives + optional role/vin/vout hints, asks Claude
(forced tool-use, no PDF) to emit a `Netlist` consumable by
`pinflow_api.emit.netlist_to_sch.place`. The placer then merges it into the
user's schematic via `add_subcircuit_from_netlist`.

Mirrors `datasheet_parse.py`'s shape: tool with input_schema flattened from
the Pydantic model, forced tool_choice, single tool_use block extracted from
the response and validated through Pydantic. Reuses `_flatten_schema`.
"""

from __future__ import annotations

import json
from typing import Optional

from pinflow_api import llm

from pinflow_api.datasheet_parse import Pin, RecommendedPassive, _flatten_schema
from pinflow_api.emit.netlist import Netlist
from pinflow_api.settings import settings


class NetlistSynthError(RuntimeError):
    """Raised when the model fails to emit a valid Netlist."""

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


_SYSTEM = (
    "You synthesize a KiCad netlist for the recommended-application "
    "subcircuit of a specific IC variant. Output is a flat list of parts + "
    "nets — no positions, no wires, no labels. The placer handles geometry.\n"
    "\n"
    "Hard rules:\n"
    "  - The `value` of every recommended_passive is PRE-COMPUTED and "
    "AUTHORITATIVE — emit each part with that value string verbatim. Do not "
    "re-estimate, round, or substitute passive values.\n"
    "  - Refdes the IC as `U1` and set its `lib_id` to the EXACT value given "
    "in `symbol_lib_id`. Set `value` to the orderable part number.\n"
    "  - For each recommended_passive, emit a part with a sensible refdes "
    "(`C1`, `C2`, …, `R1`, `R2`, …, `L1`, `Y1`). Use these bundled lib_ids: "
    "`Device:C` for capacitors, `Device:R` for resistors, `Device:L` for "
    "inductors, `Device:Crystal_GND24` (2-pin crystal) for `Y` parts, "
    "`Device:D` for diodes. Set `value` to the passive's value string "
    "(e.g. '100nF', '10k').\n"
    "  - Every NetlistEndpoint.pin number must EXIST: for U1 it must be in "
    "the `symbol_pins` list provided to you; for two-terminal passives it "
    "must be '1' or '2' (KiCad's `Device:*` symbols use these). Never "
    "invent pin numbers. If the datasheet's pin number for a passive is "
    "ambiguous, prefer wiring passive pin 1 to the chip pin and passive "
    "pin 2 to ground / the other side.\n"
    "  - Net naming: power and ground rails use `GND`, `VIN`, `VOUT`, `+3V3`, "
    "`+5V`, etc. Set `is_power: true` on these. Internal nets (e.g. switching "
    "node between inductor and IC `SW`, feedback divider midpoint) get short "
    "uppercase names like `SW`, `FB`. Use `_` for multi-word names.\n"
    "  - Ports: any net the surrounding schematic will plug into should have "
    "`is_port: true`. Default ports are `VIN`, `VOUT`, `GND` (when those "
    "rails exist). If `port_bindings` is supplied, use the bound name "
    "verbatim instead of the default.\n"
    "  - Don't drop chip pins. If a pin has no recommended-application "
    "connection, give it its own net named after the pin (e.g. an unused "
    "GPIO becomes a 1-endpoint net). The placer drops a label at the pin "
    "either way; better to surface the pin than silently swallow it.\n"
    "Always call the `submit_netlist` tool — never reply with prose."
)


def synthesize_netlist(
    *,
    mpn: str,
    variant_code: Optional[str],
    orderable_part: Optional[str],
    pintable: list[Pin],
    recommended_passives: list[RecommendedPassive],
    symbol_lib_id: str,
    symbol_pins: list[dict],
    role: Optional[str] = None,
    vin: Optional[str] = None,
    vout: Optional[str] = None,
    port_bindings: Optional[dict[str, str]] = None,
) -> Netlist:
    """LLM-B: emit a `Netlist` for this MPN/variant.

    Raises `NetlistSynthError` if the model didn't call the tool or the
    returned netlist fails `validate_self()`.
    """
    if not llm.available():
        raise RuntimeError(llm.NOT_CONFIGURED_MSG)

    client = llm.make_client()

    tool = {
        "name": "submit_netlist",
        "description": "Submit the synthesized netlist for the subcircuit.",
        "input_schema": _flatten_schema(Netlist.model_json_schema()),
    }

    payload = {
        "mpn": mpn,
        "variant_code": variant_code,
        "orderable_part": orderable_part or mpn,
        "symbol_lib_id": symbol_lib_id,
        "symbol_pins": symbol_pins,
        "datasheet_pintable": [p.model_dump() for p in pintable],
        "recommended_passives": [rp.model_dump() for rp in recommended_passives],
        "role": role,
        "vin": vin,
        "vout": vout,
        "port_bindings": port_bindings or {},
    }

    user_text = (
        "Synthesize the recommended-application subcircuit for the following "
        "IC + variant. Use only the pin numbers present in `symbol_pins`. "
        "Apply `port_bindings` to rename default port nets when provided.\n\n"
        f"```json\n{json.dumps(payload, indent=2)}\n```"
    )

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8192,
        system=_SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_netlist"},
        messages=[{"role": "user", "content": user_text}],
    )

    tool_input: Optional[dict] = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_netlist":
            tool_input = block.input
            break

    if tool_input is None:
        raise NetlistSynthError(
            "model did not call submit_netlist",
            detail={"stop_reason": response.stop_reason},
        )

    try:
        netlist = Netlist.model_validate(tool_input)
    except Exception as e:
        raise NetlistSynthError(
            f"Netlist validation failed: {type(e).__name__}: {e}",
            detail={"raw": tool_input},
        ) from e

    errors = netlist.validate_self()
    if errors:
        raise NetlistSynthError(
            "Netlist.validate_self() returned errors",
            detail={"errors": errors, "raw": tool_input},
        )

    return netlist
