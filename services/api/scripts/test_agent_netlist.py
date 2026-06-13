"""Smoke test: agent-shaped netlists must classify and place correctly.

Regression for the 2026-06-12 USB-C + LDO dead end. The chat agent hand-builds
netlists per the `add_subcircuit_from_netlist` schema: rails carry only
`is_port: true` — no `is_power`, no `voltage`. Two stacked bugs made every such
netlist degrade catastrophically:

  1. `Netlist` never derived power-ness from net names, and
  2. `classify._classify_nets` let `is_port` eclipse GROUND/RAIL (kind=GLOBAL),

so +5V/GND/+3V3 classified as signals → caps/resistors got series roles → the
layout tree lost its rails → cplace anchored both CC pulldowns to the same IC
GND pin (identical coordinates, coincident pins) → the router's label fallback
silently merged CC1+CC2 into one net → `validate_placer_output` rejected the
placement → the model retried the identical input into the failure breaker.

Run: cd services/api && .venv/bin/python scripts/test_agent_netlist.py
Needs the bundled KiCad symbol libraries (no LLM, no network).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pinflow_api.emit.classify import Role, classify  # noqa: E402
from pinflow_api.emit.layout_tree import build_layout_tree  # noqa: E402
from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.placers import get_placer  # noqa: E402
from pinflow_api.emit.structural_diff import validate_placer_output  # noqa: E402

# Verbatim shape from the agent: USB-C receptacle feeding an AP2112K-3.3 LDO.
# Rails are marked is_port only — exactly what the tool schema asks for.
AGENT_NETLIST = {
    "parts": [
        {"refdes": "J1", "lib_id": "Connector:USB_C_Receptacle_USB2.0_16P", "value": "USB-C"},
        {"refdes": "R1", "lib_id": "Device:R", "value": "5.1k"},
        {"refdes": "R2", "lib_id": "Device:R", "value": "5.1k"},
        {"refdes": "R3", "lib_id": "Device:R", "value": "10k"},
        {"refdes": "U1", "lib_id": "Regulator_Linear:AP2112K-3.3", "value": "AP2112K-3.3"},
        {"refdes": "C1", "lib_id": "Device:C", "value": "1µF"},
        {"refdes": "C2", "lib_id": "Device:C", "value": "1µF"},
    ],
    "nets": [
        {"name": "+5V", "is_port": True, "endpoints": [
            {"ref": "J1", "pin": "A4"}, {"ref": "J1", "pin": "B4"},
            {"ref": "J1", "pin": "A9"}, {"ref": "J1", "pin": "B9"},
            {"ref": "U1", "pin": "1"}, {"ref": "C1", "pin": "1"},
            {"ref": "R3", "pin": "1"},
        ]},
        {"name": "+3V3", "is_port": True, "endpoints": [
            {"ref": "U1", "pin": "5"}, {"ref": "C2", "pin": "1"},
        ]},
        {"name": "GND", "is_port": True, "endpoints": [
            {"ref": "J1", "pin": "A1"}, {"ref": "J1", "pin": "B1"},
            {"ref": "J1", "pin": "A12"}, {"ref": "J1", "pin": "B12"},
            {"ref": "J1", "pin": "SH"}, {"ref": "U1", "pin": "2"},
            {"ref": "C1", "pin": "2"}, {"ref": "C2", "pin": "2"},
            {"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "2"},
        ]},
        {"name": "CC1", "endpoints": [{"ref": "J1", "pin": "A5"}, {"ref": "R1", "pin": "1"}]},
        {"name": "CC2", "endpoints": [{"ref": "J1", "pin": "B5"}, {"ref": "R2", "pin": "1"}]},
        {"name": "EN", "endpoints": [{"ref": "U1", "pin": "3"}, {"ref": "R3", "pin": "2"}]},
    ],
}


def _rename_rails(rails: tuple[str, str, str], *, force_power: bool = False) -> dict:
    """AGENT_NETLIST with (+5V, +3V3, GND) renamed — adversarial rail names."""
    import copy

    data = copy.deepcopy(AGENT_NETLIST)
    mapping = dict(zip(("+5V", "+3V3", "GND"), rails))
    for net in data["nets"]:
        if net["name"] in mapping:
            net["name"] = mapping[net["name"]]
            if force_power:
                net["is_power"] = True
    return data


def _no_stacked_symbols(sch_text: str) -> list[tuple[str, str]]:
    """Pairs of non-power symbols sharing identical coordinates (must be [])."""
    import re as _re

    seen: dict[tuple[str, str], str] = {}
    dups: list[tuple[str, str]] = []
    pat = _re.compile(
        r'\(symbol\s+\(lib_id "[^"]+"\)\s+\(at ([\d.-]+) ([\d.-]+) \d+\)'
        r'.*?\(property "Reference" "([^"]+)"', _re.S)
    for x, y, ref in pat.findall(sch_text):
        if ref.startswith("#PWR"):
            continue
        if (x, y) in seen:
            dups.append((seen[(x, y)], ref))
        seen[(x, y)] = ref
    return dups


# Hostile variants: the invariant is graceful degradation — layout may be
# ugly, but topology must survive the round-trip and parts must never stack
# (coincident pins silently merge nets, which is what fed the retry spiral).
ADVERSARIAL = {
    "lowercase rails": _rename_rails(("+5v", "+3v3", "gnd")),
    "numeric rails": _rename_rails(("5V", "3V3", "GND")),
    "unconventional rail names (heuristics miss)": _rename_rails(
        ("PWR_RAIL", "CORE_SUPPLY", "RETURN_PATH")),
    "unconventional names, explicit is_power": _rename_rails(
        ("PWR_RAIL", "CORE_SUPPLY", "RETURN_PATH"), force_power=True),
}


def main() -> int:
    nl = Netlist.model_validate(AGENT_NETLIST)

    # is_power derivation: port-marked rails keep their power identity.
    by_name = {n.name: n for n in nl.nets}
    for rail in ("+5V", "+3V3", "GND"):
        assert by_name[rail].is_power, f"{rail} did not derive is_power"
    assert not by_name["CC1"].is_power, "CC1 wrongly derived is_power"

    # Classification: rails recognized, caps get rail roles, pulls stay pulls.
    plan = classify(nl)
    assert plan.role_of("C1") == Role.INPUT_CAP, plan.role_of("C1")
    assert plan.role_of("C2") == Role.OUTPUT_CAP, plan.role_of("C2")
    for r in ("R1", "R2", "R3"):
        assert plan.role_of(r) == Role.PULL_RESISTOR, (r, plan.role_of(r))

    tree = build_layout_tree(nl)
    rails = tree.summary()["rails"]
    assert rails == {"input": "+5V", "output": "+3V3", "ground": "GND"}, rails

    # Placement: no coincident parts, topology survives the file round-trip.
    result = get_placer()(nl, title="usb-c + ldo (agent shape)")
    vr = validate_placer_output(nl, result)
    assert vr.ok, vr.errors
    assert not any("fell back to label-only" in i for i in result.issues), result.issues
    assert not _no_stacked_symbols(result.sch_text)
    print("PASS  agent-shaped netlist classifies, places, and validates")

    for name, data in ADVERSARIAL.items():
        anl = Netlist.model_validate(data)
        ares = get_placer()(anl, title=name)
        avr = validate_placer_output(anl, ares)
        assert avr.ok, (name, avr.errors)
        stacked = _no_stacked_symbols(ares.sch_text)
        assert not stacked, (name, stacked)
        print(f"PASS  adversarial: {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
