"""Shared placer helpers — layout primitives reused across engines.

Originally lived in `emit.treeplace`; lifted here when treeplace was retired
so `emit.placers.cplace` (and any future engine) can use the same orientation
/ rail-trunk / divider / field-reposition primitives without depending on a
specific archetype dispatcher.

These helpers are pure geometry — no schematic mutation other than
`comp.rotate()` for orientation and the text rewrite in `_reposition_fields`.
They sit one layer above `netlist_to_sch`'s low-level pin-extent / snap /
pin-xy machinery and one layer below the engine's archetype emitters.
"""

from __future__ import annotations

from pinflow_api.emit.classify import NetKind, Role
from pinflow_api.emit.layout_tree import GroupNode, LayoutTree
from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.netlist_to_sch import (
    _Cell,
    _horizontal_field_angle,
    _natural_key,
    _part_pin_extent,
    _pin_xy,
    _rewrite_property_at,
    _snap,
)


# --- layout constants (mm) ---------------------------------------------------
IC_X = 130.0          # IC body anchor — leaves a left band for the input
IC_Y = 95.0           # filter and a wide right band for output + divider
TRUNK_GAP = 12.70     # gap from the IC pin column to the first bank part
CAP_GAP = 6.35        # extra width added to the widest cell for slot pitch
COL_GAP = 10.16       # gap between adjacent side columns (divider, inductor)
STACK_GAP = 5.08      # vertical gap inside a divider / config stack


# --- cell translation helpers ------------------------------------------------

def _translate(cell: _Cell, dx: float, dy: float,
               placed_refs: dict[str, tuple[float, float]]) -> None:
    """Move a cell by (dx, dy), keeping its bbox + placed_refs current."""
    for comp in cell.members:
        comp.translate(dx, dy)
    cell.min_x += dx
    cell.min_y += dy
    placed_refs[cell.refdes] = (_snap(cell.min_x), _snap(cell.min_y))


def _move_to(cell: _Cell, x: float, y: float,
             placed_refs: dict[str, tuple[float, float]]) -> None:
    """Move a cell so its top-left bbox corner lands at (x, y)."""
    _translate(cell, _snap(x - cell.min_x), _snap(y - cell.min_y), placed_refs)


def _pin(cell: _Cell, num: str) -> tuple[float, float] | None:
    return _pin_xy(cell.members[0], num) if cell.members else None


def _place_pin_at(cell: _Cell, num: str, tx: float, ty: float,
                  placed_refs: dict[str, tuple[float, float]]) -> None:
    """Move a cell so its pin `num` lands *exactly* at (tx, ty).

    The delta is applied unsnapped: callers pass grid-snapped targets, so the
    pin lands on-grid and every cap placed against one trunk Y ends up
    perfectly aligned. Snapping the delta instead drifts otherwise-identical
    caps by up to a grid step — a real `alignment` regression."""
    p = _pin(cell, num)
    if p is None:
        _move_to(cell, tx, ty, placed_refs)
        return
    _translate(cell, tx - p[0], ty - p[1], placed_refs)


# --- netlist queries ---------------------------------------------------------

def _ic_pins_on_net(net_name: str, anchor: str, netlist: Netlist) -> list[str]:
    for net in netlist.nets:
        if net.name == net_name:
            return [ep.pin for ep in net.endpoints if ep.ref == anchor]
    return []


def _group_ic_pins(g: GroupNode, tree: LayoutTree) -> list[str]:
    """IC pin numbers the group's non-ground nets land on, de-duplicated."""
    out: list[str] = []
    for r in g.members:
        pc = tree.plan.parts.get(r)
        if pc is None:
            continue
        for net_name in pc.nets:
            nc = tree.plan.nets.get(net_name)
            if nc is None or nc.kind == NetKind.GROUND:
                continue
            for p in _ic_pins_on_net(net_name, tree.anchor, netlist=tree.netlist):
                if p not in out:
                    out.append(p)
    return out


def _part_pins_by_net(refdes: str, netlist: Netlist
                      ) -> dict[str, str]:
    """net_name → pin number, for one part."""
    out: dict[str, str] = {}
    for net in netlist.nets:
        for ep in net.endpoints:
            if ep.ref == refdes:
                out[net.name] = ep.pin
    return out


def _cap_pins(refdes: str, tree: LayoutTree) -> tuple[str | None, str | None]:
    """(rail_pin, ground_pin) for a 2-pin part — the pin on a non-ground net
    and the pin on ground. Either may be None for a part bridging two
    non-ground nets."""
    rail = gnd = None
    for net_name, pin in _part_pins_by_net(refdes, tree.netlist).items():
        nc = tree.plan.nets.get(net_name)
        if nc is not None and nc.kind == NetKind.GROUND:
            gnd = pin
        else:
            rail = pin
    return rail, gnd


def _ic_side_pin(refdes: str, tree: LayoutTree) -> str | None:
    """The pin of a series element that sits on a non-ground net reaching the
    IC — the pin that must face the IC so the rail nets it splits don't run
    collinearly into a short."""
    for net_name, pin in _part_pins_by_net(refdes, tree.netlist).items():
        nc = tree.plan.nets.get(net_name)
        if (nc is not None and nc.kind != NetKind.GROUND
                and _ic_pins_on_net(net_name, tree.anchor, tree.netlist)):
            return pin
    return None


# --- IC geometry -------------------------------------------------------------

def _ic_pin_pts(ic_comp, pins: list[str]) -> list[tuple[float, float]]:
    return [p for p in (_pin_xy(ic_comp, n) for n in pins) if p is not None]


def _trunk_y(ic_comp, pins: list[str], fallback: float) -> float:
    pts = _ic_pin_pts(ic_comp, pins)
    return _snap(sum(p[1] for p in pts) / len(pts)) if pts else fallback


def _side_edge_x(ic_comp, side: str) -> float:
    """X of the IC's outermost pin column on `side` (R → rightmost pin)."""
    xs = [p[0] for p in (_pin_xy(ic_comp, str(pp.number)) for pp in ic_comp.pins)
          if p is not None]
    if not xs:
        return float(ic_comp.position.x)
    return max(xs) if side in ("R", "T") else min(xs)


# --- orientation -------------------------------------------------------------

def _orient_vertical(cell: _Cell, up_pin: str, down_pin: str) -> None:
    """Rotate a vertical 2-pin part 180° so `up_pin` is the upper pin — a cap's
    rail pin above its ground pin, a divider leg's source above its tap. No-op
    if already so, or if a pin can't be resolved."""
    up, down = _pin(cell, up_pin), _pin(cell, down_pin)
    if up is None or down is None or len(cell.members) != 1:
        return
    if up[1] > down[1]:                     # up pin is below down pin
        cell.members[0].rotate(180)


def _orient_to_axis(cell: _Cell, up_pin: str, down_pin: str,
                    axis: str = "vertical") -> None:
    """Rotate a 2-pin part so its `up_pin`/`down_pin` lie along `axis`, with
    `up_pin` on the leading side (smaller Y for vertical, smaller X for
    horizontal).

    Generalises `_orient_vertical`: handles parts whose library default puts
    the pins on the *other* axis (e.g. `Device:LED`, horizontal at rot=0)
    by first rotating 90° to swap axes, then applying the 180° flip if the
    leading pin is on the wrong side.
    """
    up, down = _pin(cell, up_pin), _pin(cell, down_pin)
    if up is None or down is None or len(cell.members) != 1:
        return
    member = cell.members[0]
    want_vertical = axis == "vertical"
    pins_are_vertical = abs(up[1] - down[1]) > abs(up[0] - down[0])
    if want_vertical != pins_are_vertical:
        member.rotate(90)
        up, down = _pin(cell, up_pin), _pin(cell, down_pin)
        if up is None or down is None:
            return
    if want_vertical:
        if up[1] > down[1]:
            member.rotate(180)
    else:
        if up[0] > down[0]:
            member.rotate(180)


def _orient_divider(div, tree: LayoutTree, by_ref: dict[str, _Cell]) -> None:
    """Orient both divider legs so the stack reads rail → tap → ground top to
    bottom: the high leg's sensed-rail pin up, the low leg's tap pin up.
    Mis-oriented legs force the router to detour and cross."""
    for refdes, up_net in ((div.high_refdes, div.sensed_net),
                           (div.low_refdes, div.tap_net)):
        cell = by_ref.get(refdes)
        if cell is None:
            continue
        pins = _part_pins_by_net(refdes, tree.netlist)
        up_pin = pins.get(up_net)
        down_pin = next((p for n, p in pins.items() if n != up_net), None)
        if up_pin and down_pin:
            _orient_vertical(cell, up_pin, down_pin)


def _orient_series(cell: _Cell, ic_side_pin: str | None, side: str) -> None:
    """Rotate a horizontal series element 180° if its IC-side-net pin faces
    away from the IC. The bank extends *away* from the IC, so the IC-side pin
    must sit on the IC-facing end — otherwise the two rail nets the element
    splits run collinearly and the router shorts them."""
    if ic_side_pin is None or len(cell.members) != 1:
        return
    other = "2" if str(ic_side_pin) == "1" else "1"
    p_ic, p_other = _pin(cell, ic_side_pin), _pin(cell, other)
    if p_ic is None or p_other is None:
        return
    ic_is_right = side == "L"               # bank extends left → IC on the right
    if (p_ic[0] > p_other[0]) != ic_is_right:
        cell.members[0].rotate(180)


# --- rail ordering -----------------------------------------------------------

def _order_rail_members(g: GroupNode, tree: LayoutTree) -> list[str]:
    """Order a rail group IC-ward → outward: caps on an IC-touching net first,
    then series elements, then caps on the far net."""
    ic_nets = {
        net.name for net in tree.netlist.nets
        if any(ep.ref == tree.anchor for ep in net.endpoints)
    }

    def rank(r: str) -> tuple[int, tuple]:
        pc = tree.plan.parts[r]
        if pc.role == Role.SERIES_ELEMENT:
            tier = 1
        else:
            nongnd = [n for n in pc.nets
                      if (nc := tree.plan.nets.get(n)) and nc.kind != NetKind.GROUND]
            tier = 0 if any(n in ic_nets for n in nongnd) else 2
        return (tier, _natural_key(r))

    return sorted(g.members, key=rank)


# --- field repositioning -----------------------------------------------------

def _reposition_fields(text: str, sch, refdes: str, *, above: bool) -> str:
    """Stack a component's Reference and Value into the clear band above (an
    IC) or below (a series element) its body — many symbols, easyeda imports
    especially, park both fields at the body centre, coincident.

    Done as a text rewrite (kicad-sch-api cannot move a field) at *exact*
    offsets — deliberately not snapped. The legacy `_reposition_ic_fields`
    snaps the field Y to the 2.54 grid; when the body sits on a 1.27-but-not-
    2.54 line the two offsets (5.08 and 2.54) round-half-to-even onto the same
    grid point and the fields collide. Field text position is cosmetic — KiCad
    does not grid it — so an exact offset is both correct and collision-free."""
    comp = sch.components.get(refdes)
    ext = _part_pin_extent(comp) if comp is not None else None
    if ext is None:
        return text
    min_x, max_x, min_y, max_y = ext
    cx = _snap((min_x + max_x) / 2.0)
    angle = _horizontal_field_angle(getattr(comp, "rotation", 0) or 0)
    ref_y, val_y = ((min_y - 5.08, min_y - 2.54) if above
                    else (max_y + 2.54, max_y + 5.08))
    text, ri = _rewrite_property_at(
        text, f'(property "Reference" "{refdes}"', 0, cx, ref_y, angle)
    if ri != -1:
        text, _ = _rewrite_property_at(
            text, '(property "Value"', ri, cx, val_y, angle)
    return text
