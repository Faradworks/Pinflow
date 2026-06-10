"""Build a DesignGraph from a parsed netlist + symbol properties + MPN profiles.

Pinflow reads MPN off `(property "MPN" ...)` on the schematic symbol
(no separate BOM CSV).
"""

from __future__ import annotations

import re
from typing import Any

from pinflow_api.graph.models import (
    Component,
    ComponentType,
    DesignGraph,
    Net,
    NetType,
    PinConnection,
)
from pinflow_api.netlist import ParsedNetlist
from pinflow_api.sch_properties import get_mpn


# ---------------------------------------------------------------------------
# Component-type classification
# ---------------------------------------------------------------------------

_PREFIX_TYPE: dict[str, ComponentType] = {
    "R": ComponentType.RESISTOR,
    "C": ComponentType.CAPACITOR,
    "L": ComponentType.INDUCTOR,
    "U": ComponentType.IC,
    "IC": ComponentType.IC,
    "J": ComponentType.CONNECTOR,
    "X": ComponentType.CRYSTAL,
    "Y": ComponentType.CRYSTAL,
    "D": ComponentType.DISCRETE,
    "LED": ComponentType.DISCRETE,
    "Q": ComponentType.DISCRETE,
    "T": ComponentType.TRANSFORMER,
    "F": ComponentType.FUSE,
    "SW": ComponentType.SWITCH,
    "TP": ComponentType.TEST_POINT,
    "FM": ComponentType.FIDUCIAL,
    "MH": ComponentType.MECHANICAL,
}

# Fallback footprint patterns for refdeses whose prefix isn't a known EE
# convention. Order matters — first match wins.
_FOOTPRINT_TYPE_PATTERNS: list[tuple[re.Pattern, ComponentType]] = [
    (re.compile(
        r"(?i)(?:^|[\s_])("
        r"CONN(?:_|\b)|TERM(?:\b|_BLK)|HEADER|SOCKET|JACK|RECEPTACLE|PLUG|"
        r"SCREW\s*TERM|PINHEADER|BARREL|BANANA|XT30|XT60|XT90|USB|"
        r"WURTH\s*746\d|TE\s*282834|TE\s*2828\d|MOLEX|JST"
        r")"
    ), ComponentType.CONNECTOR),
    (re.compile(r"(?i)TestPoint|TEST[_\s]POINT|\bTP_"), ComponentType.TEST_POINT),
    (re.compile(r"(?i)^LED[\s_]|\bLED\s+\d{3,4}"), ComponentType.DISCRETE),
    (re.compile(r"(?i)^CAP[\s_]|\bCAP_|CAPACITOR"), ComponentType.CAPACITOR),
    (re.compile(r"(?i)^RES[\s_]|\bRES_|RESISTOR"), ComponentType.RESISTOR),
    (re.compile(r"(?i)^IND[\s_]|\bIND_|INDUCTOR"), ComponentType.INDUCTOR),
    (re.compile(r"(?i)DO214|DO220|SOD\d|SMD?J5|SMB_|SOT-?23"), ComponentType.DISCRETE),
]


def classify_component(ref: str, footprint: str) -> ComponentType:
    """Classify a component by refdes prefix, with footprint regex fallback."""
    prefix = re.match(r"^[A-Za-z]+", ref)
    if prefix:
        t = _PREFIX_TYPE.get(prefix.group())
        if t is not None:
            return t
    for pattern, ctype in _FOOTPRINT_TYPE_PATTERNS:
        if pattern.search(footprint or ""):
            return ctype
    return ComponentType.UNKNOWN


# ---------------------------------------------------------------------------
# Net-name → type/voltage inference
# ---------------------------------------------------------------------------

_POWER_PREFIXES = (
    "VCC", "VDD", "VBUS", "VBAT", "VSYS", "VSUP", "VPWR",
    "AVDD", "DVDD", "AVCC", "DVCC", "PVDD", "PVCC",
    "V_",
)
_GROUND_SUFFIXES = ("_GND", "GND")
_GROUND_NAMES = {"GND", "AGND", "DGND", "PGND", "VSS", "AVSS", "DVSS", "PVSS"}


def _parse_rail_voltage(name: str) -> float | None:
    # +3V3 style → 3.3
    m = re.match(r"^\+(\d+)V(\d+)$", name)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    # +5V style → 5.0
    m = re.match(r"^\+(\d+(?:\.\d+)?)V$", name)
    if m:
        return float(m.group(1))
    # Embedded *_1V8, DVDD3V3, etc.
    m = re.search(r"(\d+)V(\d+)", name)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    # Embedded *_5V0, *_12V
    m = re.search(r"(\d+(?:\.\d+)?)V(?:\d|$|_)", name)
    if m:
        return float(m.group(1))
    return None


def infer_net_properties(name: str) -> tuple[NetType, float | None]:
    """Deterministically classify a net by its name."""
    upper = name.upper()
    if upper in _GROUND_NAMES or any(upper.endswith(s) for s in _GROUND_SUFFIXES):
        return NetType.GROUND, 0.0
    if name.startswith("+"):
        return NetType.POWER, _parse_rail_voltage(name)
    if any(upper.startswith(p) for p in _POWER_PREFIXES):
        return NetType.POWER, _parse_rail_voltage(name)
    return NetType.SIGNAL, None


# ---------------------------------------------------------------------------
# MPN normalization (for cache-identity matching)
# ---------------------------------------------------------------------------


def _norm_mpn(s: str) -> str:
    return re.sub(r"[/_\-\s]", "", s).upper()


def _resolve_profile(mpn: str | None, profiles_by_mpn: dict[str, Any]) -> Any | None:
    if not mpn or not profiles_by_mpn:
        return None
    if mpn in profiles_by_mpn:
        return profiles_by_mpn[mpn]
    norm = _norm_mpn(mpn)
    for cached_mpn, profile in profiles_by_mpn.items():
        if _norm_mpn(cached_mpn) == norm:
            return profile
    return None


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_design_graph(
    netlist: ParsedNetlist,
    props_by_ref: dict[str, dict[str, str]] | None = None,
    profiles_by_mpn: dict[str, Any] | None = None,
) -> DesignGraph:
    """Join netlist + symbol properties + MPN profiles into a DesignGraph.

    `props_by_ref` is the output of `sch_properties.parse_properties` — it
    carries MPN, Datasheet, Manufacturer per refdes.

    `profiles_by_mpn` is a dict of `ComponentProfile` (or any object exposing
    a `.chosen_pintable` list of `Pin`-shaped entries with `.number` and
    `.name`). Used only to enrich `PinConnection.pin_name`.
    """
    props_by_ref = props_by_ref or {}
    profiles_by_mpn = profiles_by_mpn or {}

    components: dict[str, Component] = {}
    nets: dict[str, Net] = {}

    # --- Components ---------------------------------------------------------
    for refdes, meta in netlist.components.items():
        props = props_by_ref.get(refdes, {})
        mpn = get_mpn(props, value_fallback=None)

        components[refdes] = Component(
            reference=refdes,
            value=meta.value,
            footprint=meta.footprint,
            component_type=classify_component(refdes, meta.footprint),
            mpn=mpn,
            lib_id=meta.lib_id,
            pins={},
        )

    # Per-refdes profile lookup (used for pin-name enrichment during net build)
    profile_by_ref: dict[str, Any] = {}
    for refdes, comp in components.items():
        profile = _resolve_profile(comp.mpn, profiles_by_mpn)
        if profile is not None:
            profile_by_ref[refdes] = profile

    # --- Nets ---------------------------------------------------------------
    for net_name, endpoints in netlist.nets.items():
        net_type, voltage = infer_net_properties(net_name)
        pin_connections: list[PinConnection] = []

        for refdes, pin_num in endpoints:
            # Record on component side: pin_number -> net_name
            if refdes in components:
                components[refdes].pins[pin_num] = net_name

            # Enrich pin_name from profile pintable when available
            pin_name: str | None = None
            profile = profile_by_ref.get(refdes)
            if profile is not None and getattr(profile, "chosen_pintable", None):
                for p in profile.chosen_pintable:
                    if str(getattr(p, "number", "")) == str(pin_num):
                        pin_name = getattr(p, "name", None)
                        break

            pin_connections.append(PinConnection(
                component_ref=refdes,
                pin_number=pin_num,
                pin_name=pin_name,
            ))

        nets[net_name] = Net(
            name=net_name,
            net_type=net_type,
            voltage=voltage,
            pins=pin_connections,
        )

    return DesignGraph(components=components, nets=nets)
