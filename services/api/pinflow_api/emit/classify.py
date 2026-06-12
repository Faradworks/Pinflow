"""Role + net classification — what each part *does*, so the placer can lay
out by function rather than by symbol type.

`classify(netlist) -> LayoutPlan` is the analysis pass between the netlist IR
and the layout grammar. It answers two questions the netlist alone can't:

  - **What kind is each net?** ground / power rail / local signal / global
    port — and for a rail, whether it's the IC's input or output side.
  - **What role does each part play?** input/output/config/decoupling cap,
    series element (inductor, ferrite, coupling cap), feedback-divider
    resistor, pull resistor — inferred from the part type, the *kinds* of
    nets it bridges, and the *names* of the IC pins those nets land on.

The IC pinout is the key input — see `emit.pinmap`. Pin **names** lead the
inference (power-management symbols type `VIN` as `input`, not `power_in`, so
electrical type alone is unreliable); the name regexes below are deliberately
broad and meant to grow as new chips are seen.

Callers must have the relevant symbol libraries discovered
(`ksa.get_symbol_cache().discover_libraries(...)`) before calling — the
pinmap step places each IC into a throwaway schematic. With no pinmap the
classifier still runs but degrades (caps fall back to generic decoupling, no
divider detection), so a missing library is non-fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.pinmap import PinInfo, by_number, pinmap_for_lib_id


# --- name heuristics ----------------------------------------------------------
# Matched against IC *pin* names. Broad on purpose; extend as chips are added.
_GND_RE = re.compile(r"^(GND|AGND|DGND|PGND|GNDA|GNDD|VSS|GROUND|EARTH)", re.I)
_VIN_RE = re.compile(
    r"^(VIN|VCC|VDD|VBAT|PVIN|AVIN|PVDD|AVDD|VDDA|SYS|VBUS|VPWR|VI|IN\b)", re.I
)
_VOUT_RE = re.compile(r"^(VOUT|OUT|VO)", re.I)
_FB_RE = re.compile(
    r"^(FB|VFB|FBK|SENSE|VSENSE|ISENSE|ISNS|VSNS|ADJ|COMP|TRIM|VSET)", re.I
)
_CTRL_RE = re.compile(
    r"^(EN|ENABLE|SS|SOFT|BOOT|BST|SYNC|MODE|RT|FREQ|SLEEP|CTRL|ONOFF|INH|PS|"
    r"DLY|NRST|RESET)",
    re.I,
)


def _is_ground_name(s: str) -> bool:
    return bool(_GND_RE.match(s.strip()))


def _is_vin(s: str) -> bool:
    return bool(_VIN_RE.match(s.strip()))


def _is_vout(s: str) -> bool:
    return bool(_VOUT_RE.match(s.strip()))


def _is_fb(s: str) -> bool:
    return bool(_FB_RE.match(s.strip()))


def _is_ctrl(s: str) -> bool:
    return bool(_CTRL_RE.match(s.strip()))


def _ref_prefix(ref: str) -> str:
    """Leading letter run of a refdes — 'C', 'R', 'FB', 'U', 'J'.

    Stops at the first digit or non-letter (e.g. '_'), so descriptive
    refdeses like 'R_SCL' or 'C_OLED' classify as plain 'R'/'C' rather than
    falling through to UNKNOWN and being dropped during placement.
    """
    for i, c in enumerate(ref):
        if not c.isalpha():
            return ref[:i]
    return ref


# --- the model ----------------------------------------------------------------

class NetKind(str, Enum):
    GROUND = "ground"
    RAIL = "rail"          # power rail (non-ground)
    SIGNAL = "signal"      # local signal net
    GLOBAL = "global"      # non-power port — signal exposed beyond this subcircuit


class RailSide(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


class Role(str, Enum):
    IC = "ic"
    INPUT_CAP = "input_cap"
    OUTPUT_CAP = "output_cap"
    DECOUPLING_CAP = "decoupling_cap"   # bypass cap, side undetermined
    CONFIG_CAP = "config_cap"           # cap on a control pin (EN, SS, …)
    SERIES_ELEMENT = "series_element"   # inductor / ferrite / coupling cap / series R
    DIVIDER_RESISTOR = "divider_resistor"
    PULL_RESISTOR = "pull_resistor"
    CONNECTOR = "connector"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ICContact:
    """A net landing on a specific IC pin."""

    ic_refdes: str
    pin: PinInfo


@dataclass
class NetClass:
    name: str
    kind: NetKind
    is_power: bool
    voltage: float | None
    rail_side: RailSide | None          # only meaningful for kind == RAIL
    ic_contacts: list[ICContact] = field(default_factory=list)


@dataclass
class PartClass:
    refdes: str
    role: Role
    host_ic: str | None                 # IC this part supports (itself, for an IC)
    lib_id: str
    pins: dict[str, str]                # pin number → net name
    nets: list[str]                     # distinct net names this part touches


@dataclass
class DividerGroup:
    """A two-resistor feedback/sense divider: rail → tap → ground."""

    host_ic: str
    tap_net: str            # the sense net (touches an FB-class IC pin)
    sensed_net: str         # the rail being divided down
    high_refdes: str        # resistor between the sensed rail and the tap
    low_refdes: str         # resistor between the tap and ground


@dataclass
class LayoutPlan:
    netlist: Netlist
    ics: list[str]
    pinmaps: dict[str, list[PinInfo]]
    nets: dict[str, NetClass]
    parts: dict[str, PartClass]
    dividers: list[DividerGroup]

    def role_of(self, refdes: str) -> Role | None:
        pc = self.parts.get(refdes)
        return pc.role if pc else None

    def with_role(self, role: Role) -> list[str]:
        return sorted(r for r, pc in self.parts.items() if pc.role == role)

    def summary(self) -> dict:
        """Compact dict for the debug trace."""
        by_role: dict[str, list[str]] = {}
        for refdes, pc in self.parts.items():
            by_role.setdefault(pc.role.value, []).append(refdes)
        by_kind: dict[str, list[str]] = {}
        for name, nc in self.nets.items():
            tag = nc.kind.value
            if nc.rail_side:
                tag += f"/{nc.rail_side.value}"
            by_kind.setdefault(tag, []).append(name)
        return {
            "parts_by_role": {k: sorted(v) for k, v in sorted(by_role.items())},
            "nets_by_kind": {k: sorted(v) for k, v in sorted(by_kind.items())},
            "dividers": [
                f"{d.high_refdes}/{d.low_refdes} → {d.tap_net} (of {d.sensed_net})"
                for d in self.dividers
            ],
        }


# --- classification passes ----------------------------------------------------

def _assign_hosts(netlist: Netlist) -> dict[str, str | None]:
    """Map each part to the IC it shares the most nets with (its 'host').

    An IC hosts itself. A part touching no IC gets `None`.
    """
    ic_refs = {p.refdes for p in netlist.parts if _ref_prefix(p.refdes) == "U"}
    nets_by_name = {n.name: n for n in netlist.nets}
    nets_by_part: dict[str, set[str]] = {p.refdes: set() for p in netlist.parts}
    for net in netlist.nets:
        for ep in net.endpoints:
            if ep.ref in nets_by_part:
                nets_by_part[ep.ref].add(net.name)

    hosts: dict[str, str | None] = {}
    for p in netlist.parts:
        if p.refdes in ic_refs:
            hosts[p.refdes] = p.refdes
            continue
        scores: dict[str, int] = {}
        for net_name in nets_by_part[p.refdes]:
            net = nets_by_name.get(net_name)
            if net is None:
                continue
            for ep in net.endpoints:
                if ep.ref in ic_refs and ep.ref != p.refdes:
                    scores[ep.ref] = scores.get(ep.ref, 0) + 1
        hosts[p.refdes] = (
            max(scores.items(), key=lambda kv: (kv[1], kv[0]))[0]
            if scores else None
        )
    return hosts


def _classify_nets(
    netlist: Netlist, pinmaps: dict[str, list[PinInfo]]
) -> dict[str, NetClass]:
    out: dict[str, NetClass] = {}
    pinmap_idx = {ref: by_number(pins) for ref, pins in pinmaps.items()}

    for net in netlist.nets:
        contacts: list[ICContact] = []
        for ep in net.endpoints:
            idx = pinmap_idx.get(ep.ref)
            if idx is None:
                continue
            pi = idx.get(ep.pin)
            if pi is not None:
                contacts.append(ICContact(ep.ref, pi))

        # Power identity FIRST — a rail is a rail even when exposed as a
        # port. Agent-built netlists mark every rail `is_port: true` (the
        # tool schema tells them to), and letting port-ness eclipse
        # GROUND/RAIL here strips power identity from every rail in the
        # block: caps stop classifying as cin/cout, pull resistors become
        # series elements, the layout tree loses its rails, and the placer
        # output degrades catastrophically (overlapping parts, merged
        # nets). GLOBAL is only for non-power boundary signals.
        if net.is_power and (_is_ground_name(net.name) or net.voltage == 0):
            kind = NetKind.GROUND
        elif net.is_power:
            kind = NetKind.RAIL
        elif net.is_port:
            kind = NetKind.GLOBAL
        else:
            kind = NetKind.SIGNAL

        rail_side: RailSide | None = None
        if kind == NetKind.RAIL:
            # A VOUT-class pin is the reliable OUTPUT signal, a VIN-class pin
            # the reliable INPUT. A rail touching neither is left undetermined
            # here and resolved by the post-pass below.
            if any(_is_vout(c.pin.name) for c in contacts):
                rail_side = RailSide.OUTPUT
            elif any(_is_vin(c.pin.name) for c in contacts):
                rail_side = RailSide.INPUT

        out[net.name] = NetClass(
            name=net.name,
            kind=kind,
            is_power=net.is_power,
            voltage=net.voltage,
            rail_side=rail_side,
            ic_contacts=contacts,
        )

    # Post-pass: a rail touching no VIN/VOUT pin is the OUTPUT when another
    # rail is the identified INPUT — a boost converter's output comes off an
    # external inductor + diode, not a chip pin, so it has no VOUT contact. A
    # lone undetermined rail (no input found at all) keeps the INPUT default.
    rails = [nc for nc in out.values() if nc.kind == NetKind.RAIL]
    has_input = any(nc.rail_side == RailSide.INPUT for nc in rails)
    for nc in rails:
        if nc.rail_side is None:
            nc.rail_side = RailSide.OUTPUT if has_input else RailSide.INPUT
    return out


def _cap_role(
    net_names: list[str],
    nets: dict[str, NetClass],
    net_members: dict[str, list[str]],
) -> Role:
    ncs = [nets[n] for n in net_names if n in nets]
    grounds = [nc for nc in ncs if nc.kind == NetKind.GROUND]
    nongrounds = [nc for nc in ncs if nc.kind != NetKind.GROUND]

    # Cap bridging two non-ground nets — a coupling/series cap, placed inline.
    if not grounds and len(nongrounds) >= 2:
        return Role.SERIES_ELEMENT
    if not grounds or not nongrounds:
        return Role.UNKNOWN

    x = nongrounds[0]
    # On a true power rail → a bulk rail cap; side comes from the rail.
    if x.kind == NetKind.RAIL:
        if x.rail_side == RailSide.OUTPUT:
            return Role.OUTPUT_CAP
        return Role.INPUT_CAP

    # x is a signal net at the IC. A cap here is a pin-local *config* bypass
    # UNLESS the node also carries a series filter element (inductor /
    # ferrite) — then it is a real input/output supply cap sitting on a
    # post-filter rail node that simply has no power symbol, so KiCad never
    # marked the net `power`. This is what tells C2 (node carries the ferrite
    # FB1 → input cap) from C3 (node is just IC pins → config cap), even
    # though both nodes touch a VIN-class pin name.
    on_supply_path = any(
        _ref_prefix(r) in ("L", "FB") for r in net_members.get(x.name, [])
    )
    if not on_supply_path:
        return Role.CONFIG_CAP

    names = [c.pin.name for c in x.ic_contacts]
    if any(_is_vout(nm) for nm in names):
        return Role.OUTPUT_CAP
    if any(_is_vin(nm) for nm in names):
        return Role.INPUT_CAP
    return Role.DECOUPLING_CAP


def _resistor_role(net_names: list[str], nets: dict[str, NetClass]) -> Role:
    ncs = [nets[n] for n in net_names if n in nets]
    grounds = [nc for nc in ncs if nc.kind == NetKind.GROUND]
    rails = [nc for nc in ncs if nc.kind == NetKind.RAIL]
    signals = [nc for nc in ncs if nc.kind in (NetKind.SIGNAL, NetKind.GLOBAL)]

    # Resistor inline between two signal nets — series (e.g. gate / damping R).
    if len(signals) >= 2 and not grounds and not rails:
        return Role.SERIES_ELEMENT
    # Anything touching a rail or ground is a pull / bias resistor; the
    # divider pass may promote it to DIVIDER_RESISTOR afterwards.
    if grounds or rails:
        return Role.PULL_RESISTOR
    return Role.UNKNOWN


def _role_for(
    prefix: str,
    net_names: list[str],
    nets: dict[str, NetClass],
    net_members: dict[str, list[str]],
) -> Role:
    if prefix == "U":
        return Role.IC
    if prefix in ("J", "P"):
        return Role.CONNECTOR
    if prefix in ("L", "FB"):
        # Inductors and ferrite beads are always inline in a path.
        return Role.SERIES_ELEMENT
    if prefix == "C":
        return _cap_role(net_names, nets, net_members)
    if prefix == "R":
        return _resistor_role(net_names, nets)
    return Role.UNKNOWN


def _detect_dividers(
    nets: dict[str, NetClass], parts: dict[str, PartClass]
) -> list[DividerGroup]:
    """Find two-resistor feedback dividers: rail → tap → ground.

    A tap net is a signal net landing on an FB-class IC pin. On that net,
    the resistor whose other end is a rail is the high leg; the one whose
    other end is ground is the low leg.
    """
    dividers: list[DividerGroup] = []
    for net_name, nc in nets.items():
        if nc.kind != NetKind.SIGNAL:
            continue
        if not any(_is_fb(c.pin.name) for c in nc.ic_contacts):
            continue
        host = nc.ic_contacts[0].ic_refdes

        high = low = sensed = None
        for refdes, pc in parts.items():
            if _ref_prefix(refdes) != "R" or net_name not in pc.nets:
                continue
            other = [n for n in pc.nets if n != net_name]
            if not other:
                continue
            other_nc = nets.get(other[0])
            if other_nc is None:
                continue
            if other_nc.kind == NetKind.RAIL and high is None:
                high, sensed = refdes, other_nc.name
            elif other_nc.kind == NetKind.GROUND and low is None:
                low = refdes

        if high and low and sensed:
            dividers.append(DividerGroup(host, net_name, sensed, high, low))
    return dividers


def _classify_parts(
    netlist: Netlist,
    nets: dict[str, NetClass],
    hosts: dict[str, str | None],
) -> tuple[dict[str, PartClass], list[DividerGroup]]:
    # pin number → net name, per part; and net name → parts on it.
    pin_nets: dict[str, dict[str, str]] = {}
    net_members: dict[str, list[str]] = {}
    for net in netlist.nets:
        for ep in net.endpoints:
            pin_nets.setdefault(ep.ref, {})[ep.pin] = net.name
            net_members.setdefault(net.name, []).append(ep.ref)

    parts: dict[str, PartClass] = {}
    for p in netlist.parts:
        prefix = _ref_prefix(p.refdes)
        pins = pin_nets.get(p.refdes, {})
        net_names = sorted(set(pins.values()))
        parts[p.refdes] = PartClass(
            refdes=p.refdes,
            role=_role_for(prefix, net_names, nets, net_members),
            host_ic=hosts.get(p.refdes),
            lib_id=p.lib_id,
            pins=pins,
            nets=net_names,
        )

    dividers = _detect_dividers(nets, parts)
    for d in dividers:
        parts[d.high_refdes].role = Role.DIVIDER_RESISTOR
        parts[d.low_refdes].role = Role.DIVIDER_RESISTOR
    return parts, dividers


def classify(netlist: Netlist) -> LayoutPlan:
    """Classify every net and part of `netlist` into a `LayoutPlan`.

    Pure apart from the pinmap step, which places each IC into a throwaway
    schematic to read its pinout (requires the symbol library discovered).
    """
    ics = [p for p in netlist.parts if _ref_prefix(p.refdes) == "U"]
    pinmaps: dict[str, list[PinInfo]] = {}
    for ic in ics:
        try:
            pinmaps[ic.refdes] = pinmap_for_lib_id(ic.lib_id, reference=ic.refdes)
        except Exception:
            pinmaps[ic.refdes] = []

    nets = _classify_nets(netlist, pinmaps)
    parts, dividers = _classify_parts(netlist, nets, _assign_hosts(netlist))
    return LayoutPlan(
        netlist=netlist,
        ics=[ic.refdes for ic in ics],
        pinmaps=pinmaps,
        nets=nets,
        parts=parts,
        dividers=dividers,
    )
