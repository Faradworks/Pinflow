"""Constraint-based placer — the production layout engine.

Archetype emitters emit declarative constraints — `Anchor`, `Offset`, `MinGap`
from `emit.constraints` — and a deterministic per-axis solver resolves them
into coordinates. A new topology emits a different *combination* of the same
primitives; the geometry generalises instead of being re-coded. Selected by
default via `emit.placers.get_placer()`.

Pipeline:

    netlist → build_layout_tree → place + measure parts (parking)
            → orient (caps rail-pin-up, series-filter element facing IC,
              divider legs rail→tap→ground)
            → emit constraints per archetype, sequence groups via MinGaps
            → solve(X) and solve(Y) independently
            → translate each part's parked cell to its solved origin
            → route + power-symbols (existing _place_connectivity)
            → emit .kicad_sch

The variable in each axis solve is a part's *symbol origin* (refdes → x or y).
The IC is anchored at a fixed (IC_X, IC_Y) before emission so its pins are
constants the emitters can reference for rail Ys, side edges, and trunks.
Within a group an emitter emits exact `Offset`s (a bank's even pitch, a
divider's stack, a cap's rail-pin offset from a rail Y). Between groups the
orchestrator threads a left/right *cursor*: each next group's first member
is tied to the previous group's last by a `MinGap` accounting for both
parts' extents and a comfortable gap — the solver then compacts them.

Scope: single-IC subcircuits. Zero/multi-IC defers to the legacy column
placer (`emit.placers.legacy.place`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import kicad_sch_api as ksa

from pinflow_api.builders._common import sch_to_string
from pinflow_api.emit import bbox
from pinflow_api.emit.classify import NetKind, Role
from pinflow_api.emit.constraints import Anchor, MinGap, Offset, solve
from pinflow_api.emit.layout_tree import (
    Archetype,
    GroupNode,
    LayoutTree,
    build_layout_tree,
)
from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.netlist_to_sch import (
    PlacerError,
    PlacerResult,
    _Cell,
    _hide_gnd_labels,
    _natural_key,
    _pin_xy,
    _place_and_measure,
    _place_connectivity,
    _place_no_connects,
    _snap,
    _topology_intact,
    place,
)
from pinflow_api.emit._placer_helpers import (
    CAP_GAP,
    COL_GAP,
    IC_X,
    IC_Y,
    STACK_GAP,
    TRUNK_GAP,
    _cap_pins,
    _group_ic_pins,
    _ic_pins_on_net,
    _ic_side_pin,
    _move_to,
    _order_rail_members,
    _orient_divider,
    _orient_series,
    _orient_to_axis,
    _orient_vertical,
    _part_pins_by_net,
    _reposition_fields,
    _side_edge_x,
    _translate,
    _trunk_y,
)


_SCH_HALF = 1.27   # one fine-grid step — the safe pin-coincidence nudge


# --- the constraint set, per axis -------------------------------------------

@dataclass
class _CSet:
    """Two constraint lists (X, Y) accumulated by the archetype emitters; the
    solver consumes each independently.

    Two anchor flavours:

      - `ax` / `ay` — origin anchors. Snap to the 2.54 mm grid because the
        value is a heuristic cursor position (column X, row Y below the IC).
        Without snap, ksa's `power:GND` autosnap-on-add disagrees with
        `comp.translate`'s no-snap behaviour and the wire to GND lands short.

      - `ax_pin` / `ay_pin` — *pin* anchors. Place the part so a named pin
        lands at `target_x` / `target_y`, with NO snap on origin. This is
        what keeps wires straight: a cap's lib pin offset is 3.81 mm
        (half-grid), so snapping the origin to 2.54 mm pulls the pin a
        half-grid step away from the on-grid IC pin it should connect to —
        forcing the router to insert a Z. Anchoring by pin position
        guarantees pins meet on the grid the IC pins live on; origins
        sometimes land at half-grid Y, which is fine because the wires
        connect by pin (on-grid) and the GND symbol is positioned from the
        pin too (also on-grid)."""

    x: list = field(default_factory=list)
    y: list = field(default_factory=list)

    def ax(self, var: str, value: float) -> None:
        self.x.append(Anchor(var, _snap(value)))

    def ay(self, var: str, value: float) -> None:
        self.y.append(Anchor(var, _snap(value)))

    def ax_pin(self, refdes: str, pin_num: str, target_x: float,
               parts: "dict[str, _Part]") -> None:
        off_x = parts[refdes].pin_off.get(pin_num, (0.0, 0.0))[0]
        self.x.append(Anchor(refdes, target_x - off_x))

    def ay_pin(self, refdes: str, pin_num: str, target_y: float,
               parts: "dict[str, _Part]") -> None:
        off_y = parts[refdes].pin_off.get(pin_num, (0.0, 0.0))[1]
        self.y.append(Anchor(refdes, target_y - off_y))

    def ox(self, a: str, b: str, delta: float) -> None:
        self.x.append(Offset(a, b, _snap(delta)))

    def oy(self, a: str, b: str, delta: float) -> None:
        self.y.append(Offset(a, b, _snap(delta)))

    def gx(self, a: str, b: str, gap: float) -> None:
        self.x.append(MinGap(a, b, _snap(gap)))

    def gy(self, a: str, b: str, gap: float) -> None:
        self.y.append(MinGap(a, b, _snap(gap)))


# --- measured part geometry --------------------------------------------------

@dataclass
class _Part:
    """A placed-and-oriented part: extents from origin (for non-overlap gaps)
    and pin offsets from origin (for pin-on-rail anchors)."""

    refdes: str
    cell: _Cell
    origin: tuple[float, float]
    leftext: float
    rightext: float
    topext: float
    botext: float
    pin_off: dict[str, tuple[float, float]]


def _measure_part(cell: _Cell) -> _Part | None:
    """Measure extents + pin offsets relative to the symbol origin. Called
    *after* any orientation, so the values reflect the part's final shape."""
    if not cell.members:
        return None
    comp = cell.members[0]
    ox, oy = float(comp.position.x), float(comp.position.y)
    pin_off: dict[str, tuple[float, float]] = {}
    for p in comp.pins:
        xy = _pin_xy(comp, str(p.number))
        if xy is not None:
            pin_off[str(p.number)] = (xy[0] - ox, xy[1] - oy)
    box = bbox.union_bbox(cell.members)
    if box is not None:
        leftext = ox - box[0]
        rightext = box[2] - ox
        topext = oy - box[1]
        botext = box[3] - oy
    else:
        # Fall back to the _place_and_measure half-extents centred on origin.
        leftext = rightext = cell.w / 2
        topext = botext = cell.h / 2
    return _Part(cell.refdes, cell, (ox, oy),
                 leftext, rightext, topext, botext, pin_off)


# --- emission context --------------------------------------------------------

@dataclass
class _Ctx:
    """Constants the emitters reference — derived from the anchored IC."""

    tree: LayoutTree
    netlist: Netlist
    ic_comp: object               # ksa Component
    anchor: str                   # the IC refdes (also the variable name)
    ic_origin: tuple[float, float]
    ic_leftext: float
    ic_rightext: float
    # Per-net trunk-Y hints emitted by archetypes that need the router to
    # honor a specific row. Populated by `_emit_signal_staircase`; consumed
    # by `_build_once` when calling `_place_connectivity`.
    rail_y_hints: dict[str, float] = field(default_factory=dict)
    # Nets that should be connected by label (name-reconciled in KiCad)
    # rather than by drawn wires. Used by SIGNAL_STAIRCASE — a staircase
    # row puts taps far from the IC's pin row, and trying to wire the L /
    # Z route past neighbouring tap bodies + GND symbols regularly clips
    # something and corrupts topology. Labels-by-name are short-safe and
    # the only cost is visual idiom (humans tend to wire short stubs).
    label_only_nets: set[str] = field(default_factory=set)


def _side_of(g: GroupNode, tree: LayoutTree) -> str:
    return g.side if g.side in ("L", "R") else (
        "R" if g.rail == tree.output_rail else "L"
    )


def _ext(ref: str, side: str, parts: dict[str, _Part], ctx: _Ctx) -> float:
    """Right- or left-extent of `ref` (refdes or the anchor)."""
    if ref == ctx.anchor:
        return ctx.ic_rightext if side == "R" else ctx.ic_leftext
    p = parts.get(ref)
    if p is None:
        return 0.0
    return p.rightext if side == "R" else p.leftext


def _gap(left_ref: str, right_ref: str, parts: dict[str, _Part], ctx: _Ctx,
         base: float) -> float:
    """Non-overlap gap between left_ref and right_ref: their extents pointing
    at each other, plus `base`."""
    return _ext(left_ref, "R", parts, ctx) + _ext(right_ref, "L", parts, ctx) + base


# --- archetype emitters ------------------------------------------------------

_CAP_ISLAND_ROW_GAP = 15.24       # mm: gap from IC body top to cap-island row Y
_CAP_ISLAND_LANE_GAP = 7.62       # mm: gap between adjacent cap-island lane bodies


def _emit_rail_bank(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                    cs: _CSet, left_neighbor: str) -> list[str]:
    """A horizontal cap bank, optionally with one or more series elements
    inline (the `series_filter` archetype). Caps' rail pins sit on the rail
    trunk; members evenly spaced; the chain anchored to `left_neighbor` via a
    MinGap. Returns the ordered member refdeses (IC-ward → outward).

    **Cap-island mode** (triggered when the IC also fans a SIGNAL_STAIRCASE):
    decoupling caps are physically lifted out of the side band into a row
    ABOVE the IC body — each cap a standalone island with its own `+V`
    label and GND drop, no shared rail trunk wire. The side band stays
    clear for BOOTSTRAP / CONTROL_RESISTOR parts that need to anchor at
    specific IC pins (BOOT cap, FSW resistor, EN pullup). A series element
    on the rail (a ferrite or coupling inductor) stays inline on the side
    trunk, since its job is to bridge two nets and its placement is
    constrained by both endpoints."""
    tree = ctx.tree
    side = _side_of(g, tree)
    direction = 1.0 if side == "R" else -1.0

    members = [r for r in _order_rail_members(g, tree) if r in parts]
    if not members:
        return []

    # Mark the rail labels-only when a SIGNAL_STAIRCASE fires on this IC.
    # Done at the archetype boundary (vs. in `_emit_all`) so the wiring
    # strategy stays with the layout choice that motivated it.
    has_staircase = any(g2.archetype == Archetype.SIGNAL_STAIRCASE
                         for g2 in tree.groups)
    if g.rail and has_staircase:
        ctx.label_only_nets.add(g.rail)

    if has_staircase and g.rail in ctx.label_only_nets:
        return _emit_rail_bank_islanded(g, members, parts, ctx, cs,
                                         side, direction, left_neighbor)

    # --- original side-trunk path (no staircase: rail banks render the
    # familiar horizontal-trunk-with-cap-fan-out idiom that matches the
    # human-drawn goldens for ams1117 / mt3608 / tps63020). -----------------
    ic_pins = _group_ic_pins(g, tree)
    trunk_y = _trunk_y(ctx.ic_comp, ic_pins, ctx.ic_origin[1])

    # Y: each member's connecting pin sits on the rail trunk.
    for r in members:
        pc = tree.plan.parts[r]
        if pc.role == Role.SERIES_ELEMENT:
            pin = "1"
        else:
            rail_pin, _ = _cap_pins(r, tree)
            pin = rail_pin or "1"
        cs.ay_pin(r, pin, trunk_y, parts)

    # X: even pitch, packed against the left neighbour.
    pitch = _snap(max(parts[r].cell.w for r in members) + CAP_GAP)
    first = members[0]                       # IC-ward (closest to neighbour)
    if side == "R":
        cs.gx(left_neighbor, first, _gap(left_neighbor, first, parts, ctx, TRUNK_GAP))
        for a, b in zip(members, members[1:]):
            cs.ox(a, b, pitch)
    else:
        cs.gx(first, left_neighbor, _gap(first, left_neighbor, parts, ctx, TRUNK_GAP))
        for a, b in zip(members, members[1:]):
            cs.ox(a, b, -pitch)
    return members


_CAP_ISLAND_EDGE_GAP = 25.4       # mm: gap from cap-island row end to series-element X
_SERIES_EDGE_Y_DROP = 7.62        # mm: how far below the row the series element sits


def _emit_rail_bank_islanded(g: GroupNode, members: list[str],
                              parts: dict[str, _Part], ctx: _Ctx, cs: _CSet,
                              side: str, direction: float,
                              left_neighbor: str) -> list[str]:
    """Cap-island variant: decoupling caps go in a row ABOVE the IC; any
    series element is pulled OUT to the far edge of the schematic on this
    side, with a `+V` label/symbol next to it — the "power-at-edge" idiom
    that hand-drawn schematics and `/examples` greedy both use to mark
    where the rail enters / exits the circuit.

    Layout:

        +V                             +V
         |    [cap]  [cap]  [cap]      |
        L1 ---|------|------|-----     L1 (vertical, far edge)
         |    GND    GND    GND        |
         (wired into IC's input pin)

    Caps in the row are labels-only on the rail (KiCad reconciles by
    name). The series element is at the FAR edge X, vertical, with its
    rail pin (pin 1 by convention) at the row Y so a horizontal label
    indicates the input/output source clearly.

    Keeping the side band clear (no series-on-trunk) is the whole point:
    BOOTSTRAP / CONTROL_RESISTOR parts have the side band to themselves
    and don't collide with the input filter."""
    tree = ctx.tree
    series = [r for r in members
              if tree.plan.parts[r].role == Role.SERIES_ELEMENT]
    caps = [r for r in members if r not in series]

    ic_part = parts.get(ctx.anchor)
    if ic_part is None:
        return members
    ic_top_y = ctx.ic_origin[1] - ic_part.topext
    # Clear the IC's top text fields (Reference / Value land at
    # ic_top - 5.08 and - 2.54) plus a cap body half-height plus the
    # `+V` symbol drawn above the cap's rail pin.
    row_y = _snap(ic_top_y - _CAP_ISLAND_ROW_GAP)

    # Lane X: starts at the IC body's outer edge on this side, extends
    # outward away from the body. Input filter (side L) extends left;
    # output bank (side R) extends right.
    if side == "R":
        lane_x = _snap(ctx.ic_origin[0] + ic_part.rightext
                       + _CAP_ISLAND_LANE_GAP)
    else:
        lane_x = _snap(ctx.ic_origin[0] - ic_part.leftext
                       - _CAP_ISLAND_LANE_GAP)

    # Place caps in the row.
    for r in caps:
        rail_pin, _ = _cap_pins(r, tree)
        pin = rail_pin or "1"
        cs.ay_pin(r, pin, row_y, parts)
        cs.ax_pin(r, pin, lane_x, parts)
        lane_x = _snap(lane_x + direction * (parts[r].leftext
                                              + parts[r].rightext
                                              + _CAP_ISLAND_LANE_GAP))

    # Place series element at the far edge X, but at the Y of its load-
    # side IC pin — short horizontal stub from L1's bottom to the IC,
    # not a long L-route from the cap row. The rail pin (top) ends up
    # above the IC's load pin level, and its `+V` label/symbol reads as
    # the source.
    for r in series:
        edge_x = _snap(lane_x + direction * _CAP_ISLAND_EDGE_GAP)
        load_pin, load_y = _series_load_target(r, ctx)
        if load_pin is not None and load_y is not None:
            cs.ay_pin(r, load_pin, load_y, parts)
            cs.ax_pin(r, load_pin, edge_x, parts)
        else:
            # Fallback: anchor pin 1 to the row line (old behaviour).
            cs.ay_pin(r, "1", row_y, parts)
            cs.ax_pin(r, "1", edge_x, parts)
        lane_x = _snap(edge_x + direction * (parts[r].leftext
                                              + parts[r].rightext
                                              + _CAP_ISLAND_LANE_GAP))

    return list(caps) + list(series)


def _emit_divider(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                  cs: _CSet, left_neighbor: str) -> list[str]:
    """A two-resistor feedback divider — high leg's rail pin on the output
    trunk; low leg directly below; both share an X. Returns [high, low]."""
    tree = ctx.tree
    div = next((d for d in tree.plan.dividers
                if set(g.members) == {d.high_refdes, d.low_refdes}), None)
    legs = ([div.high_refdes, div.low_refdes] if div
            else sorted(g.members, key=_natural_key))
    legs = [r for r in legs if r in parts]
    if not legs:
        return []

    # Y: high leg's rail pin (sensed net) on the output rail; low leg below.
    if div is not None:
        out_pins = (_ic_pins_on_net(tree.output_rail, tree.anchor, tree.netlist)
                    if tree.output_rail else [])
        top_y = _trunk_y(ctx.ic_comp, out_pins or _group_ic_pins(g, tree),
                         ctx.ic_origin[1])
        high = legs[0]
        rail_pin = _part_pins_by_net(high, tree.netlist).get(div.sensed_net)
        if rail_pin:
            cs.ay_pin(high, rail_pin, top_y, parts)
        else:
            cs.ay(high, top_y)
        for a, b in zip(legs, legs[1:]):
            drop = parts[a].botext + parts[b].topext + STACK_GAP
            cs.oy(a, b, drop)
    else:
        # No DividerGroup metadata — fall back to a tight vertical stack.
        for a, b in zip(legs, legs[1:]):
            drop = parts[a].botext + parts[b].topext + STACK_GAP
            cs.oy(a, b, drop)

    # X: legs share one X, sequenced after the left neighbour.
    high = legs[0]
    cs.gx(left_neighbor, high, _gap(left_neighbor, high, parts, ctx, COL_GAP))
    for a, b in zip(legs, legs[1:]):
        cs.ox(a, b, 0.0)
    return legs


def _emit_inductor(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                   cs: _CSet, left_neighbor: str) -> list[str]:
    """The power inductor(s) — anchored at the mean Y of the switch-node IC
    pins they bridge, sequenced after the left neighbour in X."""
    tree = ctx.tree
    members = sorted((r for r in g.members if r in parts), key=_natural_key)
    if not members:
        return []
    sw_y = _trunk_y(ctx.ic_comp, _group_ic_pins(g, tree), ctx.ic_origin[1])
    for r in members:
        # Use the IC-facing pin so its sch Y lands on the SW trunk; series
        # inductors are placed horizontal (pin Y == origin Y, no offset) so
        # the choice rarely matters, but a vertical inductor would otherwise
        # land half a grid off the trunk.
        pin = "1"
        cs.ay_pin(r, pin, sw_y, parts)
    first = members[0]
    cs.gx(left_neighbor, first, _gap(left_neighbor, first, parts, ctx, COL_GAP))
    for a, b in zip(members, members[1:]):
        cs.ox(a, b, parts[a].rightext + parts[b].leftext + COL_GAP)
    return members


def _series_rail_pin(refdes: str, tree: LayoutTree) -> str | None:
    """For a SERIES_ELEMENT in a rail bank, the pin connected to the rail
    served by this bank (the `+V` source side). The other pin is the load
    side that wires into the IC."""
    pc = tree.plan.parts.get(refdes)
    if pc is None:
        return None
    for pin_num, net_name in pc.pins.items():
        if net_name in (tree.input_rail, tree.output_rail):
            return pin_num
    return None


def _series_load_target(refdes: str, ctx: _Ctx) -> tuple[str | None, float | None]:
    """For a SERIES_ELEMENT in cap-island mode, find the load-side part-pin
    and the Y of the IC pin it connects to. Anchoring the load pin at
    that Y makes the wire from L1 to the IC short and horizontal — the
    "power-at-edge" idiom where the inductor sits at the page edge with
    only a short stub into the IC, not a long L-route from the cap row."""
    pc = ctx.tree.plan.parts.get(refdes)
    if pc is None or ctx.anchor is None:
        return None, None
    for pin_num, net_name in pc.pins.items():
        # The rail side is in label_only_nets; the load side is the other.
        if net_name in (ctx.tree.input_rail, ctx.tree.output_rail):
            continue
        nc = ctx.tree.plan.nets.get(net_name)
        if nc is None:
            continue
        for c in nc.ic_contacts:
            if c.ic_refdes == ctx.anchor:
                xy = _pin_xy(ctx.ic_comp, c.pin.number)
                if xy is not None:
                    return pin_num, xy[1]
    return None, None


def _ctrl_pin_for(refdes: str, tree: LayoutTree) -> str | None:
    """The IC pin a 2-pin cap's non-ground side connects to — its control
    pin. None if the cap touches no IC pin on a non-ground net."""
    for net_name, _pin in _part_pins_by_net(refdes, tree.netlist).items():
        nc = tree.plan.nets.get(net_name)
        if nc is None or nc.kind == NetKind.GROUND:
            continue
        ic_pins = _ic_pins_on_net(net_name, tree.anchor, tree.netlist)
        if ic_pins:
            return ic_pins[0]
    return None


# Staircase row geometry — when ≥2 single-IC-pin taps cluster on the same
# horizontal IC side, drop them into a row below the IC body. Idea + constants
# ported from /examples greedy placer (commit 996f5bc). Trunk Y for each
# net's wire is the *source IC pin's Y* (above the row), conveyed to the
# router via `rail_y_hints` so the wire doesn't cut through neighbouring
# tap bodies.
_STAIRCASE_ROW_GAP = 12.70        # mm: gap from IC body bottom to row Y
_STAIRCASE_LANE_GAP = 5.08        # mm: gap between adjacent lane bodies
_STAIRCASE_START_GAP = 5.08       # mm: gap from IC side edge to first lane


def _staircase_conn_info(refdes: str, tree: LayoutTree
                          ) -> tuple[str, str] | None:
    """`(part_pin_num, ic_signal_net_name)` for a staircase-eligible tap.
    The part's pin on its IC-signal net is the connecting pin (anchored at
    `(lane_x, row_y)` in the row); the net name tells the orchestrator
    which router rail to pin to the source pin's Y."""
    pc = tree.plan.parts.get(refdes)
    if pc is None or tree.anchor is None:
        return None
    for pin_num, net_name in pc.pins.items():
        nc = tree.plan.nets.get(net_name)
        if nc is None or nc.kind in (NetKind.GROUND, NetKind.RAIL):
            continue
        if any(c.ic_refdes == tree.anchor for c in nc.ic_contacts):
            return pin_num, net_name
    return None


def _emit_signal_staircase(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                            cs: _CSet) -> list[tuple[str, str]]:
    """Place a same-side cluster of single-IC-pin taps as a horizontal row
    at `row_y = ic_max_y + _STAIRCASE_ROW_GAP`. Each tap's IC-connecting
    pin lands at `(lane_x, row_y)`; lane_x fans outward from the IC side
    edge so no two taps share an X column.

    Returns `[(refdes, ic_signal_net_name), ...]` for the orchestrator to
    build `rail_y_hints` from — the router needs to know each net's trunk
    runs at the source IC pin's Y, not at the row_y where the bodies sit.
    Without that hint, the router routes the trunk between IC pin and tap
    pin at the median Y (≈ midway between source_y and row_y), and that
    median row cuts through neighbouring tap bodies.

    Members arrive in cluster order (bottom-most source pin first)."""
    members = [r for r in g.members if r in parts]
    if not members:
        return []
    side = g.side if g.side in ("L", "R") else "R"
    direction = 1.0 if side == "R" else -1.0
    edge_x = _side_edge_x(ctx.ic_comp, side)
    ic_part = parts.get(ctx.anchor)
    ic_bot_y = ctx.ic_origin[1] + (ic_part.botext if ic_part else 0.0)
    row_y = _snap(ic_bot_y + _STAIRCASE_ROW_GAP)
    lane_x = edge_x + direction * _STAIRCASE_START_GAP

    placed: list[tuple[str, str]] = []
    for r in members:
        info = _staircase_conn_info(r, ctx.tree)
        if info is None:
            continue
        conn_pin, net_name = info
        # Y: connecting pin sits on the shared row line.
        cs.ay_pin(r, conn_pin, row_y, parts)
        # X: connecting pin at the current lane.
        cs.ax_pin(r, conn_pin, lane_x, parts)
        # Connect this tap to its IC pin by *label*, not by wire — the
        # staircase row sits below the IC pin column, so any drawn wire
        # would risk clipping a neighbouring tap body or GND symbol. The
        # rail_y_hint stays (in case a future variant wires these),
        # but `label_only_nets` is what the router actually honors today.
        ctx.label_only_nets.add(net_name)
        # Also label-only any *other* net this tap touches that isn't
        # GND / rail — covers the COMP → R9 → C19 case where R9's pin 2
        # is on a non-IC inter-part net connecting to C19 in CONFIG_CAP.
        # Without this, the router would draw a long wire across the
        # staircase row to reach C19.
        pc = ctx.tree.plan.parts.get(r)
        if pc is not None:
            for other_net in pc.nets:
                if other_net == net_name:
                    continue
                onc = ctx.tree.plan.nets.get(other_net)
                if onc is None or onc.kind in (NetKind.GROUND, NetKind.RAIL):
                    continue
                ctx.label_only_nets.add(other_net)
        # Tell the router: this net's trunk runs at the source IC pin's Y,
        # not at row_y. Kept even though the net is now labels-only — if
        # the labels-only branch ever flips back to wired, the hint avoids
        # the body-clipping median-Y trunk.
        ic_pin_num = _ctrl_pin_for(r, ctx.tree)
        ic_xy = _pin_xy(ctx.ic_comp, ic_pin_num) if ic_pin_num else None
        if ic_xy is not None:
            ctx.rail_y_hints[net_name] = ic_xy[1]
        placed.append((r, net_name))
        # Snap the next lane to the grid. Without this, `leftext + rightext`
        # picks up the part's field-label extents (often not on the 1.27 mm
        # grid), and the next lane lands off-grid. ksa snaps power-symbol
        # positions to the grid, so the snapped GND symbol's pin would not
        # coincide with the unsnapped tap's GND pin — the wire between them
        # becomes diagonal and KiCad drops the connectivity.
        lane_x = _snap(lane_x + direction * (parts[r].leftext
                                              + parts[r].rightext
                                              + _STAIRCASE_LANE_GAP))
    return placed


def _emit_config(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                 cs: _CSet) -> int:
    """Config caps — each placed at *its own* control pin's Y, X outside the
    IC side-pin column. Spreading caps across their distinct control pins
    avoids stacking four bypass caps on one Y line (the dense-IC failure
    mode); the router draws a short horizontal stub from each cap's rail pin
    to its pin.

    Pin-coincidence column bump: a tall cap (KLC `Device:C` spans 7.62 mm
    pin-to-pin) places its GND pin exactly 7.62 mm below its rail pin. When
    two control pins on the IC sit ≤ 7.62 mm apart in Y, cap A's pin 2
    coincides with cap B's pin 1 and KiCad merges SS into GND via the
    coincident point — a silent short. The later cap is pushed one COL_GAP
    further out into a new column."""
    tree = ctx.tree
    members = sorted((r for r in g.members if r in parts), key=_natural_key)
    if not members:
        return 0
    side = _side_of(g, tree)
    edge_x = _side_edge_x(ctx.ic_comp, side)

    placed: list[tuple[int, float, float]] = []  # (col, y, pin-to-pin span)
    max_col = 0

    for r in members:
        ctrl_pin = _ctrl_pin_for(r, tree)
        ctrl_xy = _pin_xy(ctx.ic_comp, ctrl_pin) if ctrl_pin else None
        ctrl_y = ctrl_xy[1] if ctrl_xy else ctx.ic_origin[1]
        rail_pin, _ = _cap_pins(r, tree)
        off = (parts[r].pin_off.get(rail_pin, (0.0, 0.0))
               if rail_pin else (0.0, 0.0))
        # Origin Y the cap will land at; tracked for overlap collision so
        # caps that would share Y get bumped to the next column.
        target_y = ctrl_y - off[1]
        span = parts[r].topext + parts[r].botext

        col = 0
        while any(c == col and abs(target_y - py) <= ps + _EPS
                  for (c, py, ps) in placed):
            col += 1
        placed.append((col, target_y, span))
        max_col = max(max_col, col)

        if rail_pin:
            cs.ay_pin(r, rail_pin, ctrl_y, parts)
        else:
            cs.ay(r, ctrl_y)
        x_step = TRUNK_GAP + COL_GAP * col
        if side == "R":
            cs.ax(r, edge_x + x_step + parts[r].leftext - off[0])
        else:
            cs.ax(r, edge_x - x_step - parts[r].rightext - off[0])
    return max_col + 1   # number of columns this group occupied


_EPS = 0.01


def _pin_info(ctx: _Ctx, pin_num: str):
    """Look up `pin_num`'s PinInfo (name / side / etype) on the anchor IC.
    None if not found — the IC has no pinmap or the number doesn't match."""
    if ctx.anchor is None:
        return None
    pins = ctx.tree.plan.pinmaps.get(ctx.anchor) or []
    for pi in pins:
        if pi.number == pin_num:
            return pi
    return None


def _ic_signal_nets(refdes: str, ctx: _Ctx) -> list[str]:
    """Non-rail / non-ground IC-touching nets `refdes` connects to — one
    entry per net regardless of how many IC pins that net contacts (a
    multi-pin SW node should not inflate the count). Used by the control-
    resistor and bootstrap emitters to find the anchor pin(s)."""
    pc = ctx.tree.plan.parts.get(refdes)
    if pc is None or ctx.anchor is None:
        return []
    out: list[str] = []
    for net_name in pc.nets:
        nc = ctx.tree.plan.nets.get(net_name)
        if nc is None or nc.kind in (NetKind.GROUND, NetKind.RAIL):
            continue
        if any(c.ic_refdes == ctx.anchor for c in nc.ic_contacts):
            out.append(net_name)
    return out


def _ic_pin_for_net(refdes: str, net_name: str, ctx: _Ctx) -> str | None:
    """Pick a representative IC pin number for `net_name` on the anchor — the
    first contact found. For multi-pin signals like SW the choice is
    arbitrary but consistent; the placer uses it for the anchor coordinate."""
    nc = ctx.tree.plan.nets.get(net_name)
    if nc is None or ctx.anchor is None:
        return None
    for c in nc.ic_contacts:
        if c.ic_refdes == ctx.anchor:
            return c.pin.number
    return None


def _side_column_offset(side: str, config_cols: dict[str, int],
                        has_filter: set[str],
                        bootstrap_cols: dict[str, int] | None = None) -> float:
    """Extra X-offset past the IC-edge trunk column to clear earlier
    archetypes already living on `side`. A rail filter (input filter /
    output bank) gets one COL_GAP — enough to clear the cap body without
    overshooting past the next cap in the chain. Config caps add one
    COL_GAP per column they actually used (configs Y-bump to a new column
    when their pin-coincidence check fires). Bootstrap parts add one
    COL_GAP per same-side bootstrap so a CONTROL_RESISTOR at an adjacent
    IC pin (e.g. R3 on FSW next to C8 BOOT↔SW on tps61088) doesn't land
    in the same column and clip the bootstrap body."""
    extra = 0.0
    if side in has_filter:
        extra += COL_GAP
    n_cfg = config_cols.get(side, 0)
    if n_cfg:
        extra += COL_GAP * n_cfg
    if bootstrap_cols:
        n_boot = bootstrap_cols.get(side, 0)
        if n_boot:
            extra += COL_GAP * n_boot
    return extra


def _emit_control_resistor(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                           cs: _CSet, config_cols: dict[str, int],
                           has_filter: set[str],
                           bootstrap_cols: dict[str, int] | None = None,
                           side_edge: dict[str, str] | None = None) -> None:
    """Control / pull resistors — each placed at *its own* IC control pin's
    Y or X, just past the IC body on the side that pin lives on (L/R/T/B).
    The X offset compounds: a same-side rail filter pushes the resistor past
    the filter caps; a same-side config-cap group adds another column step.
    Without this, an EN pull-up on a buck-converter's LEFT side lands in the
    same X column as the input filter cap and overlaps with it (mt3608).

    When the side's outermost band occupant is known (`side_edge`) and no
    config/bootstrap columns complicate the side, the resistor clears it
    with an extent-aware MinGap instead of the fixed column guess — a fixed
    COL_GAP can't know how far the bank actually reaches."""
    ic_part = parts.get(ctx.anchor)
    if ic_part is None:
        return
    for r in sorted((r for r in g.members if r in parts), key=_natural_key):
        nets = _ic_signal_nets(r, ctx)
        if not nets:
            continue
        ctrl_pin = _ic_pin_for_net(r, nets[0], ctx)
        if ctrl_pin is None:
            continue
        ctrl_xy = _pin_xy(ctx.ic_comp, ctrl_pin)
        pi = _pin_info(ctx, ctrl_pin)
        if ctrl_xy is None or pi is None:
            continue
        # The resistor's own pin Y-offset from origin (zero for horizontal,
        # ±h/2 for vertical) — used to put its IC-side pin on the IC pin Y.
        pin_off = parts[r].pin_off
        ic_facing = "1" if pi.side == "R" else "2"
        off = pin_off.get(ic_facing, (0.0, 0.0))
        if pi.side in ("L", "R"):
            cs.ay_pin(r, ic_facing, ctrl_xy[1], parts)
            edge_ref = (side_edge or {}).get(pi.side)
            side_uncrowded = (
                not config_cols.get(pi.side)
                and not (bootstrap_cols or {}).get(pi.side)
            )
            if edge_ref is not None and edge_ref in parts and side_uncrowded:
                # Extent-aware: pack just past the band's outermost member.
                if pi.side == "R":
                    cs.gx(edge_ref, r, _gap(edge_ref, r, parts, ctx, STACK_GAP))
                else:
                    cs.gx(r, edge_ref, _gap(r, edge_ref, parts, ctx, STACK_GAP))
                continue
            edge_x = _side_edge_x(ctx.ic_comp, pi.side)
            extra = _side_column_offset(pi.side, config_cols, has_filter,
                                         bootstrap_cols)
            if pi.side == "R":
                cs.ax(r, edge_x + TRUNK_GAP + extra + parts[r].leftext)
            else:
                cs.ax(r, edge_x - TRUNK_GAP - extra - parts[r].rightext)
        else:  # T / B — pin on top / bottom edge of the IC
            cs.ax_pin(r, ic_facing, ctrl_xy[0], parts)
            top_edge = ctx.ic_origin[1] - ic_part.topext
            bot_edge = ctx.ic_origin[1] + ic_part.botext
            if pi.side == "T":
                cs.ay(r, top_edge - TRUNK_GAP - parts[r].botext)
            else:
                cs.ay(r, bot_edge + TRUNK_GAP + parts[r].topext)


def _emit_bootstrap(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                    cs: _CSet, has_filter: set[str]) -> None:
    """A 2-pin part bridging two IC signal nets — bootstrap cap (BOOT↔SW),
    FSW resistor (FSW↔SW), or similar. Placement strategy depends on which
    sides of the IC the two pins are on:

      - **Same side** (both L, both R): the part sits *on that side* at the
        Y midpoint of the two pin Ys — gives each bootstrap its own distinct
        Y so multiple bootstraps don't stack at the same coordinate.
        Multiple same-side bootstraps stagger by COL_GAP per index so their
        bodies don't collide (e.g. tps61088: BOOT cap C8 at BOOT/SW midpoint
        Y and FSW resistor R3 at FSW/SW midpoint Y are only one pin-pitch
        apart in Y, so they need different X columns).
      - **Different sides**: the part sits *above the IC*, X-centred between
        the two pin Xs.
    """
    ic_part = parts.get(ctx.anchor)
    if ic_part is None:
        return
    same_side_idx: dict[str, int] = {"L": 0, "R": 0}
    for r in sorted((r for r in g.members if r in parts), key=_natural_key):
        nets = _ic_signal_nets(r, ctx)
        if len(nets) < 2:
            continue
        pin_nums = [_ic_pin_for_net(r, n, ctx) for n in nets[:2]]
        pin_nums = [pn for pn in pin_nums if pn is not None]
        if len(pin_nums) < 2:
            continue
        xys = [_pin_xy(ctx.ic_comp, pn) for pn in pin_nums]
        sides = [_pin_info(ctx, pn).side if _pin_info(ctx, pn) else None
                 for pn in pin_nums]
        if any(xy is None for xy in xys):
            continue

        if sides[0] == sides[1] and sides[0] in ("L", "R"):
            # Same vertical side — place outside that edge at the pin midpoint
            side = sides[0]
            edge_x = _side_edge_x(ctx.ic_comp, side)
            extra = (_side_column_offset(side, {}, has_filter)
                     + same_side_idx[side] * COL_GAP)
            same_side_idx[side] += 1
            mid_y = (xys[0][1] + xys[1][1]) / 2
            if side == "R":
                cs.ax(r, edge_x + TRUNK_GAP + extra + parts[r].leftext)
            else:
                cs.ax(r, edge_x - TRUNK_GAP - extra - parts[r].rightext)
            cs.ay(r, mid_y)
        else:
            # Different sides — sit above the IC, centred between the two Xs.
            mid_x = (xys[0][0] + xys[1][0]) / 2
            top_edge = ctx.ic_origin[1] - ic_part.topext
            cs.ax(r, mid_x)
            cs.ay(r, top_edge - TRUNK_GAP - parts[r].botext)


def _emit_shunt_branch(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                       cs: _CSet, has_config: dict[str, int],
                       has_filter: set[str]) -> None:
    """Place a shunt chain (rail → series of 2-pin parts → GND) as a
    vertical stack on the rail's side, just past any rail bank that
    shares the side. Member order in g.members is natural (alphabetical
    by refdes); we order them top-down by walking from the rail end.

    The chain's top member connects to the rail trunk Y; each subsequent
    member stacks below it; the last member drops to GND. Each part is
    oriented vertically with its rail-side pin at the top so the wire
    falls straight down."""
    tree = ctx.tree
    members = [r for r in g.members if r in parts]
    if not members:
        return
    # Walk: find the part whose pin connects to the rail (rail_end), then
    # follow internal nets to the part connected to GND.
    rail_net = g.rail
    if rail_net is None:
        # Without a rail, fall back to natural order.
        ordered = sorted(members, key=_natural_key)
    else:
        # Build a quick "which parts each net touches" within the group.
        ordered = _order_shunt_chain(members, tree, rail_net)

    side = "R" if g.side != "L" else "L"
    edge_x = _side_edge_x(ctx.ic_comp, side)
    extra = _side_column_offset(side, has_config, has_filter)
    # Place column just past the rail filter on this side, beyond any
    # config-cap columns. One COL_GAP further out than a regular config.
    if side == "R":
        col_x = edge_x + TRUNK_GAP + extra + COL_GAP
    else:
        col_x = edge_x - TRUNK_GAP - extra - COL_GAP

    # Find the rail trunk Y (where the top of the chain attaches).
    rail_pins = (_ic_pins_on_net(rail_net, ctx.anchor, tree.netlist)
                 if rail_net else [])
    trunk_y = _trunk_y(ctx.ic_comp, rail_pins or [], ctx.ic_origin[1])

    # First member: its rail-side pin (the one on `rail_net`) lands at
    # trunk_y. Using `ay_pin` keeps that pin exactly on the rail trunk —
    # without it, snapping origin to 2.54 mm grid drops the half-grid pin
    # offset 1.27 mm off the trunk and the router has to bridge with a Z.
    first = ordered[0]
    first_part = parts[first]
    first_pc = tree.plan.parts.get(first)
    rail_pin = None
    if first_pc and rail_net:
        rail_pin = next(
            (pin_num for pin_num, net_name in first_pc.pins.items()
             if net_name == rail_net),
            None,
        )
    cs.ax(first, col_x)
    if rail_pin is not None:
        cs.ay_pin(first, rail_pin, trunk_y, parts)
    else:
        cs.ay(first, trunk_y + first_part.topext)

    # Stack the rest with STACK_GAP between bbox edges.
    for a, b in zip(ordered, ordered[1:]):
        cs.ax(b, col_x)
        drop = parts[a].botext + parts[b].topext + STACK_GAP
        cs.oy(a, b, drop)


def _order_shunt_chain(members: list[str], tree: LayoutTree,
                       rail_net: str) -> list[str]:
    """Order a shunt chain top-down: the rail-touching part first, then
    walk through internal nets to the GND-touching part."""
    # Build adjacency over internal signal nets (not rail, not GND).
    by_net: dict[str, list[str]] = {}
    for r in members:
        pc = tree.plan.parts.get(r)
        if pc is None:
            continue
        for net_name in pc.nets:
            nc = tree.plan.nets.get(net_name)
            if nc is None:
                continue
            if nc.kind in (NetKind.GROUND, NetKind.RAIL):
                continue
            by_net.setdefault(net_name, []).append(r)

    # Find the rail-touching seed (the one whose nets include rail_net).
    seed = None
    for r in members:
        pc = tree.plan.parts.get(r)
        if pc and rail_net in pc.nets:
            seed = r
            break
    if seed is None:
        return sorted(members, key=_natural_key)

    visited: set[str] = {seed}
    order = [seed]
    cur = seed
    while True:
        nxt = None
        pc_cur = tree.plan.parts.get(cur)
        if pc_cur is None:
            break
        for net_name in pc_cur.nets:
            for other in by_net.get(net_name, []):
                if other != cur and other not in visited:
                    nxt = other
                    break
            if nxt:
                break
        if nxt is None:
            break
        order.append(nxt)
        visited.add(nxt)
        cur = nxt
    # Tail: any unreached member appended in natural order (rare).
    for r in members:
        if r not in visited:
            order.append(r)
    return order


def _emit_loose(g: GroupNode, parts: dict[str, _Part], ctx: _Ctx,
                cs: _CSet) -> None:
    """Fallback: parts the rules did not group sit in a vertical column below
    the IC. Crude on purpose — a placer that finds genuine archetypes hits
    this rarely."""
    members = sorted((r for r in g.members if r in parts), key=_natural_key)
    if not members:
        return
    col_x = ctx.ic_origin[0]
    first_y = ctx.ic_origin[1] + ctx.ic_rightext + COL_GAP * 2
    for i, r in enumerate(members):
        cs.ax(r, col_x)
        if i == 0:
            cs.ay(r, first_y + parts[r].topext)
        else:
            prev = members[i - 1]
            drop = parts[prev].botext + parts[r].topext + STACK_GAP
            cs.oy(prev, r, drop)


def _orient_all(tree: LayoutTree, by_ref: dict[str, _Cell],
                ic_comp: object) -> None:
    """Apply per-archetype orientations to the parked cells. Done before
    measurement so pin offsets reflect each part's final orientation."""
    for g in tree.groups:
        if g.archetype == Archetype.DIVIDER_STACK:
            div = next((d for d in tree.plan.dividers
                        if set(g.members) == {d.high_refdes, d.low_refdes}),
                       None)
            if div is not None:
                _orient_divider(div, tree, by_ref)
        elif g.archetype in (Archetype.SERIES_FILTER, Archetype.RAIL_CAP_BANK):
            side = _side_of(g, tree)
            cap_island = any(g2.archetype == Archetype.SIGNAL_STAIRCASE
                             for g2 in tree.groups)
            for r in g.members:
                if r not in by_ref:
                    continue
                pc = tree.plan.parts[r]
                if pc.role == Role.SERIES_ELEMENT:
                    if cap_island:
                        # Cap-island mode: orient vertical with rail-pin on
                        # top so the `+V` label sits above the inductor and
                        # the load pin (bottom) anchors to the IC's load Y.
                        rail_pin = _series_rail_pin(r, tree)
                        other = next(
                            (p for p in pc.pins if p != rail_pin), None
                        )
                        if rail_pin and other:
                            _orient_to_axis(by_ref[r], rail_pin, other,
                                             "vertical")
                    else:
                        _orient_series(by_ref[r], _ic_side_pin(r, tree), side)
                else:
                    rp, gp = _cap_pins(r, tree)
                    if rp and gp:
                        _orient_vertical(by_ref[r], rp, gp)
        elif g.archetype == Archetype.CONFIG_CAP:
            for r in g.members:
                if r not in by_ref:
                    continue
                rp, gp = _cap_pins(r, tree)
                if rp and gp:
                    _orient_vertical(by_ref[r], rp, gp)
        elif g.archetype == Archetype.SHUNT_BRANCH:
            # Each member is a 2-pin part on the chain rail→…→GND. Orient so
            # the pin closer to the rail (top of the chain) is pin-up.
            # For the rail end: pin on the rail net up.
            # For the GND end: pin on the rail-facing internal net up.
            # For intermediate members: pin facing the previous (up the
            # chain toward the rail) is up.
            ordered = _order_shunt_chain(g.members, tree, g.rail or "")
            top_net = g.rail
            for r in ordered:
                if r not in by_ref:
                    continue
                pc = tree.plan.parts.get(r)
                if pc is None or len(pc.nets) != 2:
                    continue
                # `top_net` is the net that should be at the upper pin of
                # this part. Find which pin of the part is on top_net.
                top_pin = next(
                    (pin_num for pin_num, net_name in pc.pins.items()
                     if net_name == top_net),
                    None,
                )
                if top_pin is None:
                    continue
                bottom_pin = next(
                    (pin_num for pin_num in pc.pins
                     if pin_num != top_pin),
                    None,
                )
                if bottom_pin is None:
                    continue
                # `_orient_to_axis` handles parts whose library default is
                # horizontal (e.g. Device:LED has pins at lib (±3.81, 0)) by
                # rotating 90° before the 180° flip — otherwise an LED in a
                # shunt chain kinks horizontally between two vertical legs.
                _orient_to_axis(by_ref[r], top_pin, bottom_pin, "vertical")
                # The bottom net of this part is the top net of the next.
                top_net = pc.pins.get(bottom_pin)
        elif g.archetype == Archetype.BOOTSTRAP:
            # 2-pin part bridging two IC signal pins on the same side. Orient
            # so the part-pin connecting to the upper (smaller-Y) IC pin ends
            # up on top — saves the router from having to swap pin order via
            # an extra crossing. Diff-side bootstraps (both IC pins on the
            # same Y) sit above the IC and stay vertical; we leave them.
            for r in g.members:
                cell = by_ref.get(r)
                if cell is None or len(cell.members) != 1:
                    continue
                pc = tree.plan.parts.get(r)
                if pc is None or len(pc.pins) != 2:
                    continue
                ic_targets: list[tuple[str, tuple[float, float]]] = []
                for pin_num, net_name in pc.pins.items():
                    nc = tree.plan.nets.get(net_name)
                    if nc is None or nc.kind in (NetKind.GROUND, NetKind.RAIL):
                        continue
                    ic_pin = next((c.pin.number for c in nc.ic_contacts
                                   if c.ic_refdes == tree.anchor), None)
                    if ic_pin is None:
                        continue
                    xy = _pin_xy(ic_comp, ic_pin)
                    if xy is None:
                        continue
                    ic_targets.append((pin_num, xy))
                if len(ic_targets) != 2:
                    continue
                upper = min(ic_targets, key=lambda t: t[1][1])
                lower = next(t for t in ic_targets if t is not upper)
                if abs(upper[1][1] - lower[1][1]) >= _EPS:
                    _orient_vertical(cell, upper[0], lower[0])
        elif g.archetype == Archetype.SIGNAL_STAIRCASE:
            # Vertical orientation with the IC-connecting pin on TOP — the
            # tap hangs below the row line with its conn pin facing up
            # toward the source IC pin. The other pin (typically GND) ends
            # up at the bottom, natural for the standard GND drop.
            for r in g.members:
                cell = by_ref.get(r)
                if cell is None or len(cell.members) != 1:
                    continue
                pc = tree.plan.parts.get(r)
                if pc is None or len(pc.pins) != 2:
                    continue
                info = _staircase_conn_info(r, tree)
                if info is None:
                    continue
                conn_pin, _ = info
                other = next((p for p in pc.pins if p != conn_pin), None)
                if other is None:
                    continue
                _orient_to_axis(cell, conn_pin, other, "vertical")
        elif g.archetype == Archetype.CONTROL_RESISTOR:
            # Horizontal control resistors (rotated by _place_and_measure)
            # already have their pins on the side facing the IC. The 180°
            # flip in `_orient_series` is only needed when the IC-side pin
            # ends up on the wrong end — when the resistor's pin 1 is the
            # one that touches the IC, _orient_series flips so it stays
            # toward the IC. Vertical resistors (T/B-side pins) don't need
            # orientation — the placer set their X at the pin's X.
            for r in g.members:
                cell = by_ref.get(r)
                if cell is None or not cell.members:
                    continue
                # Identify which pin connects to the IC (the IC-side pin).
                pc = tree.plan.parts.get(r)
                if pc is None:
                    continue
                ic_pin_num = None
                for pin_num, net_name in pc.pins.items():
                    nc = tree.plan.nets.get(net_name)
                    if (nc is not None
                            and nc.kind not in (NetKind.GROUND, NetKind.RAIL)
                            and any(c.ic_refdes == tree.anchor
                                    for c in nc.ic_contacts)):
                        ic_pin_num = pin_num
                        break
                if ic_pin_num is None:
                    continue
                # On L/R sides the cell is horizontal; flip if the IC-side
                # pin points outward (away from IC). We don't know the IC's
                # side without the pinmap; defer to the existing helper.
                side = next((c.pin.side for n in pc.nets
                             for c in tree.plan.nets[n].ic_contacts
                             if c.ic_refdes == tree.anchor
                             and tree.plan.nets[n].kind
                             not in (NetKind.GROUND, NetKind.RAIL)), None)
                if side in ("L", "R"):
                    _orient_series(cell, ic_pin_num, side)


# --- orchestration -----------------------------------------------------------

def _emit_all(tree: LayoutTree, parts: dict[str, _Part], ctx: _Ctx) -> _CSet:
    """Build the constraint set for the whole tree. The IC is anchored; group
    emitters add intra-group constraints; the orchestrator sequences groups
    by side, threading a left/right cursor between them."""
    cs = _CSet()
    cs.ax(ctx.anchor, ctx.ic_origin[0])
    cs.ay(ctx.anchor, ctx.ic_origin[1])

    rail_banks = [g for g in tree.groups
                  if g.archetype in (Archetype.SERIES_FILTER,
                                     Archetype.RAIL_CAP_BANK)]
    left_banks = [g for g in rail_banks if _side_of(g, tree) == "L"]
    right_banks = [g for g in rail_banks if _side_of(g, tree) == "R"]
    dividers = [g for g in tree.groups if g.archetype == Archetype.DIVIDER_STACK]
    inductors = [g for g in tree.groups
                 if g.archetype == Archetype.POWER_INDUCTOR]
    configs = [g for g in tree.groups if g.archetype == Archetype.CONFIG_CAP]
    ctrl_rs = [g for g in tree.groups
               if g.archetype == Archetype.CONTROL_RESISTOR]
    boots = [g for g in tree.groups if g.archetype == Archetype.BOOTSTRAP]
    shunts = [g for g in tree.groups if g.archetype == Archetype.SHUNT_BRANCH]
    staircases = [g for g in tree.groups
                  if g.archetype == Archetype.SIGNAL_STAIRCASE]
    looses = [g for g in tree.groups if g.archetype == Archetype.LOOSE]

    # Right side, in X-order: divider(s), inductor(s), output bank(s).
    cursor = ctx.anchor
    for g in dividers + inductors + right_banks:
        if g.archetype == Archetype.DIVIDER_STACK:
            members = _emit_divider(g, parts, ctx, cs, cursor)
        elif g.archetype == Archetype.POWER_INDUCTOR:
            members = _emit_inductor(g, parts, ctx, cs, cursor)
        else:
            members = _emit_rail_bank(g, parts, ctx, cs, cursor)
        if members:
            cursor = members[-1]                # rightmost so far
    right_edge = cursor if cursor != ctx.anchor else None

    # Left side: input filter(s).
    cursor = ctx.anchor
    for g in left_banks:
        members = _emit_rail_bank(g, parts, ctx, cs, cursor)
        if members:
            cursor = members[-1]                # leftmost so far
    left_edge = cursor if cursor != ctx.anchor else None

    # Config caps, control resistors, bootstrap, and loose all sit
    # independently of the left/right rail-trunk cursor — each anchors to
    # its own IC control pin. We pre-compute which sides already host a
    # rail filter (input filter on L, output bank on R) so control
    # resistors and bootstrap parts can push past them rather than
    # colliding with rail caps that share the trunk column.
    #
    # Cap-islanded rails (their members live ABOVE the IC, not on the
    # side) are excluded — they no longer reserve a side-band column, so
    # CONTROL_RESISTOR / BOOTSTRAP can pack closer to the IC. Without
    # this, R9 on tps61088 sat 30mm past the IC's right edge because the
    # output bank "still" claimed a column it had vacated.
    has_filter_on = {
        _side_of(g, tree) for g in rail_banks
        if not (g.rail and g.rail in ctx.label_only_nets)
    }
    config_cols: dict[str, int] = {}        # side → columns used by configs
    for g in configs:
        n = _emit_config(g, parts, ctx, cs)
        side = _side_of(g, tree)
        config_cols[side] = max(config_cols.get(side, 0), n)
    # Count BOOTSTRAP parts per side so CONTROL_RESISTOR can offset past
    # them. Without this, R3 (CONTROL_RESISTOR on FSW) and C8 (BOOTSTRAP
    # BOOT↔SW) both compute the same side-column offset and land at the
    # same X — bodies overlap because their IC pins (FSW / BOOT-midpoint)
    # are only one pin pitch apart in Y.
    bootstrap_cols: dict[str, int] = {}
    for g in boots:
        side = _side_of(g, tree)
        bootstrap_cols[side] = bootstrap_cols.get(side, 0) + len(g.members)
    # The outermost side-band occupant per side, for extent-aware clearing.
    # A fixed COL_GAP step can't know how far a multi-cap bank actually
    # reaches, so a control resistor offset by one column still lands inside
    # the bank's body (AP2112K: EN pull-up vs the input cap at the same Y).
    # Cap-island mode vacates the side bands, so the edges don't apply there.
    side_edge: dict[str, str] = {}
    if not any(g.archetype == Archetype.SIGNAL_STAIRCASE for g in tree.groups):
        if right_edge is not None and "R" in has_filter_on:
            side_edge["R"] = right_edge
        if left_edge is not None and "L" in has_filter_on:
            side_edge["L"] = left_edge
    for g in ctrl_rs:
        _emit_control_resistor(g, parts, ctx, cs,
                               config_cols, has_filter_on, bootstrap_cols,
                               side_edge)
    for g in boots:
        _emit_bootstrap(g, parts, ctx, cs, has_filter_on)
    for g in shunts:
        _emit_shunt_branch(g, parts, ctx, cs, config_cols, has_filter_on)
    for g in staircases:
        _emit_signal_staircase(g, parts, ctx, cs)
    for g in looses:
        _emit_loose(g, parts, ctx, cs)

    return cs


def _apply_extras(cs: _CSet, extras_x: list, extras_y: list) -> None:
    """Merge critic-supplied constraints into the archetype-emitted set.

    Behaviour by kind:
      - `Anchor(var, value)` — REPLACES any existing anchor on `var` (same
        axis). The critic's anchor wins; the solver would otherwise raise
        on conflicting anchors. Use this to override an archetype placement.
      - `Offset(a, b, delta)` / `MinGap(a, b, gap)` — appended additively.
        These tighten the constraint system without nullifying anything.

    Per-axis. The caller has already split `extras_x` / `extras_y`.
    """
    for axis_name, extras, current in (("x", extras_x, cs.x),
                                       ("y", extras_y, cs.y)):
        for ec in extras:
            if isinstance(ec, Anchor):
                # Drop any prior anchor on this var so the critic's wins.
                for i in range(len(current) - 1, -1, -1):
                    c = current[i]
                    if isinstance(c, Anchor) and c.var == ec.var:
                        del current[i]
                current.append(ec)
            else:
                current.append(ec)


def _horiz_refs(tree: LayoutTree) -> frozenset[str]:
    """Refdeses to rotate to horizontal in `_place_and_measure`: rail series
    elements (on the rail trunk) and control resistors whose IC pin lives on a
    vertical (L/R) edge of the IC body — those want to extend left/right of the
    IC so the wire from the pin meets a horizontal pin of the R.

    Shared with `emit.placers.fdplace`, which reuses `_orient_all` and so needs
    the identical pre-rotation (`_orient_series` only 180°-flips an already-
    horizontal part)."""
    plan = tree.plan

    def _ctrl_resistor_is_horizontal(refdes: str) -> bool:
        pc = plan.parts.get(refdes)
        if pc is None or tree.anchor is None:
            return False
        for net_name in pc.nets:
            nc = plan.nets.get(net_name)
            if nc is None or nc.kind in (NetKind.GROUND, NetKind.RAIL):
                continue
            for c in nc.ic_contacts:
                if c.ic_refdes == tree.anchor and c.pin.side in ("L", "R"):
                    return True
        return False

    # Exclude cap-islanded series elements: they go vertical at the page
    # edge (see `_emit_rail_bank_islanded`), not horizontal on the trunk.
    cap_island = any(
        g.archetype == Archetype.SIGNAL_STAIRCASE for g in tree.groups
    )
    horiz_series = {
        r for g in tree.groups if g.archetype == Archetype.SERIES_FILTER
        for r in g.members if plan.parts[r].role == Role.SERIES_ELEMENT
        and not cap_island
    }
    horiz_ctrl = {
        r for g in tree.groups if g.archetype == Archetype.CONTROL_RESISTOR
        for r in g.members if _ctrl_resistor_is_horizontal(r)
    }
    return frozenset(horiz_series | horiz_ctrl)


def _build_once(netlist: Netlist, tree: LayoutTree, title: str,
                wiring: str, extras_x: list | None = None,
                extras_y: list | None = None,
                external_emitter=None) -> PlacerResult:
    sch = ksa.create_schematic(title)
    issues: list[str] = []
    plan = tree.plan
    anchor = tree.anchor

    horiz = _horiz_refs(tree)
    ordered = sorted(netlist.parts, key=lambda p: _natural_key(p.refdes))
    cells = _place_and_measure(sch, ordered, issues, rotate_horizontal=horiz)
    by_ref = {c.refdes: c for c in cells}
    if anchor not in by_ref:
        raise PlacerError([f"IC {anchor} failed to place"])

    placed_refs: dict[str, tuple[float, float]] = {}
    _move_to(by_ref[anchor], IC_X, IC_Y, placed_refs)
    ic_comp = sch.components.get(anchor)

    _orient_all(tree, by_ref, ic_comp)

    # Measure every cell *after* orientation so pin offsets are final.
    parts: dict[str, _Part] = {}
    for ref, cell in by_ref.items():
        mp = _measure_part(cell)
        if mp is not None:
            parts[ref] = mp

    ic_part = parts.get(anchor)
    if ic_part is None:
        raise PlacerError([f"could not measure IC {anchor}"])
    ctx = _Ctx(
        tree=tree, netlist=netlist, ic_comp=ic_comp, anchor=anchor,
        ic_origin=ic_part.origin,
        ic_leftext=ic_part.leftext, ic_rightext=ic_part.rightext,
    )

    # Constraint emission: either run the archetype emitters (default) or
    # delegate to an external emitter (the LLM-direct-placer experiment).
    # Either path can be augmented with critic-supplied `extras_*`.
    if external_emitter is not None:
        cs = external_emitter(tree, parts, ctx)
    else:
        cs = _emit_all(tree, parts, ctx)
    if extras_x or extras_y:
        _apply_extras(cs, list(extras_x or []), list(extras_y or []))
    xr = solve(cs.x, fallback=IC_X)
    yr = solve(cs.y, fallback=IC_Y)
    if xr.issues:
        issues.extend(f"solve(x): {m}" for m in xr.issues)
    if yr.issues:
        issues.extend(f"solve(y): {m}" for m in yr.issues)

    # Translate each support cell so its origin lands on the solved position.
    # The IC is already at its anchored spot; translating by zero is a no-op.
    #
    # Anti-stack guard: two cells must never land on the same solved spot —
    # same-symbol parts at one origin have coincident pins, which silently
    # merges their nets at wiring time (the topology validator then rejects
    # the whole placement, and the agent retries the identical input into a
    # dead end). Identical targets mean an upstream emitter anchored two
    # parts to the same reference; stagger duplicates deterministically so
    # connectivity survives even when the layout is imperfect.
    taken: set[tuple[float, float]] = set()
    if anchor in parts:
        a = parts[anchor]
        taken.add((round(a.origin[0], 2), round(a.origin[1], 2)))
    for ref in sorted(parts, key=_natural_key):
        p = parts[ref]
        if ref == anchor:
            placed_refs.setdefault(ref, (_snap(p.origin[0] - p.leftext),
                                          _snap(p.origin[1] - p.topext)))
            continue
        target_x = xr.pos.get(ref, IC_X)
        target_y = yr.pos.get(ref, IC_Y)
        nudged = False
        while (round(target_x, 2), round(target_y, 2)) in taken:
            target_y += COL_GAP
            nudged = True
        if nudged:
            issues.append(
                f"anti-stack: {ref} shared a solved position with another "
                f"part — staggered down to y={target_y:.2f}"
            )
        taken.add((round(target_x, 2), round(target_y, 2)))
        dx = target_x - p.origin[0]
        dy = target_y - p.origin[1]
        _translate(p.cell, dx, dy, placed_refs)

    label_specs = _place_connectivity(sch, netlist, plan, placed_refs, issues,
                                      wiring=wiring,
                                      rail_y_hints=ctx.rail_y_hints or None,
                                      label_only_nets=ctx.label_only_nets or None)
    _place_no_connects(sch, netlist, placed_refs, issues)

    text = _hide_gnd_labels(sch_to_string(sch), netlist)
    text = _reposition_fields(text, sch, anchor, above=True)
    for refdes, pc in plan.parts.items():
        if pc.role == Role.SERIES_ELEMENT:
            text = _reposition_fields(text, sch, refdes, above=False)

    return PlacerResult(sch_text=text, issues=issues,
                        placed_refs=placed_refs, label_specs=label_specs)


def cplace(netlist: Netlist, *, title: str = "Subcircuit",
           tree: LayoutTree | None = None,
           extras_x: list | None = None,
           extras_y: list | None = None,
           external_emitter=None) -> PlacerResult:
    """Constraint-based placer entry point. Single-IC scope; a zero / multi-IC
    netlist defers to the legacy column placer. Wires with the crossing-
    minimising router, falling back to label-only wiring if the router
    corrupts topology (same guard as the legacy `place()`).

    `tree=None` (default): runs `build_layout_tree` deterministically.
    `tree=<LayoutTree>`: skips the builder — used by the LLM-planner
    experiment to inject a tree from an alternate source. The tree must
    cover the same netlist's parts.

    `extras_x` / `extras_y`: additional constraints (Anchor / Offset /
    MinGap) injected after the archetype emitters. A critic-supplied
    Anchor replaces any prior anchor on the same variable; Offsets and
    MinGaps append additively. Used by the VLM critic experiment to nudge
    specific parts based on visual feedback from the rendered output.
    """
    errors = netlist.validate_self()
    if errors:
        raise PlacerError(errors)
    if tree is None:
        tree = build_layout_tree(netlist)
    if tree.anchor is None:
        return place(netlist, title=title)

    routed = _build_once(netlist, tree, title, "router",
                         extras_x=extras_x, extras_y=extras_y,
                         external_emitter=external_emitter)
    if _topology_intact(netlist, routed):
        return routed
    fallback = _build_once(netlist, tree, title, "labels",
                           extras_x=extras_x, extras_y=extras_y,
                           external_emitter=external_emitter)
    fallback.issues.append(
        "router output failed the connectivity check — fell back to "
        "label-only wiring"
    )
    return fallback
