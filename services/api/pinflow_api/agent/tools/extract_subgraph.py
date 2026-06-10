"""Tool: extract_subgraph.

Pulls a netlist fragment out of the active schematic for replication or
swap-in-place edits. BFS from `seed_refdeses` over the design graph, cutting
at `boundary_nets`; collected components and nets become a `Netlist` the
model can feed back into `add_subcircuit_from_netlist` (typically with
`port_bindings` to rename the cut-edge nets).
"""

from __future__ import annotations

import re

from pinflow_api.emit.netlist import (
    Netlist,
    NetlistEndpoint,
    NetlistNet,
    NetlistPart,
)
from pinflow_api.graph.models import DesignGraph, NetType

SCHEMA = {
    "name": "extract_subgraph",
    "description": (
        "Extract a netlist fragment from the active schematic for replication. "
        "boundary_nets are cut points that become ports on the extracted "
        "netlist; the seeds + everything internal to them are pulled into the "
        "fragment. Returns a netlist consumable by add_subcircuit_from_netlist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "seed_refdeses": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Component refdeses to start the extraction from, "
                    "e.g. ['U1','C1','C2']."
                ),
            },
            "boundary_nets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Nets to cut at; these become ports on the extracted "
                    "netlist. Typically the IC's interface signals "
                    "(power rails, I/O nets connecting to the rest of the "
                    "design)."
                ),
            },
        },
        "required": ["seed_refdeses", "boundary_nets"],
    },
}


# KiCad emits `unconnected-(Ref-PadN)` for pins explicitly left unconnected.
# These aren't real nets — they're "intentionally floating" markers. Drop
# them from the extracted netlist entirely.
_NO_CONNECT = re.compile(r"^unconnected-")

# Auto-named internal nets (KiCad churn-prone). Renamed to stable INT_<i>.
_AUTO_NET = re.compile(r"^(Net-\(|/)")


def _is_no_connect(name: str) -> bool:
    return bool(_NO_CONNECT.match(name))


def _is_auto_named(name: str) -> bool:
    return bool(_AUTO_NET.match(name))


def _bfs(
    graph: DesignGraph, seeds: set[str], boundary_nets: set[str]
) -> set[str]:
    """Return the set of refdeses reachable from `seeds`, cutting at `boundary_nets`."""
    visited: set[str] = set()
    frontier: list[str] = [r for r in seeds if r in graph.components]
    while frontier:
        ref = frontier.pop()
        if ref in visited:
            continue
        visited.add(ref)
        comp = graph.components.get(ref)
        if comp is None:
            continue
        for _pin_num, net_name in comp.pins.items():
            if net_name in boundary_nets:
                continue
            for other_ref in graph.components_on_net(net_name):
                if other_ref not in visited:
                    frontier.append(other_ref)
    return visited


def run(
    state,
    seed_refdeses: list[str] | None = None,
    boundary_nets: list[str] | None = None,
    **_,
) -> dict:
    if not seed_refdeses:
        return {"status": "missing_input", "hint": "seed_refdeses is required."}
    if boundary_nets is None:
        boundary_nets = []
    if state.design_graph is None:
        return {
            "status": "no_active_schematic",
            "hint": "Call read_active_schematic before extract_subgraph.",
        }

    graph = state.design_graph
    seeds = set(seed_refdeses)
    unknown_seeds = [r for r in seeds if r not in graph.components]
    if unknown_seeds:
        return {
            "status": "unknown_refdes",
            "hint": (
                f"Seed refdeses not in the active schematic: "
                f"{sorted(unknown_seeds)}"
            ),
        }
    boundary_set = set(boundary_nets)

    visited = _bfs(graph, seeds, boundary_set)
    if not visited:
        return {"status": "empty_extraction", "hint": "BFS found no components."}

    # Collect nets touched by visited components. Partition into ports
    # (boundary or has endpoints outside `visited`) vs internal. Skip
    # explicit no-connect markers — they're not real nets.
    nets_touched: dict[str, list[tuple[str, str]]] = {}
    no_connects: dict[str, list[str]] = {}
    for ref in visited:
        comp = graph.components[ref]
        for pin_num, net_name in comp.pins.items():
            if _is_no_connect(net_name):
                no_connects.setdefault(ref, []).append(pin_num)
                continue
            nets_touched.setdefault(net_name, []).append((ref, pin_num))

    # Build the renaming map for auto-named internal nets.
    int_counter = 0
    rename: dict[str, str] = {}
    for net_name in sorted(nets_touched.keys()):
        if net_name in boundary_set:
            continue
        # External component on the net? → it's a port.
        net = graph.nets.get(net_name)
        external = False
        if net is not None:
            for pc in net.pins:
                if pc.component_ref not in visited:
                    external = True
                    break
        if external:
            continue
        if _is_auto_named(net_name):
            rename[net_name] = f"INT_{int_counter}"
            int_counter += 1

    netlist_nets: list[NetlistNet] = []
    for net_name, endpoints in sorted(nets_touched.items()):
        net = graph.nets.get(net_name)
        is_power = (
            net is not None
            and net.net_type in (NetType.POWER, NetType.GROUND)
        )
        voltage = net.voltage if net is not None else None

        if net_name in boundary_set:
            is_port = True
        elif net is not None and any(
            pc.component_ref not in visited for pc in net.pins
        ):
            is_port = True
        else:
            is_port = False

        # Keep only endpoints belonging to visited components.
        visible_endpoints = [
            NetlistEndpoint(ref=r, pin=p)
            for r, p in endpoints
            if r in visited
        ]
        if not visible_endpoints:
            continue

        out_name = rename.get(net_name, net_name)
        netlist_nets.append(
            NetlistNet(
                name=out_name,
                is_power=is_power,
                voltage=voltage,
                endpoints=visible_endpoints,
                is_port=is_port,
            )
        )

    # Build the parts list. Empty lib_id is fatal — the placer can't proceed.
    netlist_parts: list[NetlistPart] = []
    for ref in sorted(visited):
        c = graph.components[ref]
        if not c.lib_id:
            return {
                "status": "missing_lib_id",
                "refdes": ref,
                "hint": (
                    f"Component {ref} has no lib_id in the netlist. The "
                    "schematic's symbol library may be missing the source "
                    "definition. Consider re-saving the schematic in KiCad "
                    "or installing the symbol via install_symbol_to_project."
                ),
            }
        netlist_parts.append(
            NetlistPart(
                refdes=ref,
                lib_id=c.lib_id,
                value=c.value or "",
                footprint=c.footprint or "",
                mpn=c.mpn,
                no_connect_pins=sorted(no_connects.get(ref, [])),
            )
        )

    netlist = Netlist(parts=netlist_parts, nets=netlist_nets)

    return {
        "status": "ok",
        "netlist": netlist.model_dump(),
        "summary": {
            "parts": [p.refdes for p in netlist_parts],
            "ports": [n.name for n in netlist.ports()],
            "internal_nets": [
                n.name for n in netlist.nets if not n.is_port
            ],
        },
    }
