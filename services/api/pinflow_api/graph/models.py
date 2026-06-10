"""Design-graph data model.

Bipartite graph of components ↔ nets, joined by `PinConnection` edges.

Traversal:
    component.pins[pin_num] → net_name → graph.nets[net_name].pins → other components
    net.pins[i].component_ref → graph.components[ref] → its other pins/nets
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class NetType(str, Enum):
    POWER = "power"
    GROUND = "ground"
    SIGNAL = "signal"
    UNKNOWN = "unknown"


class ComponentType(str, Enum):
    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    INDUCTOR = "inductor"
    IC = "ic"
    CONNECTOR = "connector"
    CRYSTAL = "crystal"
    DISCRETE = "discrete"
    TRANSFORMER = "transformer"
    FUSE = "fuse"
    SWITCH = "switch"
    TEST_POINT = "test_point"
    FIDUCIAL = "fiducial"
    MECHANICAL = "mechanical"
    UNKNOWN = "unknown"


class PinConnection(BaseModel):
    """A pin on a component that participates in a net."""

    component_ref: str
    pin_number: str
    pin_name: str | None = None  # enriched from profile pintable when MPN resolved


class Net(BaseModel):
    name: str
    net_type: NetType = NetType.UNKNOWN
    voltage: float | None = None
    pins: list[PinConnection] = []


class Component(BaseModel):
    """A placed component (topology only — datasheet semantics live in the profile)."""

    reference: str
    value: str
    footprint: str
    component_type: ComponentType = ComponentType.UNKNOWN
    mpn: str | None = None
    lib_id: str | None = None  # e.g. "MCU_RaspberryPi:RP2040"; from netlist (libsource)
    pins: dict[str, str] = {}  # pin_number -> net_name


class DesignGraph(BaseModel):
    """
    Bipartite design graph: Components ↔ Nets.

    Traversal:
        component.pins[pin_num] → net_name → graph.nets[net_name].pins → other components
        net.pins[i].component_ref → graph.components[ref] → its other pins/nets
    """

    components: dict[str, Component] = {}
    nets: dict[str, Net] = {}

    # -- Traversal helpers ----------------------------------------------------

    def components_on_net(self, net_name: str) -> list[str]:
        """All component refs connected to a net."""
        net = self.nets.get(net_name)
        if not net:
            return []
        return list({pc.component_ref for pc in net.pins})

    def nets_of_component(self, ref: str) -> list[str]:
        """All net names a component touches."""
        comp = self.components.get(ref)
        if not comp:
            return []
        return list(set(comp.pins.values()))

    def neighbors(self, ref: str) -> dict[str, list[str]]:
        """Components sharing a net with *ref*, grouped by net name."""
        result: dict[str, list[str]] = {}
        for net_name in self.nets_of_component(ref):
            others = [r for r in self.components_on_net(net_name) if r != ref]
            if others:
                result[net_name] = others
        return result

    def components_by_type(self, comp_type: ComponentType) -> list[str]:
        """All refs matching a component type."""
        return [r for r, c in self.components.items() if c.component_type == comp_type]

    def power_nets(self) -> list[Net]:
        """All power and ground nets."""
        return [n for n in self.nets.values() if n.net_type in (NetType.POWER, NetType.GROUND)]

    def capacitors_on_net(self, net_name: str) -> list[str]:
        """Capacitor refs connected to a net (useful for decoupling checks)."""
        return [
            r for r in self.components_on_net(net_name)
            if self.components[r].component_type == ComponentType.CAPACITOR
        ]

    def pin_net(self, ref: str, pin_number: str) -> str | None:
        """Net name for a specific pin on a component."""
        comp = self.components.get(ref)
        if not comp:
            return None
        return comp.pins.get(pin_number)
