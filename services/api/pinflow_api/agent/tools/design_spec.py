"""Tool: design_spec — deterministic design abstract between profile and netlist.

Call after `parse_datasheet` returns `status:"profile_ready"`. Runs the
deterministic equation pass (`pinflow_api.equations` via
`pinflow_api.design_spec.build_design_spec`) to size the feedback divider,
main inductor, and in/out caps for the chosen topology, OVERRIDING the
datasheet's LLM-guessed values for those parts. Emits a reviewable
`DesignSpec` (the loop renders it as a `DesignSpecCard`, mirroring
`plan_block_diagram`) and synthesizes a spec-driven `Netlist` via
`netlist_synth`. The model then runs the Confirm/Discard gate and, on
Confirm, hands the returned netlist to `add_subcircuit_from_netlist`.
"""

from __future__ import annotations

import math
import re
from typing import Optional

from pinflow_api import netlist_synth
from pinflow_api.design_spec import DesignSpec, normalize_value_str
from pinflow_api.design_spec import build_design_spec
from pinflow_api.emit.netlist import Netlist
from pinflow_api.equations import TOPOLOGIES

SCHEMA = {
    "name": "design_spec",
    "description": (
        "Step 2 of the subcircuit chain. Call after parse_datasheet returns "
        "status:'profile_ready'. Computes deterministic component values "
        "(feedback divider, inductor, in/out caps) for the topology and "
        "returns a reviewable design spec + a synthesized netlist. After "
        "this returns status:'ok', call "
        "ask_user(question='Apply this design spec?', "
        "options=['Confirm','Discard']); on Confirm call "
        "add_subcircuit_from_netlist(netlist=<the netlist from THIS "
        "result>, label=<MPN or descriptive name>). Supply `vref` (the "
        "datasheet feedback reference voltage) whenever the regulator has "
        "an adjustable output — without it the divider falls back to "
        "datasheet values."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mpn": {"type": "string", "description": "Same MPN passed to parse_datasheet."},
            "topology": {
                "type": "string",
                "enum": list(TOPOLOGIES),
                "description": "Converter topology: buck | boost | buck_boost | ldo.",
            },
            "vin": {"type": "string", "description": "Input rail name as it appears in the schematic, e.g. '+5V'."},
            "vout": {"type": "string", "description": "Output rail name, e.g. '+3V3'."},
            "vref": {
                "type": "number",
                "description": "Datasheet feedback reference voltage in volts (e.g. 0.5). Required for a correct adjustable-Vout divider.",
            },
            "fsw_hz": {
                "type": "number",
                "description": "Switching frequency in Hz (e.g. 2400000). Needed for inductor/cap sizing.",
            },
            "iout_a": {
                "type": "number",
                "description": "Max output current in amps (e.g. 0.5). Needed for inductor/cap sizing.",
            },
            "role": {"type": "string", "description": "Block role, e.g. 'buck regulator'."},
            "port_bindings": {
                "type": "object",
                "description": "Map default port net name → user-facing rail (passed through to netlist synthesis).",
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["mpn", "topology", "vin", "vout"],
    },
}


def _volts(s: Optional[str]) -> Optional[float]:
    """'+3V3'→3.3, '5V'→5.0, '1V8'→1.8, '3.3V'→3.3, '0.5'→0.5. None on miss."""
    if not s:
        return None
    t = str(s).strip().lstrip("+").upper()
    m = re.fullmatch(r"(\d+)V(\d+)", t)  # KiCad-style '3V3'
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    t = t.rstrip("V")
    try:
        return float(t)
    except ValueError:
        return None


_INPUT_KEYS = ("input", "vin", "cin", "bulk")
_OUTPUT_KEYS = ("output", "vout", "cout", "feedback", "fb", "divider")


def _norm_value(v: str) -> str:
    """Loose key for matching a SpecComponent value to a NetlistPart value:
    lowercase, drop spaces, µ→u, Ω→ohm. '4.7 µF' and '4.7uF' collide."""
    s = normalize_value_str(v or "").lower().replace(" ", "")
    return s.replace("µ", "u").replace("μ", "u").replace("ω", "ohm").replace("Ω", "ohm")


def _letter(lib_id: str) -> str:
    """'Device:C_Polarized' → 'C', 'Device:R' → 'R'. '' for non-Device libs."""
    if not lib_id.startswith("Device:"):
        return ""
    return lib_id.split(":", 1)[1][:1].upper()


def _rail_v(sc, spec: DesignSpec) -> Optional[float]:
    """Best-effort rail voltage a passive sits on, for cap derating."""
    purpose = (sc.purpose or "").lower()
    if any(k in purpose for k in _INPUT_KEYS):
        return spec.vin_v
    if any(k in purpose for k in _OUTPUT_KEYS):
        return spec.vout_v
    if sc.chip_pin_number:
        for rm in spec.rail_map:
            if rm.pin_number == sc.chip_pin_number:
                if rm.rail == spec.vin:
                    return spec.vin_v
                if rm.rail == spec.vout:
                    return spec.vout_v
                return _volts(rm.rail)
    return None


def _query_for(sc, part, spec: DesignSpec) -> tuple[str, Optional[float]]:
    """Build (search_query, min_voltage) for a SpecComponent + its NetlistPart."""
    value = sc.value or part.value
    fp = part.footprint or ""
    letter = (sc.component or _letter(part.lib_id)).upper()
    if letter == "R":
        bits = [value, sc.tolerance or "", fp, "resistor"]
        return " ".join(b for b in bits if b).strip(), None
    if letter == "C":
        rail_v = _rail_v(sc, spec)
        vrating = f"{math.ceil(rail_v * 1.5)}V" if rail_v else ""
        bits = [value, vrating, fp, "capacitor"]
        return " ".join(b for b in bits if b).strip(), rail_v
    if letter == "L":
        cur = f">{spec.iout_a}A" if spec.iout_a else ""
        bits = [value, fp, cur, "inductor"]
        return " ".join(b for b in bits if b).strip(), None
    if letter == "D":
        return " ".join(b for b in [value, fp, "diode"] if b).strip(), None
    if letter == "Q":
        return " ".join(b for b in [value, fp, "transistor"] if b).strip(), None
    return " ".join(b for b in [value, fp] if b).strip(), None


def _bake_search_queries(spec: DesignSpec, netlist: Netlist) -> None:
    """Stamp `search_query`/`min_voltage` onto each passive NetlistPart from
    its matching SpecComponent (deterministic; no LLM, off the trust boundary).

    Match key is (component-letter, normalized-value). Unmatched parts (the IC,
    or anything the synth re-valued) are left with `search_query=None` — the
    resolve tool's deterministic fallback covers them.
    """
    by_key: dict[tuple[str, str], object] = {}
    for sc in spec.components:
        key = ((sc.component or "").upper(), _norm_value(sc.value))
        by_key.setdefault(key, sc)
    for part in netlist.parts:
        letter = _letter(part.lib_id)
        if not letter:
            continue  # ICs / non-Device libs — resolve via MPN, not keyword
        sc = by_key.get((letter, _norm_value(part.value)))
        if sc is None:
            continue
        q, mv = _query_for(sc, part, spec)
        part.search_query = q or None
        part.min_voltage = mv


def run(
    state,
    mpn: str = "",
    topology: str = "",
    vin: str = "",
    vout: str = "",
    vref: Optional[float] = None,
    fsw_hz: Optional[float] = None,
    iout_a: Optional[float] = None,
    role: Optional[str] = None,
    port_bindings: Optional[dict] = None,
    **_inputs,
) -> dict:
    mpn = (mpn or "").strip()
    topology = (topology or "").strip().lower()
    if not mpn:
        return {"status": "missing_input", "hint": "mpn is required."}
    if topology not in TOPOLOGIES:
        return {
            "status": "missing_input",
            "hint": f"topology must be one of {list(TOPOLOGIES)}.",
        }
    if not vin or not vout:
        return {"status": "missing_input", "hint": "vin and vout are required."}

    prof = state.profiles_by_mpn.get(mpn)
    rs = state.resolved_symbols.get(mpn)
    if prof is None or rs is None:
        return {
            "status": "needs_parse_datasheet",
            "hint": (
                f"No resolved profile/symbol for {mpn} on this conversation. "
                "Call parse_datasheet first (it must return "
                "status:'profile_ready')."
            ),
        }

    variant = None
    vcode = rs.get("variant_code")
    if vcode:
        variant = next(
            (v for v in prof.available_variants if v.package_code == vcode), None
        )

    try:
        spec = build_design_spec(
            profile=prof,
            variant=variant,
            topology=topology,
            vin=vin,
            vout=vout,
            vin_v=_volts(vin),
            vout_v=_volts(vout),
            vref=vref,
            fsw_hz=fsw_hz,
            iout_a=iout_a,
            role=role,
        )
    except Exception as e:
        return {"status": "equation_failed", "error": f"{type(e).__name__}: {e}"}

    try:
        netlist = netlist_synth.synthesize_netlist(
            mpn=mpn,
            variant_code=spec.variant_code,
            orderable_part=spec.orderable_part,
            pintable=prof.pintable_for(variant),
            recommended_passives=spec.to_recommended_passives(),
            symbol_lib_id=rs["lib_id"],
            symbol_pins=rs["symbol_pins"],
            role=role,
            vin=vin,
            vout=vout,
            port_bindings=port_bindings,
        )
    except netlist_synth.NetlistSynthError as e:
        return {"status": "netlist_synth_failed", "error": str(e), **e.detail}
    except Exception as e:
        return {"status": "netlist_synth_failed", "error": f"{type(e).__name__}: {e}"}

    # Bake LCSC search hints onto the passives so resolve_parts can resolve
    # them later without re-deriving the design context.
    try:
        _bake_search_queries(spec, netlist)
    except Exception:
        pass  # query baking is a best-effort enrichment, never fatal

    state.design_specs[mpn] = spec
    state.pending_netlists[mpn] = netlist.model_dump()

    return {
        "status": "ok",
        "spec": spec.model_dump(),
        "netlist": netlist.model_dump(),
        "hint": (
            "Show the user this design spec, then call "
            "ask_user(question='Apply this design spec?', "
            "options=['Confirm','Discard']). On Confirm, call "
            "add_subcircuit_from_netlist(netlist=<the netlist from this "
            "result>, label=<MPN or descriptive name>). On Discard, end the "
            "turn without staging."
        ),
    }
