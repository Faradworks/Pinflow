"""Deterministic netlist→schematic placer.

Pass 1 of the netlist-first generate / replicate pipeline. Pure function: same
netlist always produces the same `.kicad_sch`. No LLM, no network.

Layout dispatches on IC count (`classify` decides the rest):

  - **Single IC** → the *pin-anchored grammar* (`_place_pin_anchored`): the IC
    is the anchor; every support part is placed to the body side its function
    belongs — input caps / input filters toward the VIN pins, output caps and
    the feedback divider toward the VOUT / FB pins — each side a measured,
    non-overlapping column. This recovers the left→right signal-flow layout a
    human would draw, because the IC symbol's pinout already encodes it.
  - **Zero or several ICs** → the measured-column fallback (`_place_columns`):
    IC blocks laid left→right, cohorts in columns.

Connections are currently made via labels at pin positions (`_place_labels`)
— KiCad's connection engine ties same-named labels together at netlist time.
The wiring pass (real wires + power/ground symbols) is the next phase; the
deterministic placer can route safely because every endpoint is anchored to a
real pin by construction.
"""

from __future__ import annotations

import functools
import math
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field

import kicad_sch_api as ksa
from kicad_sch_api.core.component_bounds import get_component_bounding_box

from pinflow_api.builders._common import sch_to_string
from pinflow_api.emit import bbox
from pinflow_api.emit.route import (
    _interior,
    count_crossings,
    count_overlaps,
    route_nets,
)
from pinflow_api.emit.classify import LayoutPlan, NetKind, Role, classify
from pinflow_api.emit.netlist import Netlist, NetlistNet, NetlistPart

# Page geometry — A4 landscape. Mirrors emit.layout constants.
PAGE_W = 297.0
PAGE_H = 210.0
MARGIN = 10.0
GRID = 2.54

# Block layout. Each IC becomes a "block": its other-passive column and its
# decoupling-cap column to the left, the IC to the right. All part positions
# are derived from *measured* symbol bboxes (emit/bbox.py) at layout time —
# these constants are only the gaps left between measured cells. Net-label
# text length is not measured, so the column / block gaps are deliberately
# generous to leave the labels somewhere to go.
STACK_GAP = 5.08    # vertical gap between stacked parts within a column
COL_GAP = 7.62      # horizontal gap between the sub-columns of one IC block
BLOCK_GAP = 12.7    # horizontal gap between adjacent IC blocks
ROW_GAP = 12.7      # vertical gap between wrapped rows of IC blocks
BLOCK_Y_TOP = MARGIN + 20.0   # top edge of the first row of blocks
RAIL_FLAG_GAP = 15.24   # headroom above a rail-cap row for its power flag
GND_STUB = 2.54         # visible drop wire from a pin to its GND symbol

# Right margin: power symbols stack here, one per rail.
POWER_X = PAGE_W - MARGIN - 5.0
POWER_Y_START = MARGIN + 10.0
POWER_Y_PITCH = 10.16

# Parts-bin packing geometry (used by place_parts).
BIN_PAD = 2.54           # gutter between adjacent parts (1 grid step)
BIN_ROW_PAD = 2.54       # extra vertical gutter between rows
BIN_UNIT_SPACING = 25.4  # tight multi-unit fan-out (no wiring room needed)


# Standard KiCad power-library symbols. Non-standard rail names get labels
# only — the refiner pass can later swap in `power:VCC` with a value override.
_POWER_LIB_IDS: dict[str, str] = {
    "GND": "power:GND",
    "AGND": "power:GNDA",
    "DGND": "power:GNDD",
    "PGND": "power:GNDPWR",
    "+3V3": "power:+3V3",
    "+3.3V": "power:+3V3",
    "+5V": "power:+5V",
    "+12V": "power:+12V",
    "+1V8": "power:+1V8",
    "VCC": "power:VCC",
    "VDD": "power:VDD",
}


class PlacerError(RuntimeError):
    """Fatal placement failure carrying a structured error list."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class LabelSpec:
    """A label the placer dropped at a specific pin. Used by the validator to
    cross-check that file round-trip preserved every connection."""

    ref: str
    pin: str
    net_name: str
    position: tuple[float, float]


@dataclass
class PlacerResult:
    sch_text: str
    issues: list[str] = field(default_factory=list)
    placed_refs: dict[str, tuple[float, float]] = field(default_factory=dict)
    label_specs: list[LabelSpec] = field(default_factory=list)


@dataclass
class _Cell:
    """A placed part with its measured, field-inclusive bounding box.

    `members` is the list of ksa Component objects sharing the refdes — >1 for
    a multi-unit symbol. `(min_x, min_y)` is the box's top-left corner in the
    *live* schematic's coordinate frame; `(w, h)` its extent. The layout pass
    translates `members` to move the cell into its final position.
    """

    refdes: str
    members: list
    w: float
    h: float
    min_x: float
    min_y: float


def _snap(v: float) -> float:
    return round(v / GRID) * GRID


def _ref_prefix(ref: str) -> str:
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref


@functools.lru_cache(maxsize=256)
def _real_unit_count(lib_id: str) -> int:
    """Genuine section count for `lib_id`; 1 if single-unit / unknown.

    Not `SymbolDefinition.units`: kicad-sch-api counts the `_0_` common-
    graphics pseudo-unit as a unit, so it reports 2 for single-section
    parts (Device:R, a one-section regulator like TPS628436DRL). Passing
    `add_all_units=True` on those makes kicad-sch-api emit a phantom
    `(unit 2)` instance. We count only the distinct *non-zero* unit
    indices in the sub-symbol names (`<name>_<unit>_<style>`) — the real
    section count (TL072 → 3, TL074 → 5, single-unit → 1).
    """
    try:
        data = ksa.get_symbol_info(lib_id).raw_kicad_data
    except Exception:
        return 1
    units: set[int] = set()
    for item in data[1:] if isinstance(data, list) else []:
        if (isinstance(item, list) and len(item) >= 2
                and str(item[0]) == "symbol"):
            parts = str(item[1]).strip('"').split("_")
            if len(parts) >= 2:
                try:
                    u = int(parts[-2])
                except ValueError:
                    continue
                if u:
                    units.add(u)
    return max(1, len(units))


def _natural_key(ref: str) -> tuple[str, int, str]:
    """Sort 'U2' before 'U10': (prefix, numeric tail, raw) — stable & total."""
    prefix = _ref_prefix(ref)
    tail = ref[len(prefix):]
    return (prefix, int(tail) if tail.isdigit() else 0, ref)


def _power_lib_id(net_name: str) -> str | None:
    return _POWER_LIB_IDS.get(net_name)


def _is_decoupling_cap(
    part: NetlistPart,
    part_nets: set[str],
    nets_by_name: dict[str, NetlistNet],
) -> bool:
    """A cap with both pins on power/ground nets."""
    if not part.lib_id.startswith("Device:C"):
        return False
    if len(part_nets) < 2:
        return False
    for net_name in part_nets:
        net = nets_by_name.get(net_name)
        if net is None or not net.is_power:
            return False
    return True


def _bucket_parts(
    netlist: Netlist,
) -> tuple[list[NetlistPart], list[NetlistPart], dict[str, list[NetlistPart]]]:
    """Sort parts into (ICs, connectors, other_by_host_ic).

    `other_by_host[ic_refdes]` lists non-IC, non-connector parts that share
    the most nets with that IC. Parts with no IC host (or no ICs in the
    netlist) land under the special key `"__shared__"`.
    """
    nets_by_name = {n.name: n for n in netlist.nets}
    nets_by_part: dict[str, set[str]] = {p.refdes: set() for p in netlist.parts}
    for net in netlist.nets:
        for ep in net.endpoints:
            if ep.ref in nets_by_part:
                nets_by_part[ep.ref].add(net.name)

    ics = [p for p in netlist.parts if _ref_prefix(p.refdes) == "U"]
    connectors = [p for p in netlist.parts if _ref_prefix(p.refdes) == "J"]
    ic_refs = {p.refdes for p in ics}
    conn_refs = {p.refdes for p in connectors}

    other_by_host: dict[str, list[NetlistPart]] = {ic.refdes: [] for ic in ics}
    shared: list[NetlistPart] = []

    for p in netlist.parts:
        if p.refdes in ic_refs or p.refdes in conn_refs:
            continue
        # Score each IC by net-sharing count; ties broken by refdes order.
        scores: dict[str, int] = {}
        for net_name in nets_by_part[p.refdes]:
            net = nets_by_name.get(net_name)
            if net is None:
                continue
            for ep in net.endpoints:
                if ep.ref in ic_refs and ep.ref != p.refdes:
                    scores[ep.ref] = scores.get(ep.ref, 0) + 1
        if scores:
            host = max(scores.items(), key=lambda kv: (kv[1], -ord(kv[0][0]), kv[0]))[0]
            other_by_host[host].append(p)
        else:
            shared.append(p)

    if shared:
        other_by_host["__shared__"] = shared
    return ics, connectors, other_by_host


def _split_decouplers(
    parts: list[NetlistPart],
    nets_by_part: dict[str, set[str]],
    nets_by_name: dict[str, NetlistNet],
) -> tuple[list[NetlistPart], list[NetlistPart]]:
    decouplers: list[NetlistPart] = []
    others: list[NetlistPart] = []
    for p in sorted(parts, key=lambda x: (_ref_prefix(x.refdes), x.refdes)):
        if _is_decoupling_cap(p, nets_by_part.get(p.refdes, set()), nets_by_name):
            decouplers.append(p)
        else:
            others.append(p)
    return decouplers, others


def _comps_for_ref(sch: ksa.Schematic, ref: str) -> list:
    """All Component objects sharing `ref` — >1 for a multi-unit symbol."""
    return [c for c in sch.components if c.reference == ref]


def _make_horizontal(members: list) -> None:
    """Rotate a 2-pin part 90° if its pins sit vertically, so a series
    element (inductor, ferrite, series R) reads as *inline* in the signal
    path rather than upright. No-op if the part is already horizontal or
    isn't a plain single-unit 2-pin part. Rotation is about the component
    origin, so the parked position is unchanged — the measure pass that
    follows captures the rotated extent.
    """
    if len(members) != 1:
        return
    pins = list(members[0].pins)
    if len(pins) != 2:
        return
    x0, y0 = _xy(pins[0].position)
    x1, y1 = _xy(pins[1].position)
    if abs(y1 - y0) > abs(x1 - x0):
        members[0].rotate(90)


def _add_component(
    sch: ksa.Schematic,
    part: NetlistPart,
    position: tuple[float, float],
    multi_unit: bool,
    issues: list[str],
    unit_spacing: float = 80.0,
) -> bool:
    """Try to add `part` to `sch` at `position`. Returns True on success.

    `unit_spacing` only matters for multi-unit symbols — it sets how far the
    sections fan out. The placer measures whatever extent results, so the
    value only affects compactness, not correctness.
    """
    kwargs: dict = dict(
        lib_id=part.lib_id,
        reference=part.refdes,
        value=part.value or part.refdes,
        position=position,
    )
    if part.footprint:
        kwargs["footprint"] = part.footprint
    if multi_unit:
        kwargs["add_all_units"] = True
        kwargs["unit_spacing"] = unit_spacing
    try:
        sch.components.add(**kwargs)
    except Exception as e:
        issues.append(f"add({part.refdes}, {part.lib_id}) failed: {e}")
        return False
    return True


def _place_and_measure(
    sch: ksa.Schematic,
    ordered: list[NetlistPart],
    issues: list[str],
    *,
    unit_spacing: float = BIN_UNIT_SPACING,
    rotate_horizontal: frozenset[str] = frozenset(),
    flip_180: frozenset[str] = frozenset(),
) -> list[_Cell]:
    """Place every part provisionally, then measure field-inclusive bboxes.

    Shared pass-1 of the placers. Returns one `_Cell` per successfully placed
    part, in the order given.

    Two-pass because Reference/Value fields only materialize on *serialize*:
    a bbox measured off freshly-added ksa components sees symbol geometry
    only and sizes too tight — the field labels then overhang the gap and
    neighbouring cells collide. We place all parts in a non-overlapping
    parking column, serialize + reload once, and re-measure the reloaded
    copies. Reload preserves coordinates, so the field-inclusive boxes are
    still valid in the live components' frame and the caller's translate math
    stays correct.

    ICs that fail to add raise `PlacerError` (a netlist↔lib_id mismatch the
    caller must fix). Non-IC add failures are recorded in `issues` and the
    part skipped, so a partial result is still inspectable.
    """
    cells: list[_Cell] = []
    park_y = MARGIN  # provisional parking row; real positions set by caller
    for part in ordered:
        is_ic = _ref_prefix(part.refdes) == "U"
        multi_unit = is_ic and _real_unit_count(part.lib_id) > 1
        if not _add_component(
            sch, part, (MARGIN, park_y), multi_unit=multi_unit,
            issues=issues, unit_spacing=unit_spacing,
        ):
            if is_ic:
                raise PlacerError(issues[-1:])
            continue

        members = _comps_for_ref(sch, part.refdes)
        if part.refdes in rotate_horizontal:
            _make_horizontal(members)
        if part.refdes in flip_180 and len(members) == 1:
            members[0].rotate(180)
        box = bbox.union_bbox(members)
        if box is not None:
            min_x, min_y, max_x, max_y = box
            w, h = max_x - min_x, max_y - min_y
        else:
            # Measurement unavailable — fall back to a conservative estimate,
            # treating the component anchor as its centre.
            w, h = bbox.estimate_extent(part.refdes, part.lib_id)
            anchor = members[0].position if members else None
            ax = anchor.x if anchor is not None else MARGIN
            ay = anchor.y if anchor is not None else park_y
            min_x, min_y = ax - w / 2, ay - h / 2
            issues.append(
                f"{part.refdes}: bbox unmeasurable, used estimate {w:.0f}x{h:.0f}mm"
            )
        cells.append(_Cell(part.refdes, members, w, h, min_x, min_y))
        park_y += h + BIN_ROW_PAD  # keep provisional copies from overlapping

    if not cells:
        return cells

    # Re-measure each cell from a reloaded copy so Reference/Value fields are
    # included (see docstring). Reload preserves coordinates, so the boxes
    # land back in the live components' frame.
    # Series elements get their Reference/Value stacked below the body by
    # `_reposition_series_fields` in the final pass. Apply that same move to
    # the provisional layout here, before measuring, so `cell.h` accounts for
    # the stacked fields — otherwise a series part's value text overhangs the
    # measured cell and collides with the neighbour below it in a column.
    _meas_text = sch_to_string(sch)
    if rotate_horizontal:
        _meas_text = _reposition_series_fields(
            _meas_text, sch, rotate_horizontal
        )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as _f:
        _f.write(_meas_text)
        _meas_path = _f.name
    try:
        _meas = ksa.load_schematic(_meas_path)
        _by_ref: dict[str, list] = {}
        for _c in _meas.components:
            _by_ref.setdefault(_c.reference, []).append(_c)
        for cell in cells:
            box = bbox.union_bbox(_by_ref.get(cell.refdes, []))
            if box is not None:
                cell.min_x, cell.min_y, _mx, _my = box
                cell.w = _mx - cell.min_x
                cell.h = _my - cell.min_y
    finally:
        os.unlink(_meas_path)
    return cells


def _layout_column(
    cells: list[_Cell],
    x_left: float,
    y_top: float,
    col_w: float,
    placed_refs: dict[str, tuple[float, float]],
) -> float:
    """Stack `cells` vertically into a column of width `col_w`.

    The column's left edge is `x_left`; stacking starts at `y_top`. Each cell
    is centred horizontally in the column and its `members` translated into
    place; `placed_refs` is filled with the final snapped top-left corner.
    Returns the y of the column's bottom edge (for row-wrap bookkeeping).
    """
    y = y_top
    for cell in cells:
        target_x = x_left + (col_w - cell.w) / 2.0
        dx = _snap(target_x - cell.min_x)
        dy = _snap(y - cell.min_y)
        for comp in cell.members:
            comp.translate(dx, dy)
        placed_refs[cell.refdes] = (
            _snap(cell.min_x + dx),
            _snap(cell.min_y + dy),
        )
        y += cell.h + STACK_GAP
    return y


def _layout_row(
    cells: list[_Cell],
    x_anchor: float,
    trunk_y: float,
    placed_refs: dict[str, tuple[float, float]],
    *,
    leftward: bool = False,
) -> float:
    """Place `cells` in a horizontal row, each shifted so its pin "1" lands
    on the rail trunk at `trunk_y` — caps then hang below the trunk and an
    inline series element sits on it, so the rail wires as one straight
    trunk. Grows rightward from `x_anchor`, or leftward (right edge at
    `x_anchor`) when `leftward`. Returns the row's far x edge.
    """
    x = x_anchor
    for cell in (reversed(cells) if leftward else cells):
        x_left = x - cell.w if leftward else x
        dx = _snap(x_left - cell.min_x)
        p1 = _pin_xy(cell.members[0], "1") if cell.members else None
        dy = (_snap(trunk_y - p1[1]) if p1 is not None
              else _snap((trunk_y - cell.h / 2.0) - cell.min_y))
        for comp in cell.members:
            comp.translate(dx, dy)
        placed_refs[cell.refdes] = (
            _snap(cell.min_x + dx), _snap(cell.min_y + dy),
        )
        x = x_left - STACK_GAP if leftward else x_left + cell.w + STACK_GAP
    return x


# --- pin-anchored layout (single-IC grammar) ---------------------------------

def _side_from_contacts(
    plan: LayoutPlan, refdes: str, primary_ic: str
) -> str | None:
    """Body edge (L/R/T/B) the part's non-ground nets reach on the IC.

    `None` when the part touches the IC only through ground (or not at all) —
    the caller falls back to a role default.
    """
    pc = plan.parts.get(refdes)
    if pc is None:
        return None
    collected: list[str] = []
    for net_name in pc.nets:
        nc = plan.nets.get(net_name)
        if nc is None or nc.kind == NetKind.GROUND:
            continue
        for contact in nc.ic_contacts:
            if contact.ic_refdes == primary_ic:
                collected.append(contact.pin.side)
    if not collected:
        return None
    return Counter(collected).most_common(1)[0][0]


def _target_y(
    plan: LayoutPlan, refdes: str, primary_ic: str, ic_comp
) -> float | None:
    """Desired column-centre Y for a support part: the mean true-schematic Y
    of the IC pins it should sit beside, so its wire runs short and level.

    A divider resistor targets the divider's tap (FB) pin — so the two legs
    cluster together regardless of the other rails they touch. Ground is
    ignored (it reaches every pin). Returns `None` when the part touches no
    IC pin at all (e.g. a bulk cap on an input rail with no IC power pin) —
    the caller substitutes a fallback.
    """
    pc = plan.parts.get(refdes)
    if pc is None:
        return None
    pin_nums: list[str] = []
    div = next(
        (d for d in plan.dividers if refdes in (d.high_refdes, d.low_refdes)),
        None,
    )
    if div is not None:
        tap = plan.nets.get(div.tap_net)
        if tap:
            pin_nums = [c.pin.number for c in tap.ic_contacts
                        if c.ic_refdes == primary_ic]
    if not pin_nums:
        for net_name in pc.nets:
            nc = plan.nets.get(net_name)
            if nc is None or nc.kind == NetKind.GROUND:
                continue
            pin_nums += [c.pin.number for c in nc.ic_contacts
                         if c.ic_refdes == primary_ic]
    ys: list[float] = []
    for pn in pin_nums:
        xy = _pin_xy(ic_comp, pn)
        if xy is not None:
            ys.append(xy[1])
    return sum(ys) / len(ys) if ys else None


def _layout_targeted_column(
    items: list[tuple["_Cell", float]],
    x_left: float,
    col_w: float,
    placed_refs: dict[str, tuple[float, float]],
) -> float:
    """Stack `items` (cell, target-centre-Y), pre-sorted by target Y.

    Each cell is placed centred on its target Y when there is room, and
    pushed down just enough to clear the previous cell otherwise — so a part
    sits level with the IC pin it wires to, and parts sharing a pin (output
    caps, divider legs) fall into a tight adjacent cluster. Returns the
    column's bottom edge.
    """
    prev_bottom = MARGIN - STACK_GAP
    for cell, target_y in items:
        top = target_y - cell.h / 2.0
        if top < prev_bottom + STACK_GAP:
            top = prev_bottom + STACK_GAP
        tx = x_left + (col_w - cell.w) / 2.0
        dx = _snap(tx - cell.min_x)
        dy = _snap(top - cell.min_y)
        for comp in cell.members:
            comp.translate(dx, dy)
        placed_refs[cell.refdes] = (
            _snap(cell.min_x + dx),
            _snap(cell.min_y + dy),
        )
        prev_bottom = top + cell.h
    return prev_bottom


def _modal_side(sides: list[str], default: str) -> str:
    return Counter(sides).most_common(1)[0][0] if sides else default


def _place_pin_anchored(
    sch: ksa.Schematic, netlist: Netlist, plan: LayoutPlan, issues: list[str]
) -> dict[str, tuple[float, float]]:
    """Single-IC layout: the IC anchors; support parts go to the body side
    their function serves, each side a measured non-overlapping column.

    Returns `placed_refs`. Connectivity (labels / wires) is added by the
    caller — this routine only positions components.
    """
    placed_refs: dict[str, tuple[float, float]] = {}
    primary = plan.ics[0]

    # Provisional placement + measured bboxes for every part. Series elements
    # (inductors, ferrites, series R) are rotated horizontal so they read as
    # inline in the signal path.
    ordered = sorted(netlist.parts, key=lambda p: _natural_key(p.refdes))
    series_refs = frozenset(
        r for r, pc in plan.parts.items() if pc.role == Role.SERIES_ELEMENT
    )

    # A divider reads top→bottom as rail → tap → ground. A leg resistor whose
    # "up" pin (rail for the high leg, tap for the low leg) is symbol pin 2
    # must be flipped 180°, or its ground/tap symbol lands inside the body.
    pin_net = {
        (ep.ref, ep.pin): net.name
        for net in netlist.nets for ep in net.endpoints
    }
    flip_divider: set[str] = set()
    for d in plan.dividers:
        for refdes, is_high in ((d.high_refdes, True), (d.low_refdes, False)):
            pins = sorted({p for (r, p) in pin_net if r == refdes})
            tap = next(
                (p for p in pins if pin_net.get((refdes, p)) == d.tap_net),
                None,
            )
            up = (next((p for p in pins if p != tap), None)
                  if is_high else tap)
            if up is not None and up != "1":
                flip_divider.add(refdes)

    cells = _place_and_measure(
        sch, ordered, issues, rotate_horizontal=series_refs,
        flip_180=frozenset(flip_divider),
    )
    by_ref = {c.refdes: c for c in cells}
    ic_cell = by_ref.get(primary)
    if ic_cell is None:  # _place_and_measure raises on IC failure — defensive
        return placed_refs

    # Side for each support part: the IC-body edge its non-ground nets reach.
    sides: dict[str, str] = {}
    for p in netlist.parts:
        if p.refdes == primary or p.refdes not in by_ref:
            continue
        s = _side_from_contacts(plan, p.refdes, primary)
        if s is not None:
            sides[p.refdes] = s

    # Input / output sides: where the parts that *did* resolve cluster. Used
    # as the fallback for parts that touch the IC only through ground (e.g. a
    # bulk input cap on a rail with no power symbol on an IC pin).
    in_side = _modal_side(
        [sides[r] for r in plan.with_role(Role.INPUT_CAP) if r in sides], "L"
    )
    out_side = _modal_side(
        [sides[r] for r in plan.with_role(Role.OUTPUT_CAP) if r in sides], "R"
    )
    role_fallback = {
        Role.INPUT_CAP: in_side,
        Role.OUTPUT_CAP: out_side,
        Role.DIVIDER_RESISTOR: out_side,
        Role.CONFIG_CAP: in_side,
        Role.SERIES_ELEMENT: in_side,
        Role.CONNECTOR: in_side,
    }

    # Each support part goes to the IC body side its function serves; the
    # side is laid as a *targeted* column — every part pulled level with the
    # IC pin it connects to (see `_target_y` / `_layout_targeted_column`).
    left: list[str] = []
    right: list[str] = []
    for p in netlist.parts:
        if p.refdes == primary or p.refdes not in by_ref:
            continue
        s = sides.get(p.refdes)
        if s is None:
            s = role_fallback.get(plan.parts[p.refdes].role, in_side)
        # Two-sided ICs are the norm; fold any top/bottom parts onto a column.
        (left if s in ("L", "T") else right).append(p.refdes)

    # Rail-cap rows. A power rail with several decoupling caps is drawn as a
    # horizontal trunk with the caps hanging off it. Non-ground nets bridged
    # by a series element (ferrite, series R) are one logical rail — so an
    # input rail a ferrite splits in two still clusters its caps into one row.
    _rail_parent: dict[str, str] = {}

    def _rail_root(net: str) -> str:
        _rail_parent.setdefault(net, net)
        root = net
        while _rail_parent[root] != root:
            root = _rail_parent[root]
        _rail_parent[net] = root
        return root

    def _nonground_nets(refdes: str) -> list[str]:
        pc = plan.parts.get(refdes)
        if pc is None:
            return []
        return [
            n for n in pc.nets
            if (nc := plan.nets.get(n)) is not None
            and nc.kind != NetKind.GROUND
        ]

    for p in netlist.parts:
        pc = plan.parts.get(p.refdes)
        if pc is not None and pc.role == Role.SERIES_ELEMENT:
            nn = _nonground_nets(p.refdes)
            for other in nn[1:]:
                _rail_parent[_rail_root(other)] = _rail_root(nn[0])

    def _rail_group(refdes: str) -> str | None:
        nn = _nonground_nets(refdes)
        return _rail_root(nn[0]) if nn else None

    # IC-touching nets — for ordering a rail row outer (flag) → inner (IC).
    ic_nets = {
        net.name for net in netlist.nets
        if any(ep.ref in plan.ics for ep in net.endpoints)
    }

    def _order_rail_row(members: list[str]) -> list[str]:
        # outer caps (rail-flag end), then series elements, then inner caps
        # (the IC end) — the row then reads in rail order, flag → IC.
        def _key(r: str) -> tuple[int, tuple]:
            pc = plan.parts.get(r)
            if pc is not None and pc.role == Role.SERIES_ELEMENT:
                rank = 1
            elif any(n in ic_nets for n in _nonground_nets(r)):
                rank = 2
            else:
                rank = 0
            return (rank, _natural_key(r))
        return sorted(members, key=_key)

    def _extract_rail_rows(
        refs: list[str],
    ) -> tuple[list[list[str]], list[str]]:
        cap_by_group: dict[str, list[str]] = {}
        for r in refs:
            pc = plan.parts.get(r)
            if pc is None or pc.role not in (Role.INPUT_CAP, Role.OUTPUT_CAP):
                continue
            g = _rail_group(r)
            if g is not None:
                cap_by_group.setdefault(g, []).append(r)

        rows: list[list[str]] = []
        used: set[str] = set()
        for g, caps in cap_by_group.items():
            if len(caps) < 2:
                continue
            series = [
                r for r in refs
                if plan.parts.get(r) is not None
                and plan.parts[r].role == Role.SERIES_ELEMENT
                and _rail_group(r) == g
            ]
            rows.append(_order_rail_row(caps + series))
            used.update(caps)
            used.update(series)
        return rows, [r for r in refs if r not in used]

    left_rows, left = _extract_rail_rows(left)
    right_rows, right = _extract_rail_rows(right)

    # Divider stacks: a feedback divider is pulled from its column and placed
    # as a tight vertical stack beside the IC, centred on the FB pin — so its
    # tap wires to FB as a short local loop instead of detouring.
    divider_legs = {
        r for d in plan.dividers
        for r in (d.high_refdes, d.low_refdes) if r in by_ref
    }
    left_div = [r for r in left if r in divider_legs]
    right_div = [r for r in right if r in divider_legs]
    left = [r for r in left if r not in divider_legs]
    right = [r for r in right if r not in divider_legs]
    left_div_w = max((by_ref[r].w for r in left_div), default=0.0)
    right_div_w = max((by_ref[r].w for r in right_div), default=0.0)

    left_w = max((by_ref[r].w for r in left), default=0.0)
    right_w = max((by_ref[r].w for r in right), default=0.0)

    def _row_width(row: list[str]) -> float:
        return (sum(by_ref[r].w for r in row)
                + STACK_GAP * max(0, len(row) - 1))

    left_row_w = max((_row_width(rw) for rw in left_rows), default=0.0)

    # A rail-cap row occupies a top band — the rail trunk (pin-1 line) with
    # headroom above for the power flag and the cap bodies hanging below.
    # The IC drops beneath that band.
    row_refs = [r for rw in left_rows + right_rows for r in rw]
    row_h = max((by_ref[r].h for r in row_refs), default=0.0)
    trunk_y = MARGIN + RAIL_FLAG_GAP
    ic_top = (trunk_y + row_h + STACK_GAP) if row_refs else BLOCK_Y_TOP

    # The IC sits right of the left column, the left divider lane, and the
    # (often wider) left rail row, so leftward layout stays on the page.
    left_col_total = left_w + (left_div_w + COL_GAP if left_div else 0.0)
    left_extent = max(left_col_total, left_row_w)
    ic_x = MARGIN + (left_extent + COL_GAP
                     if (left or left_div or left_rows) else 0.0)
    _layout_column([ic_cell], ic_x, ic_top, ic_cell.w, placed_refs)
    ic_comp = sch.components.get(primary)

    # Divider legs: high (rail) leg sorts above the low (ground) leg.
    div_rank: dict[str, int] = {}
    for d in plan.dividers:
        div_rank[d.high_refdes] = 0
        div_rank[d.low_refdes] = 1

    targets: dict[str, float] = {}
    for refdes in left + right:
        ty = _target_y(plan, refdes, primary, ic_comp)
        targets[refdes] = ty if ty is not None else BLOCK_Y_TOP

    def _column(refs: list[str]) -> list[tuple["_Cell", float]]:
        ordered_refs = sorted(
            refs,
            key=lambda r: (targets[r], div_rank.get(r, 0), _natural_key(r)),
        )
        return [(by_ref[r], targets[r]) for r in ordered_refs]

    # The divider lane sits innermost (next to the IC); the support column
    # is pushed out beyond it.
    right_col_x = (ic_x + ic_cell.w + COL_GAP
                   + (right_div_w + COL_GAP if right_div else 0.0))
    left_col_x = (ic_x - COL_GAP - left_w
                  - (left_div_w + COL_GAP if left_div else 0.0))
    if left:
        _layout_targeted_column(_column(left), left_col_x, left_w, placed_refs)
    if right:
        _layout_targeted_column(
            _column(right), right_col_x, right_w, placed_refs
        )

    # Rail-cap rows sit in a band above the IC; the router then wires each
    # shared rail as one horizontal trunk through the row.
    # Rows are pre-ordered outer→inner by `_order_rail_row`; keep that order.
    # Each row sits in the top band on its own side of the IC: a right-side
    # row grows rightward from the IC's right edge, a left-side row grows
    # leftward from its left edge — so the two never overlap.
    for row in right_rows:
        _layout_row([by_ref[r] for r in row], ic_x + ic_cell.w, trunk_y,
                    placed_refs, leftward=False)
    for row in left_rows:
        _layout_row([by_ref[r] for r in row], ic_x, trunk_y, placed_refs,
                    leftward=True)

    # Divider stacks — a tight vertical column centred on the FB pin, so the
    # tap sits level with FB and wires to it as a short local loop.
    def _place_divider(legs: list[str], x_left: float) -> None:
        if not legs:
            return
        cells = [by_ref[r]
                 for r in sorted(legs, key=lambda r: div_rank.get(r, 0))]
        stack_h = (sum(c.h for c in cells)
                   + STACK_GAP * max(0, len(cells) - 1))
        fb_y = _target_y(plan, cells[0].refdes, primary, ic_comp)
        top = (fb_y - stack_h / 2.0) if fb_y is not None else ic_top
        _layout_column(cells, x_left, top, max(c.w for c in cells),
                       placed_refs)

    _place_divider(right_div, ic_x + ic_cell.w + COL_GAP)
    _place_divider(left_div, ic_x - COL_GAP - left_div_w)

    return placed_refs


def _place_columns(
    sch: ksa.Schematic, netlist: Netlist, issues: list[str]
) -> dict[str, tuple[float, float]]:
    """Fallback layout (zero or several ICs): measured IC-block columns.

    Each IC is a block of [other-passives column][decoupler column][IC];
    blocks run left→right, wrapping to new rows; connectors at the left
    margin; shared parts below. Used when the pin-anchored grammar doesn't
    apply (it assumes exactly one IC to anchor on).
    """
    placed_refs: dict[str, tuple[float, float]] = {}

    nets_by_name: dict[str, NetlistNet] = {n.name: n for n in netlist.nets}
    nets_by_part: dict[str, set[str]] = {p.refdes: set() for p in netlist.parts}
    for net in netlist.nets:
        for ep in net.endpoints:
            if ep.ref in nets_by_part:
                nets_by_part[ep.ref].add(net.name)

    ics, connectors, other_by_host = _bucket_parts(netlist)
    ics_sorted = sorted(ics, key=lambda p: _natural_key(p.refdes))
    conns_sorted = sorted(connectors, key=lambda p: _natural_key(p.refdes))
    shared = other_by_host.get("__shared__", [])

    ordered: list[NetlistPart] = list(ics_sorted)
    for ic in ics_sorted:
        ordered.extend(other_by_host.get(ic.refdes, []))
    ordered.extend(shared)
    ordered.extend(conns_sorted)
    cells = _place_and_measure(sch, ordered, issues)
    by_ref = {c.refdes: c for c in cells}

    conn_cells = [by_ref[c.refdes] for c in conns_sorted if c.refdes in by_ref]
    conn_w = max((c.w for c in conn_cells), default=0.0)
    if conn_cells:
        _layout_column(conn_cells, MARGIN, BLOCK_Y_TOP, conn_w, placed_refs)
        block_x0 = MARGIN + conn_w + BLOCK_GAP
    else:
        block_x0 = MARGIN

    cursor_x = block_x0
    y_top = BLOCK_Y_TOP
    row_bottom = y_top
    for ic in ics_sorted:
        ic_cell = by_ref.get(ic.refdes)
        if ic_cell is None:
            continue
        cohort = other_by_host.get(ic.refdes, [])
        decouplers, others = _split_decouplers(cohort, nets_by_part, nets_by_name)
        d_cells = [by_ref[p.refdes] for p in decouplers if p.refdes in by_ref]
        o_cells = [by_ref[p.refdes] for p in others if p.refdes in by_ref]
        d_w = max((c.w for c in d_cells), default=0.0)
        o_w = max((c.w for c in o_cells), default=0.0)

        block_w = ic_cell.w
        if o_cells:
            block_w += o_w + COL_GAP
        if d_cells:
            block_w += d_w + COL_GAP

        if cursor_x > block_x0 and cursor_x + block_w > PAGE_W - MARGIN:
            cursor_x = block_x0
            y_top = row_bottom + ROW_GAP
            row_bottom = y_top

        x = cursor_x
        if o_cells:
            row_bottom = max(
                row_bottom, _layout_column(o_cells, x, y_top, o_w, placed_refs)
            )
            x += o_w + COL_GAP
        if d_cells:
            row_bottom = max(
                row_bottom, _layout_column(d_cells, x, y_top, d_w, placed_refs)
            )
            x += d_w + COL_GAP
        row_bottom = max(
            row_bottom,
            _layout_column([ic_cell], x, y_top, ic_cell.w, placed_refs),
        )
        cursor_x += block_w + BLOCK_GAP

    shared_cells = [by_ref[p.refdes] for p in shared if p.refdes in by_ref]
    if shared_cells:
        shared_w = max(c.w for c in shared_cells)
        shared_y = (row_bottom + ROW_GAP) if ics_sorted else BLOCK_Y_TOP
        _layout_column(shared_cells, block_x0, shared_y, shared_w, placed_refs)

    return placed_refs


# --- connectivity: power/ground symbols + labels -----------------------------

# lib_ids from _POWER_LIB_IDS that are *ground* symbols (body hangs away from
# the pin "downward" at 0°); the rest are positive-rail symbols (body "up").
_GND_LIB_IDS = {"power:GND", "power:GNDA", "power:GNDD", "power:GNDPWR"}

# Power-symbol orientation is fixed by convention, not by the pin: ground
# always points down, positive rails always point up. Both are rotation 0
# (the symbols' drawn default) — `power:GND` body hangs down, `power:+3V3`
# and friends extend up.
_PWR_ROT = 0.0

_NET_NAME_RE = re.compile(r"^Net-\(.+?-(.+)\)$")


def _xy(pt) -> tuple[float, float]:
    return (pt.x if hasattr(pt, "x") else pt[0],
            pt.y if hasattr(pt, "y") else pt[1])


def _pin_xy(comp, pin_num: str) -> tuple[float, float] | None:
    """True schematic connection-point of `pin_num` on a placed component.

    NOT `comp.get_pin_position()` — that adds the pin's library-frame offset
    to the instance origin *without the library→schematic Y mirror* KiCad
    applies when instantiating a symbol, so it lands at the Y-reflection of
    the real pin. (Harmless for a symmetric 2-pin part — the pins merely
    swap — but it leaves every other pin disconnected.) We take the library-
    local offset from `comp.pins`, flip Y, apply the instance rotation, then
    translate to the instance origin.

    Rotation: KiCad's stored rotation is visually CCW in the Y-down sch
    frame, equivalent to math CW after the Y-flip. The matrix below applies
    `-rot` so the math-CCW formula reproduces KiCad's behaviour. Without the
    sign flip, pin 1 ↔ pin 2 silently transposes at rot=90/270 — invisible
    for symmetric R/C/L, but reverses polarised parts (LEDs, diodes).
    """
    target = str(pin_num)
    for p in comp.pins:
        if str(p.number) != target:
            continue
        lx, ly = _xy(p.position)
        cx, cy = _xy(comp.position)
        dx, dy = lx, -ly  # library frame is Y-up; schematic Y-down
        rot = float(getattr(comp, "rotation", 0.0) or 0.0)
        if rot:
            a = math.radians(-rot)
            ca, sa = math.cos(a), math.sin(a)
            dx, dy = dx * ca - dy * sa, dx * sa + dy * ca
        return (cx + dx, cy + dy)
    return None


def _away_dir(comp, pin_xy: tuple[float, float]) -> str:
    """U/D/L/R direction the pin points away from the component's body."""
    xs: list[float] = []
    ys: list[float] = []
    for p in comp.pins:
        xy = _pin_xy(comp, str(p.number))
        if xy is not None:
            xs.append(xy[0])
            ys.append(xy[1])
    if not xs:
        return "D"
    dx = pin_xy[0] - sum(xs) / len(xs)
    dy = pin_xy[1] - sum(ys) / len(ys)
    if abs(dx) >= abs(dy):
        return "R" if dx > 0 else "L"
    return "D" if dy > 0 else "U"  # KiCad schematic Y grows downward


# A GND-symbol drop whose end (or its drop wire) lands on a pin closer than this
# is treated as landing *on* that pin. Pins and the GND_STUB drop share the grid
# and the same `_pin_xy` transform, so a genuine coincidence is exact to well
# under this; the nearest distinct grid node is 1.27 mm away, so it never false-
# positives on a merely-adjacent pin.
_GND_PIN_EPS = 1e-3


def _drop_hits_foreign(
    pin_xy: tuple[float, float],
    sym_xy: tuple[float, float],
    foreign: list[tuple[float, float]],
) -> bool:
    """True if a GND drop (pin → symbol) shorts a foreign net — i.e. the symbol
    lands on a foreign-net pin, or the drop wire passes over one. `foreign` is
    every pin *not* on the dropping net, so a same-net coincidence (harmless) is
    never flagged. Guards the generalisation of the side-pin step-out below."""
    seg = (pin_xy, sym_xy)
    for q in foreign:
        if (abs(sym_xy[0] - q[0]) < _GND_PIN_EPS
                and abs(sym_xy[1] - q[1]) < _GND_PIN_EPS):
            return True
        if _interior(q, seg):
            return True
    return False


def _net_alias_map(
    netlist: Netlist, plan: LayoutPlan
) -> dict[str, str]:
    """Short label text for each non-power net.

    KiCad auto-names a net `Net-(<ref>-<pin>)`; the `<pin>` part, minus any
    trailing `_<n>`, is a far better label (`Net-(U1-VIN_10)` → `VIN`).
    Falls back to the IC contact pin name, then the raw name. Uniqueness is
    enforced so two distinct nets never collapse onto one label.
    """
    aliases: dict[str, str] = {}
    used: set[str] = {n.name for n in netlist.nets if n.is_power}
    for net in netlist.nets:
        if net.is_power:
            continue
        cand = ""
        m = _NET_NAME_RE.match(net.name)
        if m:
            cand = re.sub(r"_\d+$", "", m.group(1)).strip()
        if not cand:
            nc = plan.nets.get(net.name)
            if nc and nc.ic_contacts:
                cand = re.sub(r"_\d+$", "", nc.ic_contacts[0].pin.name).strip()
        if not cand:
            aliases[net.name] = net.name
            used.add(net.name)
            continue
        base, n = cand, 2
        while cand in used:
            cand = f"{base}{n}"
            n += 1
        aliases[net.name] = cand
        used.add(cand)
    return aliases


_DIR_VEC = {"L": (-1.0, 0.0), "R": (1.0, 0.0), "U": (0.0, -1.0), "D": (0.0, 1.0)}
_WIRE_STUB = 2.54   # mm a wire runs straight off a pin before it may turn


def _add_path(sch: ksa.Schematic, pts: list[tuple[float, float]]) -> None:
    """Add a wire segment for each non-degenerate consecutive pair."""
    for p, q in zip(pts, pts[1:]):
        if abs(p[0] - q[0]) > 1e-6 or abs(p[1] - q[1]) > 1e-6:
            sch.add_wire(p, q)


def _route_wire(
    sch: ksa.Schematic,
    a: tuple[float, float], da: str,
    b: tuple[float, float], db: str,
) -> None:
    """Orthogonal wire from pin `a` (away dir `da`) to pin `b` (away dir `db`).

    Each pin is left by a short stub *along its own away direction*, so the
    wire never runs back along the part's pin axis through its other pin.
    The stubs are then joined by an orthogonal path that departs each stub
    *perpendicular* to that pin — keeping the run off both parts' axes.
    """
    dax, day = _DIR_VEC[da]
    dbx, dby = _DIR_VEC[db]
    pa = (a[0] + dax * _WIRE_STUB, a[1] + day * _WIRE_STUB)
    pb = (b[0] + dbx * _WIRE_STUB, b[1] + dby * _WIRE_STUB)
    a_h = da in ("L", "R")
    b_h = db in ("L", "R")
    if a_h and not b_h:
        mid = [pa, (pa[0], pb[1]), pb]
    elif not a_h and b_h:
        mid = [pa, (pb[0], pa[1]), pb]
    elif a_h and b_h:
        mx = (pa[0] + pb[0]) / 2.0
        mid = [pa, (mx, pa[1]), (mx, pb[1]), pb]
    else:
        my = (pa[1] + pb[1]) / 2.0
        mid = [pa, (pa[0], my), (pb[0], my), pb]
    _add_path(sch, [a] + mid + [b])


def _gnd_pin_clusters(pins: list) -> list[list]:
    """Group one part's ground pins into runs of vertically adjacent pins —
    same x, no more than one pin pitch apart. Within such a run nothing else
    can sit between the pins, so a wire linking them crosses no foreign pin
    and the whole run can share a single ground symbol. `pins` items are the
    `(ref, pin, xy, comp)` records resolved by `_place_connectivity`.
    """
    items = sorted(pins, key=lambda r: (round(r[2][0], 2), r[2][1]))
    clusters: list[list] = []
    for it in items:
        x, y = it[2]
        if clusters:
            px, py = clusters[-1][-1][2]
            if abs(x - px) < 0.05 and 0.0 < y - py <= 2.54 + 0.05:
                clusters[-1].append(it)
                continue
        clusters.append([it])
    return clusters


def _ic_keepouts(
    sch: ksa.Schematic, ic_refs: list[str],
) -> list[tuple[float, float, float, float]]:
    """Body rects (symbol+pins) of the IC anchors, for the router to route
    around. Pin connections terminate on the box edge, not its interior, so
    feeding the full box never blocks a legitimate pin wire — only segments
    that traverse the chip register against it (see `route._seg_hits_rect`)."""
    rects: list[tuple[float, float, float, float]] = []
    for ref in ic_refs:
        comp = sch.components.get(ref)
        if comp is None:
            continue
        try:
            bb = get_component_bounding_box(comp, include_properties=False)
        except Exception:  # noqa: BLE001 — a missing symbol just skips keep-out
            continue
        rects.append((bb.min_x, bb.min_y, bb.max_x, bb.max_y))
    return rects


# A net counts as IC-spanning when its routed path reaches this far past BOTH
# opposite flanks of the IC body — one full grid step, so a detour that wraps
# just past one edge never qualifies (`route._outside` snaps one step out).
_SPAN_PAD = 2.54


def _wire_router(
    wired: list, all_pins: list[tuple[float, float]],
    rail_y_hints: dict[str, float] | None = None,
    keepouts: list[tuple[float, float, float, float]] | None = None,
) -> list:
    """Route the IC-touching nets with the crossing-minimising router and
    return the routed nets (one per input net). `wired` is a list of
    `(net, pts)` where `pts` are `(ref, pin, xy, comp)` records.

    Wires are NOT committed here — the caller decides per net whether to draw
    the wire or, for a net that spans the IC side-to-side, drop net labels
    instead. `rail_y_hints` and `keepouts` pass straight through to
    `route_nets` (the staircase-trunk-Y and IC keep-out cases)."""
    return route_nets(
        [(net.name, [p[2] for p in pts]) for net, pts in wired], all_pins,
        rail_y_hints=rail_y_hints, keepouts=keepouts,
    )


def _commit_wires(sch: ksa.Schematic, routed: list, issues: list[str]) -> None:
    """Draw the wire segments of each routed net onto the schematic."""
    for rn in routed:
        for a, b in rn.segments:
            try:
                sch.add_wire(a, b)
            except Exception as e:
                issues.append(f"wire on {rn.name}: {e}")


def _net_crosses_ic(rn, ic_rects: list[tuple[float, float, float, float]]) -> bool:
    """True if the routed net should be a label rather than a wire because its
    path crosses an IC badly — either of:

    - **Side-to-side span**: reaches past both opposite flanks (`_SPAN_PAD`
      beyond each) while engaging the IC's perpendicular band — the long run
      from one side across to the other, through the body OR wrapped around it.
    - **Through-body cut**: a single segment runs more than half-way through the
      body interior (the rubric's own `wire_through_part` definition), e.g. a
      pin diving into the middle of the chip.

    Decided on the ACTUAL routed segments, not pin coordinates, so a same-side
    run or a clean one-edge wrap (reaches only one flank, never enters the
    interior) stays a wire. Both crossing cases are eyesores a same-named net
    label removes cleanly."""
    if not rn.segments:
        return False
    pts = [p for seg in rn.segments for p in seg]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    for x0, y0, x1, y1 in ic_rects:
        # (a) side-to-side span past both opposite flanks, engaging the band.
        in_yband = any(y0 <= p[1] <= y1 for p in pts)
        in_xband = any(x0 <= p[0] <= x1 for p in pts)
        if min_x <= x0 - _SPAN_PAD and max_x >= x1 + _SPAN_PAD and in_yband:
            return True
        if min_y <= y0 - _SPAN_PAD and max_y >= y1 + _SPAN_PAD and in_xband:
            return True
        # (b) a segment cutting >half-way through the (1mm-shrunk) interior.
        ix0, iy0, ix1, iy1 = x0 + 1.0, y0 + 1.0, x1 - 1.0, y1 - 1.0
        if ix1 - ix0 < 1e-6 or iy1 - iy0 < 1e-6:
            continue
        for (ax, ay), (bx, by) in rn.segments:
            if abs(ay - by) < 1e-6 and iy0 < ay < iy1:           # horizontal
                run = min(max(ax, bx), ix1) - max(min(ax, bx), ix0)
                if run > 0.5 * (ix1 - ix0):
                    return True
            elif abs(ax - bx) < 1e-6 and ix0 < ax < ix1:         # vertical
                run = min(max(ay, by), iy1) - max(min(ay, by), iy0)
                if run > 0.5 * (iy1 - iy0):
                    return True
    return False


def _relabel_spanning(
    sch: ksa.Schematic, net, pts: list, src_idx: int, text: str,
    label_specs: list, issues: list[str],
) -> None:
    """Drop a net label at every pin of an IC-spanning net except its source
    pin (which already carries the inline source symbol/label) — connectivity
    by name, no wire. KiCad merges same-named labels, so topology is preserved
    while the side-to-side wire disappears."""
    for i, (ref, pin, xy, comp) in enumerate(pts):
        if i == src_idx:
            continue
        sch.add_label(text=text, position=xy)
        label_specs.append(
            LabelSpec(ref=ref, pin=pin, net_name=text, position=xy)
        )
    issues.append(f"net {net.name!r} spans the IC — labelled instead of wired")


def _wire_star(
    sch: ksa.Schematic, wired: list, plan: LayoutPlan, issues: list[str],
) -> None:
    """Legacy wiring: a star from the IC hub pin per net. Every edge runs
    hub→part rather than weaving between column parts, but the fans of
    different nets can still run *collinearly* and be merged by KiCad's
    netlister — so this is NOT short-safe (it once was assumed to be). The
    short-safe fallback is now the "labels" engine; `_wire_star` is kept
    only as a forced diagnostic mode (see `place`)."""
    for _net, pts in wired:
        if len(pts) < 2:
            continue
        hub_i = next((i for i, p in enumerate(pts) if p[0] in plan.ics), 0)
        hub_xy = pts[hub_i][2]
        hub_away = _away_dir(pts[hub_i][3], hub_xy)
        for i, (_ref, _pin, xy, comp) in enumerate(pts):
            if i != hub_i:
                _route_wire(sch, hub_xy, hub_away, xy, _away_dir(comp, xy))
    issues.append("wiring: legacy star-fan (router fallback)")


def _place_connectivity(
    sch: ksa.Schematic,
    netlist: Netlist,
    plan: LayoutPlan,
    placed_refs: dict[str, tuple[float, float]],
    issues: list[str],
    *,
    wiring: str = "router",
    rail_y_hints: dict[str, float] | None = None,
    label_only_nets: set[str] | None = None,
) -> list[LabelSpec]:
    """Render every net's connectivity.

    - Ground / no-IC nets → a `power:GND` / rail symbol or label terminated
      right at each pin: geometry-free, distributed, never wired.
    - IC-touching rail / signal nets → `wiring` picks the engine:
      "router" (crossing-minimising orthogonal router, `emit/route`) and
      "star" (legacy star-fan) both draw orthogonal wires plus one `power:*`
      symbol or aliased label per net; "labels" draws no wires and instead
      drops a net label at every pin (short-safe by construction — the
      fallback when the router corrupts topology).

    Returns `label_specs` (the labels emitted, for the structural validator).
    Raises `PlacerError` if a pin can't be located.
    """
    label_specs: list[LabelSpec] = []
    label_errors: list[str] = []
    aliases = _net_alias_map(netlist, plan)
    pwr_n = 1

    # Resolve every net's endpoints to true pin positions up front — the
    # router needs every pin in the schematic (corners must dodge foreign
    # ones), not just the pins of the net it is currently routing.
    net_pts: dict[str, list[tuple[str, str, tuple[float, float], object]]] = {}
    all_pins: list[tuple[float, float]] = []
    for net in netlist.nets:
        pts: list[tuple[str, str, tuple[float, float], object]] = []
        for ep in net.endpoints:
            if ep.ref not in placed_refs:
                continue
            comp = sch.components.get(ep.ref)
            if comp is None:
                label_errors.append(f"missing component {ep.ref}")
                continue
            xy = _pin_xy(comp, ep.pin)
            if xy is None:
                label_errors.append(
                    f"pin {ep.pin!r} not found on {ep.ref} ({comp.lib_id})"
                )
                continue
            pts.append((ep.ref, ep.pin, xy, comp))
        net_pts[net.name] = pts
        all_pins.extend(p[2] for p in pts)

    # Ground gets distributed symbols; every other net is wired by the
    # router (a visible net). IC-touching and no-IC nets alike are routed —
    # the crossing-aware router handles both safely, and `place()` falls back
    # to the star-fan if a routed result fails the connectivity check.
    wired: list = []
    # For each wired net, the pin index that carries its inline source
    # symbol/label — so a net later reclassified as IC-spanning can label its
    # *other* pins without duplicating the source.
    routed_src: dict[str, int] = {}
    for net in netlist.nets:
        pts = net_pts[net.name]
        if not pts:
            continue
        lib_id = _power_lib_id(net.name) if net.is_power else None
        is_gnd = lib_id in _GND_LIB_IDS
        text = aliases.get(net.name, net.name)

        # Label-only mode: every net — ground and rails included — gets a net
        # label at every pin and nothing else (no wires, no power symbols).
        # Connectivity is purely by label name, so no geometry can short two
        # differently-named nets: neither a collinear star-fan run nor a
        # ground symbol landing on a neighbouring IC pin. Short-safe by
        # construction — this is the fallback when the router corrupts
        # topology. Degraded-looking; the router path is the pretty one.
        if wiring == "labels":
            for ref, pin, xy, comp in pts:
                sch.add_label(text=text, position=xy)
                label_specs.append(
                    LabelSpec(ref=ref, pin=pin, net_name=text, position=xy)
                )
            continue

        # Per-net labels-only mode: a specific net (e.g. a SIGNAL_STAIRCASE
        # IC-signal net, an inter-tap compensation net, or a rail being
        # cap-islanded) gets labels at every pin — connectivity by name.
        # Used by archetypes whose layout would otherwise force the router
        # to drag long wires across other parts. Ground is excluded so its
        # dedicated handler (cluster-with-drop) still runs; power rails are
        # allowed so a dense IC's input filter can be cap-islanded.
        #
        # For power rails, one pin gets a power *symbol* (the visible
        # arrow) instead of a text label — the source indicator. Stock
        # rails (`+5V`, `+3V3`, `GND`, …) use their library symbol; non-
        # stock names like `+9V` or `+6V6` fall back to `power:+5V` with
        # the Value field overridden to the actual rail name (same trick
        # /examples greedy commit 0feb49f uses). Source pin choice: first
        # non-IC pin if any, else the first pin — for cap-island layouts
        # the non-IC pin is the series element or far-edge cap, where the
        # source arrow naturally belongs.
        if (label_only_nets and net.name in label_only_nets and not is_gnd):
            src_idx = -1
            if net.is_power:
                # Pick the source pin: among non-IC pins, the one farthest
                # from the IC body. For an input rail this lands on the
                # leftmost cap or series element (L1's rail pin); for an
                # output rail, the rightmost. The `+V` arrow then sits at
                # the schematic edge — the "power-at-edge" idiom.
                non_ic = [i for i, p in enumerate(pts)
                          if p[0] not in plan.ics]
                if non_ic:
                    ic_ref = plan.ics[0] if plan.ics else None
                    ic_comp = (sch.components.get(ic_ref)
                                if ic_ref else None)
                    ic_x = (float(ic_comp.position.x)
                            if ic_comp is not None else 0.0)
                    src_idx = max(non_ic,
                                  key=lambda i: abs(pts[i][2][0] - ic_x))
                else:
                    src_idx = 0
                sym_lib_id = lib_id or "power:+5V"
                ref, pin, xy, comp = pts[src_idx]
                try:
                    sch.components.add(
                        lib_id=sym_lib_id, reference=f"#PWR{pwr_n:03d}",
                        value=net.name, position=xy, rotation=_PWR_ROT,
                    )
                    pwr_n += 1
                except Exception as e:
                    issues.append(
                        f"rail source symbol {sym_lib_id} ({net.name}): {e}"
                    )
                    src_idx = -1
            for i, (ref, pin, xy, comp) in enumerate(pts):
                if i == src_idx:
                    continue
                sch.add_label(text=text, position=xy)
                label_specs.append(
                    LabelSpec(ref=ref, pin=pin, net_name=text, position=xy)
                )
            continue

        if is_gnd:
            # Ground is distributed. A part's pins are clustered by adjacency
            # (`_gnd_pin_clusters`): a run of vertically adjacent ground pins
            # is wired into one short chain dropping to a *single* power:GND
            # symbol, rather than one cramped symbol per pin. Same-named
            # symbols still connect the clusters by net name.
            #
            # `foreign` = every placed pin not on this ground net (`all_pins`
            # is complete here — built in the first pass above). A symbol drop
            # landing on one of these silently shorts the two nets; the drop
            # direction below steps clear of any such pin.
            own = [rec[2] for rec in pts]
            foreign = [
                q for q in all_pins
                if not any(abs(q[0] - o[0]) < _GND_PIN_EPS
                           and abs(q[1] - o[1]) < _GND_PIN_EPS for o in own)
            ]
            per_part: dict[str, list] = {}
            for rec in pts:
                per_part.setdefault(rec[0], []).append(rec)
            for plist in per_part.values():
                for cluster in _gnd_pin_clusters(plist):
                    for a, b in zip(cluster, cluster[1:]):
                        try:
                            sch.add_wire(a[2], b[2])
                        except Exception as e:
                            issues.append(f"ground link {a[0]}.{a[1]}: {e}")
                    ref, pin, xy, comp = cluster[-1]
                    # A straight drop (xy[1] + GND_STUB) lands the symbol one
                    # pin-pitch below the pin — fine when that spot is empty,
                    # but if a foreign pin sits there (a side IC pin's
                    # neighbour, or a part stacked one stub below) the drop
                    # silently shorts the two nets. Pick the first drop
                    # direction that clears every foreign pin. The default —
                    # side pins step out along `away`, others drop down — is
                    # tried first, so a well-spaced layout (e.g. every cplace
                    # golden) reproduces the prior geometry exactly; only a
                    # real collision falls through to the alternatives.
                    away = _away_dir(comp, xy)
                    primary = away if away in ("L", "R") else "D"
                    sym_xy = None
                    for d in (primary, *(o for o in ("D", "L", "R", "U")
                                         if o != primary)):
                        cand = (xy[0] + _DIR_VEC[d][0] * GND_STUB,
                                xy[1] + _DIR_VEC[d][1] * GND_STUB)
                        if not _drop_hits_foreign(xy, cand, foreign):
                            sym_xy = cand
                            break
                    if sym_xy is None:
                        # Nothing clear one stub out — a double-length stub in
                        # the default direction clears a pin exactly one pitch
                        # away; else give up to the default (no worse than
                        # before, and the connectivity gate still catches it).
                        lng = (xy[0] + _DIR_VEC[primary][0] * 2 * GND_STUB,
                               xy[1] + _DIR_VEC[primary][1] * 2 * GND_STUB)
                        sym_xy = lng if not _drop_hits_foreign(xy, lng, foreign) \
                            else (xy[0] + _DIR_VEC[primary][0] * GND_STUB,
                                  xy[1] + _DIR_VEC[primary][1] * GND_STUB)
                    try:
                        sch.components.add(
                            lib_id=lib_id, reference=f"#PWR{pwr_n:03d}",
                            value=net.name, position=sym_xy,
                            rotation=_PWR_ROT,
                        )
                        pwr_n += 1
                        sch.add_wire(xy, sym_xy)
                    except Exception as e:
                        issues.append(f"ground symbol at {ref}.{pin}: {e}")
            continue

        # Routed/star path: one symbol (rail) or label (signal) names the
        # net; prefer a non-IC endpoint so it doesn't crowd an IC pin.
        wired.append((net, pts))
        # Pick the source pin for the rail symbol/label: among non-IC
        # pins, the one farthest from the IC body (largest |x - ic_x|).
        # For an input rail this lands on the leftmost cap; for output,
        # the rightmost. Visually marks the rail's edge of the schematic,
        # not a random midpoint. Same heuristic as the labels-only branch
        # above. Falls back to first non-IC pin if no IC reference.
        non_ic = [i for i, p in enumerate(pts) if p[0] not in plan.ics]
        if non_ic:
            ic_ref = plan.ics[0] if plan.ics else None
            ic_comp_obj = sch.components.get(ic_ref) if ic_ref else None
            ic_x = (float(ic_comp_obj.position.x)
                    if ic_comp_obj is not None else 0.0)
            si = max(non_ic, key=lambda i: abs(pts[i][2][0] - ic_x))
        else:
            si = 0
        routed_src[net.name] = si
        ref, pin, xy, comp = pts[si]
        if lib_id is not None:
            try:
                sch.components.add(
                    lib_id=lib_id, reference=f"#PWR{pwr_n:03d}",
                    value=net.name, position=xy, rotation=_PWR_ROT,
                )
                pwr_n += 1
            except Exception as e:
                issues.append(f"rail symbol {lib_id} ({net.name}): {e}")
        else:
            sch.add_label(text=text, position=xy)
            label_specs.append(
                LabelSpec(ref=ref, pin=pin, net_name=text, position=xy)
            )

    if wiring == "star":
        _wire_star(sch, wired, plan, issues)
    elif wiring == "router":
        ic_rects = _ic_keepouts(sch, plan.ics)
        routed = _wire_router(wired, all_pins, rail_y_hints=rail_y_hints,
                              keepouts=ic_rects)
        by_name = {net.name: (net, pts) for net, pts in wired}
        committed: list = []
        for rn in routed:
            net, pts = by_name[rn.name]
            # A net whose routed path crosses the IC — spans it side-to-side or
            # cuts through the body — becomes net labels (KiCad merges
            # same-named labels) instead of a wire; everything else is wired as
            # routed. Applies even to a net that doesn't touch the IC: a wire
            # through the chip is an eyesore regardless of whose net it is.
            if _net_crosses_ic(rn, ic_rects):
                _relabel_spanning(sch, net, pts, routed_src.get(net.name, -1),
                                  aliases.get(net.name, net.name),
                                  label_specs, issues)
            else:
                committed.append(rn)
        _commit_wires(sch, committed, issues)
        issues.append(
            f"router: {count_crossings(committed)} crossing(s), "
            f"{count_overlaps(committed)} foreign overlap(s)"
        )
    # wiring == "labels": no wires emitted — connectivity is label-borne.

    for net in netlist.nets:
        if net.is_power and _power_lib_id(net.name) is None:
            issues.append(
                f"no standard power symbol for rail {net.name!r}; labels only"
            )
    if label_errors:
        raise PlacerError(label_errors)
    return label_specs


def _place_no_connects(
    sch: ksa.Schematic,
    netlist: Netlist,
    placed_refs: dict[str, tuple[float, float]],
    issues: list[str],
) -> None:
    """Emit a `(no_connect)` X marker at every pin the netlist flags as
    intentionally unconnected, so KiCad ERC stays satisfied — a bare pin
    would otherwise read as an accidental dangler."""
    for part in netlist.parts:
        if part.refdes not in placed_refs:
            continue
        comp = sch.components.get(part.refdes)
        if comp is None:
            continue
        for pin in part.no_connect_pins:
            xy = _pin_xy(comp, pin)
            if xy is None:
                issues.append(
                    f"no-connect pin {pin!r} not found on {part.refdes}"
                )
                continue
            try:
                sch.no_connects.add(position=xy)
            except Exception as e:
                issues.append(f"no-connect at {part.refdes}.{pin}: {e}")


def _hide_gnd_labels(sch_text: str, netlist: Netlist) -> str:
    """Hide the redundant "GND" value text on `power:GND` symbols — but only
    when GND is the design's *sole* ground.

    A lone ground symbol's graphic is universal, so its "GND" text carries
    no information and only collides with nearby references in a dense
    layout. But once a design has several distinct grounds (GND alongside
    AGND / DGND / PGND / earth …), the "GND" label *does* identify that net,
    so it must stay — and then every ground keeps its label.

    kicad-sch-api does not cleanly hide a power symbol's value field, so the
    `(hide yes)` flag is stamped onto the serialized text. Rail flags (+3V3,
    +5V, …) always keep their text — those are never redundant.
    """
    grounds = sum(
        1 for net in netlist.nets
        if _power_lib_id(net.name) in _GND_LIB_IDS
    )
    if grounds != 1:
        return sch_text
    return sch_text.replace(
        '(property "Value" "GND"',
        '(property "Value" "GND"\n\t\t\t(hide yes)',
    )


def _horizontal_field_angle(rotation: float) -> int:
    """The field `(at …)` angle that renders text upright and horizontal on a
    symbol placed at `rotation`.

    KiCad *adds* the symbol's rotation to the field's stored angle, so the
    field must store the complement: a 90° symbol needs a 270° field to land
    at an effective 0°. (Storing 90° would give an effective 180° — still
    horizontal, but flipped, so left-justified text then runs the wrong way.)
    """
    return (360 - round(rotation)) % 360


def _rewrite_property_at(
    text: str, head: str, start: int, x: float, y: float, angle: int
) -> tuple[str, int]:
    """Rewrite the `(at …)` of the property form `head` found at or after
    `start` to `(x, y, angle)`. Returns (text, index-of-head), or (text, -1)
    when `head` is absent. kicad-sch-api cannot reposition a field, so field
    moves are done by this text rewrite; `angle` comes from
    `_horizontal_field_angle` so the text renders horizontally.
    """
    i = text.find(head, start)
    if i == -1:
        return text, -1
    a = text.find("(at ", i)
    if a == -1:
        return text, i
    close = text.find(")", a)
    return text[:a] + f"(at {x:g} {y:g} {angle}" + text[close:], i


def _part_pin_extent(comp) -> tuple[float, float, float, float] | None:
    """(min_x, max_x, min_y, max_y) over a placed component's pin connection
    points, or None if no pin resolves."""
    xs: list[float] = []
    ys: list[float] = []
    for pin in comp.pins:
        xy = _pin_xy(comp, pin.number)
        if xy is not None:
            xs.append(xy[0])
            ys.append(xy[1])
    if not xs:
        return None
    return min(xs), max(xs), min(ys), max(ys)


def _reposition_ic_fields(
    sch_text: str, sch: ksa.Schematic, ic_ref: str
) -> str:
    """Move the main IC's Reference and Value into the clear band directly
    above its body. Many symbols (easyeda-imported ones especially) park the
    value at the bottom edge, where on a tall IC it overlaps the lowest pins;
    the space above the IC is clear — the rail rows sit to its sides.
    """
    ic = sch.components.get(ic_ref)
    ext = _part_pin_extent(ic) if ic is not None else None
    if ext is None:
        return sch_text
    min_x, max_x, top, _ = ext
    cx = _snap((min_x + max_x) / 2.0)
    angle = _horizontal_field_angle(ic.rotation)
    sch_text, ri = _rewrite_property_at(
        sch_text, f'(property "Reference" "{ic_ref}"', 0, cx,
        _snap(top - 5.08), angle)
    if ri != -1:
        # Value is the next property after Reference in the symbol block.
        sch_text, _ = _rewrite_property_at(
            sch_text, '(property "Value"', ri, cx, _snap(top - 2.54), angle)
    return sch_text


def _reposition_series_fields(
    sch_text: str, sch: ksa.Schematic, series_refs: frozenset[str]
) -> str:
    """Stack each series element's Reference and Value below its body.

    A series element (inductor, ferrite, series R) is rotated horizontal, so
    a long value — an inductor part number especially — would otherwise be
    drawn straight across the body and its pins. Below the body is clear
    (the connecting wires enter at the ends); below is used rather than
    above so a part near the page top still has somewhere to put them.

    `series_refs` is the set of series-element refdeses. `_place_and_measure`
    runs this on its provisional layout *before* the measurement pass so the
    stacked-below fields are reflected in `cell.h`; `_build` runs it again on
    the final layout. Both must agree, hence the shared routine.
    """
    for refdes in series_refs:
        comp = sch.components.get(refdes)
        ext = _part_pin_extent(comp) if comp is not None else None
        if ext is None:
            continue
        min_x, max_x, min_y, max_y = ext
        cx = _snap((min_x + max_x) / 2.0)
        cy = (min_y + max_y) / 2.0
        angle = _horizontal_field_angle(comp.rotation)
        sch_text, ri = _rewrite_property_at(
            sch_text, f'(property "Reference" "{refdes}"', 0, cx,
            _snap(cy + 2.54), angle)
        if ri != -1:
            sch_text, _ = _rewrite_property_at(
                sch_text, '(property "Value"', ri, cx, _snap(cy + 5.08),
                angle)
    return sch_text


def _build(
    netlist: Netlist, plan: LayoutPlan, title: str, wiring: str
) -> PlacerResult:
    """One full placement pass with the given wiring engine
    ("router" / "star" / "labels").

    Place (single IC → pin-anchored grammar, else measured columns), wire,
    stamp no-connects, serialize. Raises `PlacerError` on an IC add failure
    or a label that can't be anchored at a pin.
    """
    sch = ksa.create_schematic(title)
    issues: list[str] = []
    if len(plan.ics) == 1:
        placed_refs = _place_pin_anchored(sch, netlist, plan, issues)
    else:
        placed_refs = _place_columns(sch, netlist, issues)
    label_specs = _place_connectivity(
        sch, netlist, plan, placed_refs, issues, wiring=wiring
    )
    _place_no_connects(sch, netlist, placed_refs, issues)
    text = _hide_gnd_labels(sch_to_string(sch), netlist)
    if len(plan.ics) == 1:
        text = _reposition_ic_fields(text, sch, plan.ics[0])
    series_refs = frozenset(
        r for r, pc in plan.parts.items() if pc.role == Role.SERIES_ELEMENT
    )
    text = _reposition_series_fields(text, sch, series_refs)
    return PlacerResult(
        sch_text=text,
        issues=issues,
        placed_refs=placed_refs,
        label_specs=label_specs,
    )


def _topology_intact(netlist: Netlist, result: PlacerResult) -> bool:
    """True if KiCad's netlister sees `result`'s topology as the input's —
    the test `place(wiring="auto")` uses to decide whether to fall back. The
    import is function-local to break the netlist_to_sch ↔ structural_diff
    cycle. Returns True when kicad-cli is unavailable (cannot judge → trust)."""
    from pinflow_api.emit.structural_diff import (
        _export_topology, _netlist_topology,
    )
    exported = _export_topology(result.sch_text)
    return exported is None or exported == _netlist_topology(netlist)


def place(
    netlist: Netlist, *, title: str = "Subcircuit", wiring: str = "auto",
) -> PlacerResult:
    """Lay out the netlist and emit a full `(kicad_sch ...)` document.

    `wiring`:
      - "auto" (default) — wire with the crossing-minimising router; if its
        output fails the connectivity check, fall back to label-only wiring
        (a net label at every pin, no wires — short-safe by construction).
        The router is newer and can corrupt topology on some netlists, so
        the fallback guarantees a connectivity-correct schematic is always
        returned.
      - "router" / "star" / "labels" — force one engine, no fallback (for
        diagnostics). Note "star" is *not* short-safe — its hub→part fans
        can run collinearly and merge nets; "labels" is the safe fallback.

    Raises `PlacerError` on self-validation failure, an IC add failure, or a
    label that can't be anchored at a pin.
    """
    errors = netlist.validate_self()
    if errors:
        raise PlacerError(errors)
    plan = classify(netlist)

    if wiring in ("router", "star", "labels"):
        return _build(netlist, plan, title, wiring)

    routed = _build(netlist, plan, title, "router")
    if _topology_intact(netlist, routed):
        return routed
    fallback = _build(netlist, plan, title, "labels")
    fallback.issues.append(
        "router output failed the connectivity check — fell back to "
        "label-only wiring"
    )
    return fallback


# Parts-bin packing geometry is the BIN_* constants near the top of the file.


def place_parts(netlist: Netlist, *, title: str = "Subcircuit") -> PlacerResult:
    """Parts-only placement: components packed side by side, NO connectivity.

    A reviewable, debuggable *parts bin* — the right symbols with the right
    values packed into a grid without overlapping, nothing wired. No longer
    on the agent path (the generate flow now places *and wires* via `place()`);
    kept as a utility for the parts-bin render/test harness
    (`scripts/test_place_parts.py`) and for inspecting symbol resolution +
    bbox measurement in isolation. No power symbols either — those are net
    anchors, i.e. connectivity.

    Layout is a **square grid**: `ceil(sqrt(n))` columns filled row-major in
    the deterministic order below. Cell sizes come from `_place_and_measure`'s
    measured boxes; variable column widths / row heights keep the grid lines
    aligned without a single large IC inflating every passive's spacing. Same
    netlist → same sheet.

    The labeled rectangle around the block is NOT drawn here — it is added at
    merge time by `emit.layout.draw_frame`, independent of the placer.

    Raises `PlacerError` on self-validation failure or if an IC fails to add
    (a netlist↔lib_id mismatch the model must fix); non-IC add failures are
    recorded in `issues` and skipped so a partial bin is still inspectable.
    """
    errors = netlist.validate_self()
    if errors:
        raise PlacerError(errors)

    sch = ksa.create_schematic(title)
    issues: list[str] = []
    placed_refs: dict[str, tuple[float, float]] = {}

    ics, connectors, other_by_host = _bucket_parts(netlist)
    others: list[NetlistPart] = []
    for cohort in other_by_host.values():
        others.extend(cohort)

    # ICs first, then loose passives, then connectors — each natural-sorted.
    ordered: list[NetlistPart] = (
        sorted(ics, key=lambda p: _natural_key(p.refdes))
        + sorted(others, key=lambda p: _natural_key(p.refdes))
        + sorted(connectors, key=lambda p: _natural_key(p.refdes))
    )

    # --- Pass 1: place provisionally + measure every part ----------------
    cells = _place_and_measure(sch, ordered, issues)
    if not cells:
        return PlacerResult(
            sch_text=sch_to_string(sch), issues=issues,
            placed_refs={}, label_specs=[],
        )

    # --- Pass 2: lay the measured cells out in a square grid -------------
    n = len(cells)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    col_w = [0.0] * cols
    row_h = [0.0] * rows
    for idx, cell in enumerate(cells):
        r, c = divmod(idx, cols)
        col_w[c] = max(col_w[c], cell.w)
        row_h[r] = max(row_h[r], cell.h)

    col_x = [MARGIN]
    for c in range(cols):
        col_x.append(col_x[-1] + col_w[c] + BIN_PAD)
    row_y = [MARGIN]
    for r in range(rows):
        row_y.append(row_y[-1] + row_h[r] + BIN_ROW_PAD)

    for idx, cell in enumerate(cells):
        r, c = divmod(idx, cols)
        # Centre the part within its (variable-size) grid cell.
        target_x = col_x[c] + (col_w[c] - cell.w) / 2
        target_y = row_y[r] + (row_h[r] - cell.h) / 2
        dx = _snap(target_x - cell.min_x)
        dy = _snap(target_y - cell.min_y)
        for comp in cell.members:
            comp.translate(dx, dy)
        placed_refs[cell.refdes] = (
            _snap(cell.min_x + dx),
            _snap(cell.min_y + dy),
        )

    sch_text = sch_to_string(sch)
    return PlacerResult(
        sch_text=sch_text,
        issues=issues,
        placed_refs=placed_refs,
        label_specs=[],
    )
