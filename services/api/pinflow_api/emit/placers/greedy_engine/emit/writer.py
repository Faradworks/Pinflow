"""Placement + CircuitGraph → .kicad_sch via kicad-sch-api.

Two phases:
  1. Add every PlacedComponent and PlacedPowerFlag to the schematic. ksa handles
     lib_symbols inclusion automatically when components are added from its cache.
  2. Route each net: connect the pin positions and power-flag positions on that
     net with a horizontal "rail" + vertical drops, with junctions where 3+ wires
     meet. Skip butt-connected nets (all endpoints at the same position) and
     single-endpoint nets.

The emitter does NOT decide layout — that's the placer's job. It just records
what the placer told it to record and routes the resulting connectivity.
"""

from __future__ import annotations

from pathlib import Path

import kicad_sch_api as ksa

from pinflow_api.emit.placers.greedy_engine.parse import CircuitGraph, Net
from pinflow_api.emit.placers.greedy_engine.place import Placement, PlacedComponent, PlacedPowerFlag
from pinflow_api.emit.placers.greedy_engine.symbols import SymbolLibrary, PlacedSymbol


GRID = 1.27


def _next_pwr_ref(counter: list[int]) -> str:
    """Generate #PWR001, #PWR002, ... in stable order."""
    counter[0] += 1
    return f"#PWR{counter[0]:03d}"


def _gather_endpoints(net: Net,
                      placement: Placement,
                      lib: SymbolLibrary,
                      placed_syms: dict[str, PlacedSymbol],
                      ) -> list[tuple[float, float, str]]:
    """Return the endpoints on `net` that still need routing.

    Power flags terminate a net via KiCad's global-net semantics — every
    "GND" symbol shares a net regardless of wires. So a pin butt-connected
    to a flag is already on the net and doesn't need a wire.

    For nets with at least one power flag:
      - Pins butt-connected to a flag (same position) are dropped.
      - Remaining (pin-only) positions need to reach a flag → return them
        plus every flag position so rail-routing connects them.
      - If no remaining pins, return [] (nothing to route — GND case).

    For signal nets (no flags), return every pin position; routing connects
    them all together.
    """
    pin_pts: list[tuple[float, float, str]] = []
    for node in net.nodes:
        comp = placement.by_ref(node.ref)
        if comp is None:
            continue
        sym = lib.get(comp.lib_id)
        placed = placed_syms.setdefault(
            comp.ref, sym.place(comp.x, comp.y, comp.rotation)
        )
        try:
            pin = placed.pin(node.pin)
        except KeyError:
            continue
        pin_pts.append((round(pin.x, 3), round(pin.y, 3), f"{node.ref}.{node.pin}"))

    flag_pts: list[tuple[float, float, str]] = []
    flag_positions: set[tuple[float, float]] = set()
    for f in placement.power_flags:
        if f.net == net.name:
            pt = (round(f.x, 3), round(f.y, 3))
            flag_pts.append((pt[0], pt[1], f"flag:{f.net}"))
            flag_positions.add(pt)

    if flag_positions:
        non_butt_pins = [(x, y, lbl) for x, y, lbl in pin_pts
                         if (x, y) not in flag_positions]
        if not non_butt_pins:
            return []
        return non_butt_pins + flag_pts

    return pin_pts


def _split_into_paired_groups(
    endpoints: list[tuple[float, float, str]],
) -> list[list[tuple[float, float, str]]]:
    """Split endpoints into pair groups + a rail group.

    Pairing is PARTIAL: each pin pairs with its nearest flag if that flag is
    within MIN_STUB_DIST. Pins that find a nearby flag become [pin, flag]
    pair groups (short-stub routing, no rail). Pins without a nearby flag
    fall into a rail group together with any flags that didn't pair off.

    Use case: decoupling-cap islands have their own +V/GND flag MIN_STUB
    from each cap pin, so they pair off and don't get drawn onto the same
    rail as the rest of the net. Meanwhile inline taps (e.g., an LED on
    +3V3) sit ON the rail with no per-pin flag — they stay in the rail
    group along with the source pin and the end-of-rail flag, and get
    routed together with rail+drops.
    """
    MIN_STUB_DIST = 2 * GRID + 0.001  # 2.54 mm + epsilon

    pins = [(x, y, lbl) for x, y, lbl in endpoints if not lbl.startswith("flag:")]
    flags = [(x, y, lbl) for x, y, lbl in endpoints if lbl.startswith("flag:")]

    if not pins or not flags:
        return [endpoints]

    used: set[int] = set()
    pairs: list[list[tuple[float, float, str]]] = []
    rail_pins: list[tuple[float, float, str]] = []
    for px, py, plbl in pins:
        best_i = None
        best_d = float("inf")
        for i, (fx, fy, _) in enumerate(flags):
            if i in used:
                continue
            d = abs(px - fx) + abs(py - fy)
            if d < best_d:
                best_d = d
                best_i = i
        if best_i is not None and best_d <= MIN_STUB_DIST:
            used.add(best_i)
            pairs.append([(px, py, plbl), flags[best_i]])
        else:
            rail_pins.append((px, py, plbl))

    # Unpaired pins + leftover flags get routed together with rail+drops.
    leftover_flags = [flags[i] for i in range(len(flags)) if i not in used]
    rail_group = rail_pins + leftover_flags
    if rail_group:
        pairs.append(rail_group)
    return pairs if pairs else [endpoints]


def _route_net(sch, endpoints: list[tuple[float, float, str]],
               rail_y_hint: float | None = None) -> tuple[int, int]:
    """Add wires + junctions to connect the endpoints. Returns (wires_added, junctions_added).

    Strategy:
      - 0 or 1 endpoints: nothing to route.
      - All endpoints at same (x, y): butt-connected, nothing to route.
      - All same Y: one horizontal wire from min_x to max_x.
      - All same X: one vertical wire from min_y to max_y.
      - Otherwise: pick a "rail Y" — if rail_y_hint matches an existing endpoint,
        use it (placer override); else use histogram (the Y with the most
        endpoints; ties favor the smallest Y, which usually matches the power
        rail). Draw the rail spanning min_x..max_x, then a vertical drop for
        each off-rail endpoint and a junction at the drop's intersection with
        the rail.
    """
    if len(endpoints) < 2:
        return (0, 0)

    unique_pts = {(x, y) for x, y, _ in endpoints}
    if len(unique_pts) == 1:
        return (0, 0)

    xs = sorted({x for x, _, _ in endpoints})
    ys_sorted = sorted({y for _, y, _ in endpoints})

    # All same Y → one horizontal wire, junctions at any mid-wire pins so
    # KiCad treats them as connected (pin in the middle of a wire is NOT
    # electrically connected; you need a junction or the wire split).
    if len(ys_sorted) == 1:
        rail_y = ys_sorted[0]
        sch.wires.add(start=(xs[0], rail_y), end=(xs[-1], rail_y))
        junction_pts = {
            (x, rail_y)
            for x, _, _ in endpoints
            if xs[0] + 0.001 < x < xs[-1] - 0.001
        }
        for jx, jy in junction_pts:
            sch.junctions.add(position=(jx, jy))
        return (1, len(junction_pts))
    # All same X → one vertical wire + mid-wire junctions
    if len(xs) == 1:
        rail_x = xs[0]
        sch.wires.add(start=(rail_x, ys_sorted[0]), end=(rail_x, ys_sorted[-1]))
        junction_pts = {
            (rail_x, y)
            for _, y, _ in endpoints
            if ys_sorted[0] + 0.001 < y < ys_sorted[-1] - 0.001
        }
        for jx, jy in junction_pts:
            sch.junctions.add(position=(jx, jy))
        return (1, len(junction_pts))

    # Rail + drops. Prefer the placer's hint when it lands on an endpoint;
    # otherwise fall back to a histogram of endpoint Ys.
    if rail_y_hint is not None and any(
        abs(y - rail_y_hint) < 0.001 for _, y, _ in endpoints
    ):
        rail_y = rail_y_hint
    else:
        y_counts: dict[float, int] = {}
        for _, y, _ in endpoints:
            y_counts[y] = y_counts.get(y, 0) + 1
        rail_y = sorted(y_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    # Rail: horizontal wire spanning min_x to max_x at rail_y.
    sch.wires.add(start=(xs[0], rail_y), end=(xs[-1], rail_y))
    wires_added = 1
    junction_pts: set[tuple[float, float]] = set()

    # Drops + junctions for each off-rail endpoint. A junction goes only
    # where 3+ wire segments meet — a drop at the rail's end-X is just an
    # L-bend (rail terminates, wire turns 90°) and KiCad renders that
    # cleanly without a dot. Mid-rail drops are 3-way (rail continues past)
    # and need the dot.
    for x, y, _label in endpoints:
        if abs(y - rail_y) < 0.001:
            if xs[0] + 0.001 < x < xs[-1] - 0.001:
                junction_pts.add((x, rail_y))
            continue
        sch.wires.add(start=(x, rail_y), end=(x, y))
        if xs[0] + 0.001 < x < xs[-1] - 0.001:
            junction_pts.add((x, rail_y))
        wires_added += 1

    for jx, jy in junction_pts:
        sch.junctions.add(position=(jx, jy))

    return (wires_added, len(junction_pts))


def write_schematic(placement: Placement,
                    cg: CircuitGraph,
                    lib: SymbolLibrary,
                    output_path: str | Path,
                    title: str | None = None) -> dict:
    """Build a .kicad_sch from placement+cg and save it. Returns a stats dict."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sch = ksa.create_schematic(title or output_path.stem)

    # Phase 1a: add components
    for c in placement.components:
        sch.components.add(
            lib_id=c.lib_id,
            reference=c.ref,
            value=c.value,
            position=(c.x, c.y),
            rotation=c.rotation,
            footprint=c.footprint or None,
        )

    # Phase 1b: add power flags. Non-standard power-net names (e.g. "+6V6")
    # don't have a matching power:<NET> symbol in KiCad's stock library, so
    # we fall back to power:+12V with the value overridden to display the
    # actual net name — KiCad treats the displayed value as the net name
    # via its global-power-symbol convention.
    pwr_counter = [0]
    pwr_flag_lookup: dict[tuple[str, float, float], str] = {}
    for f in placement.power_flags:
        ref = _next_pwr_ref(pwr_counter)
        try:
            sch.components.add(
                lib_id=f.lib_id,
                reference=ref,
                value=f.net,
                position=(f.x, f.y),
                rotation=f.rotation,
            )
        except Exception as e:
            if "not found" not in str(e).lower():
                raise
            sch.components.add(
                lib_id="power:+12V",
                reference=ref,
                value=f.net,
                position=(f.x, f.y),
                rotation=f.rotation,
            )
        pwr_flag_lookup[(f.net, f.x, f.y)] = ref

    # Phase 2: route nets. Cache placed-symbol info so we don't recompute it.
    placed_syms: dict[str, PlacedSymbol] = {}
    total_wires = 0
    total_junctions = 0
    per_net_stats: dict[str, tuple[int, int]] = {}
    for net in cg.nets:
        endpoints = _gather_endpoints(net, placement, lib, placed_syms)
        net_w, net_j = 0, 0
        rail_y_hint = placement.routing_hints.get(net.name)
        for group in _split_into_paired_groups(endpoints):
            w, j = _route_net(sch, group, rail_y_hint=rail_y_hint)
            net_w += w
            net_j += j
        total_wires += net_w
        total_junctions += net_j
        per_net_stats[net.name] = (net_w, net_j)

    sch.save(str(output_path))

    return {
        "output_path": str(output_path),
        "components": len(placement.components),
        "power_flags": len(placement.power_flags),
        "wires": total_wires,
        "junctions": total_junctions,
        "per_net": per_net_stats,
    }
