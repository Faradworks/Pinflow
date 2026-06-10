"""KiCad netlist (.net) → CircuitGraph.

The .net file is a KiCad S-expression with this shape:

    (export (version "E")
      (design ...)
      (components (comp (ref "C22") (value "4.7u") (footprint "...")
                        (libsource (lib "Device") (part "C") ...)
                        (property (name "X") (value "Y")) ...))
      (libparts ...)
      (libraries ...)
      (nets (net (code "1") (name "+3V3")
                 (node (ref "C23") (pin "1") (pintype "passive"))
                 (node (ref "D2")  (pin "2") (pinfunction "A_2") (pintype "passive")) ...) ...))

We extract just the components and nets and drop the rest (libparts/libraries
duplicate information already in component.lib_id; design header is metadata).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import sexpdata


# ---------- Data model ----------

@dataclass(frozen=True)
class Node:
    """A pin reference inside a net: which component, which pin, what kind of pin."""
    ref: str
    pin: str
    pin_type: str = "passive"   # 'passive', 'power_in', 'power_out', 'input', ...

    def __repr__(self) -> str:
        return f"{self.ref}.{self.pin}"


@dataclass
class Net:
    name: str
    nodes: list[Node] = field(default_factory=list)

    @property
    def is_power(self) -> bool:
        """+5V, +3V3, +12V, -12V, VCC, VDD, V+, V-, etc."""
        n = self.name.strip()
        if not n:
            return False
        if n[0] in "+-":
            return True
        if n.upper() in {"VCC", "VDD", "VSS", "VEE", "V+", "V-"}:
            return True
        return False

    @property
    def is_ground(self) -> bool:
        """GND, AGND, DGND, PGND, GNDA, GNDD, etc."""
        n = self.name.strip().upper()
        return n == "GND" or n.endswith("GND") or n.startswith("GND")


@dataclass
class Component:
    ref: str                                        # "C22"
    value: str                                      # "4.7u"
    lib_id: str                                     # "Device:C"
    footprint: str = ""
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Crude category from the lib_id: 'C', 'R', 'L', 'D', 'LED', 'U', 'FB' ..."""
        # Use the part name (text after the colon) as the kind signal.
        if ":" not in self.lib_id:
            return self.lib_id
        return self.lib_id.split(":", 1)[1]


@dataclass
class CircuitGraph:
    components: dict[str, Component]                # keyed by refdes
    nets: list[Net]

    # ---- lookup helpers ----

    def net_for_pin(self, ref: str, pin: str) -> Net | None:
        for net in self.nets:
            for n in net.nodes:
                if n.ref == ref and n.pin == pin:
                    return net
        return None

    def pins_of(self, ref: str) -> list[tuple[str, str]]:
        """All (pin, net_name) for a given component."""
        out: list[tuple[str, str]] = []
        for net in self.nets:
            for n in net.nodes:
                if n.ref == ref:
                    out.append((n.pin, net.name))
        return out

    def power_nets(self) -> list[Net]:
        return [n for n in self.nets if n.is_power]

    def ground_nets(self) -> list[Net]:
        return [n for n in self.nets if n.is_ground]

    def signal_nets(self) -> list[Net]:
        return [n for n in self.nets if not n.is_power and not n.is_ground]

    def gnd_pins(self, ref: str) -> list[str]:
        """Pin numbers of `ref` that connect to a ground net."""
        gnd_names = {n.name for n in self.ground_nets()}
        return [p for p, net in self.pins_of(ref) if net in gnd_names]

    def power_pins(self, ref: str) -> list[tuple[str, str]]:
        """(pin, net_name) tuples for `ref` connecting to a power net."""
        pwr_names = {n.name for n in self.power_nets()}
        return [(p, n) for p, n in self.pins_of(ref) if n in pwr_names]


# ---------- Parser ----------

def _sym(x) -> str | None:
    return x.value() if isinstance(x, sexpdata.Symbol) else None


def _children_named(node: list, name: str) -> list:
    return [c for c in node[1:]
            if isinstance(c, list) and c and _sym(c[0]) == name]


def _child_value(node: list, name: str, default: str = "") -> str:
    """Return the string in `(name VALUE)`; "" if absent."""
    for c in node[1:]:
        if isinstance(c, list) and c and _sym(c[0]) == name:
            if len(c) >= 2 and isinstance(c[1], str):
                return c[1]
    return default


def _parse_component(comp_node: list) -> Component:
    ref = _child_value(comp_node, "ref")
    value = _child_value(comp_node, "value")
    footprint = _child_value(comp_node, "footprint")

    # libsource: (libsource (lib "Device") (part "C") (description "..."))
    lib_id = ""
    libsource_nodes = _children_named(comp_node, "libsource")
    if libsource_nodes:
        lib = _child_value(libsource_nodes[0], "lib")
        part = _child_value(libsource_nodes[0], "part")
        if lib and part:
            lib_id = f"{lib}:{part}"

    # properties: (property (name "X") (value "Y"))
    properties: dict[str, str] = {}
    for prop in _children_named(comp_node, "property"):
        pname = _child_value(prop, "name")
        pval = _child_value(prop, "value")
        if pname:
            properties[pname] = pval

    return Component(ref=ref, value=value, lib_id=lib_id, footprint=footprint, properties=properties)


def _parse_net(net_node: list) -> Net:
    name = _child_value(net_node, "name")
    nodes: list[Node] = []
    for node_block in _children_named(net_node, "node"):
        ref = _child_value(node_block, "ref")
        pin = _child_value(node_block, "pin")
        pin_type = _child_value(node_block, "pintype", default="passive")
        if ref and pin:
            nodes.append(Node(ref=ref, pin=pin, pin_type=pin_type))
    return Net(name=name, nodes=nodes)


def parse_netlist(net_path: str | Path) -> CircuitGraph:
    """Parse a KiCad .net file into a CircuitGraph."""
    text = Path(net_path).read_text()
    parsed = sexpdata.loads(text)

    if not isinstance(parsed, list) or _sym(parsed[0]) != "export":
        raise ValueError(f"{net_path}: not a KiCad netlist (expected top (export ...))")

    components: dict[str, Component] = {}
    nets: list[Net] = []

    for block in _children_named(parsed, "components"):
        for comp_node in _children_named(block, "comp"):
            c = _parse_component(comp_node)
            if c.ref:
                components[c.ref] = c

    for block in _children_named(parsed, "nets"):
        for net_node in _children_named(block, "net"):
            n = _parse_net(net_node)
            if n.name:
                nets.append(n)

    return CircuitGraph(components=components, nets=nets)
