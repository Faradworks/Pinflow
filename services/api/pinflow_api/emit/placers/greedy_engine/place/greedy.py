"""Greedy bbox-aware placer.

Start at the IC with the most pins. For each of its pins, walk outward
in the pin's natural direction and place whatever connects on the other
end of that net. Multi-tap nets fan out in a row along the source pin's
natural direction; each tap's connecting pin lands on the implicit rail
extending from the source pin's tip. GND flags drop below their pin;
+V flags drop above. Equidistant visible whitespace between bboxes.

No topology classification, no role assignment.

Algorithm:

    1. find IC with most pins; place at snapped origin, rotation chosen
       so power pins face up and ground pins face down.

    2. BFS queue, seeded with every pin of the main IC.

    3. for each (from_ref, from_pin) popped:
         - GND net → drop a GND flag at pin_tip + MIN_STUB along natural_dir
         - power rail with no un-placed taps → drop +V flag
         - else → fan out unplaced taps along natural_dir:
             * for each tap, choose_rotation(GND-down, power-up)
             * place such that:
                 - tap's bbox.opposite edge sits BBOX_GAP from cursor
                 - tap's connecting pin sits on the rail (perp-axis fixed
                   at source.pin.tip's perp coord)
             * snap to grid
             * push tap's other pins onto the queue
             * advance cursor by tap.bbox.along_offset

The "symmetry trick": every spacing decision uses the bbox's half-extent
from the bbox CENTER (not the symbol origin). Pin-extent-only bboxes are
padded to GRID so degenerate cases (Device:C with width=0) get sensible
visible spacing.
"""

from __future__ import annotations

from collections import deque

from kicad_sch_api.core.component_bounds import SymbolBoundingBoxCalculator
from kicad_sch_api.library.cache import get_symbol_cache

from pinflow_api.emit.placers.greedy_engine.parse import CircuitGraph
from pinflow_api.emit.placers.greedy_engine.place.rotation import choose_rotation
from pinflow_api.emit.placers.greedy_engine.place.types import PlacedComponent, PlacedPowerFlag, Placement
from pinflow_api.emit.placers.greedy_engine.symbols import BBox, PlacedSymbol, Symbol, SymbolLibrary
from pinflow_api.emit.placers.greedy_engine.symbols.symbol import _rotate_then_yinv

GRID = 1.27
MIN_STUB = 2 * GRID              # 2.54 mm
BBOX_GAP = 5 * GRID              # 6.35 mm: visible whitespace between adjacent bboxes
BBOX_PAD = GRID                  # 1.27 mm: minimum half-extent for degenerate bboxes
# Half-extent of a refdes / value label around its anchor. Covers up to
# ~15-char strings ("D_Schottky_mini", "TPS61088RHLR") at KiCad's default
# 1.27mm font. Conservative square so the same value works for both
# horizontal and 90°-rotated labels. Used to grow _world_bbox so collision
# detection accounts for the visible labels, not just body+pins.
LABEL_HALF = 5 * GRID            # 6.35 mm: text rectangle half-extent

# Decoupling-cap island layout: caps peeled off the rail and placed as
# standalone +V-flag → cap → GND-flag stacks in a horizontal band above the
# source IC. Connected to the rest of the circuit only via net-name matching;
# the emitter pairs each cap pin with its nearest flag.
# Band is anchored to the source IC's body TOP, not its pin tip — a tall IC
# whose body graphics extend past its pins still gets clear vertical headroom.
BAND_GAP_ABOVE_BODY = 8 * GRID  # 10.16 mm above source IC body top
CAP_PITCH           = 8 * GRID  # 10.16 mm between adjacent caps in the band

# Signal-tributary row layout: when 2+ main-IC pins on the same side (right /
# left) each have signal-net taps, those taps are placed in a horizontal row
# BELOW the IC body. Lowest source pin (largest screen Y) gets the closest
# lane to the IC; each higher pin's tap is placed progressively further
# outward. The wire from each source pin runs horizontally at its own Y and
# drops vertically to its tap — no wire crosses another tap's body because
# every body sits at row_y, well below all source pin Ys.
ROW_GAP_BELOW_BODY = 8 * GRID    # 10.16 mm: row of taps sits this far below IC body
LANE_GAP           = 4 * GRID    # 5.08 mm: horizontal gap between adjacent lanes
LANE_START_GAP     = 4 * GRID    # 5.08 mm: gap between IC body side and first lane
CLUSTER_WINDOW     = 8 * GRID    # 10.16 mm: max Y span of the bottom-pin cluster

# Default origin in mm (snapped to grid). 120*GRID × 80*GRID puts the main IC
# near the upper-middle of an A4 landscape sheet (usable area ~10..280 × 10..200).
DEFAULT_ORIGIN = (120 * GRID, 80 * GRID)

# Screen-space unit vectors. +Y is down on screen.
DIR = {
    "right": (1, 0),
    "left":  (-1, 0),
    "up":    (0, -1),
    "down":  (0, 1),
}
OPPOSITE = {"right": "left", "left": "right", "up": "down", "down": "up"}


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class GreedyPlacer:
    """Topology-agnostic placer. See module docstring."""

    def __init__(self, lib: SymbolLibrary):
        self.lib = lib

    def place(self, cg: CircuitGraph,
              origin: tuple[float, float] = DEFAULT_ORIGIN) -> Placement:
        # 1. Main IC
        main = _largest_ic(cg)
        main_sym = self.lib.get(main.lib_id)
        main_rot = choose_rotation(
            main_sym,
            set(cg.gnd_pins(main.ref)),
            {p for p, _ in cg.power_pins(main.ref)},
        )

        placed: dict[str, PlacedSymbol] = {}
        placed[main.ref] = main_sym.place(_snap(origin[0]), _snap(origin[1]), main_rot)

        flags: list[PlacedPowerFlag] = []
        queue: deque[tuple[str, str]] = deque()
        routing_hints: dict[str, float] = {}

        # 2. Pre-place signal tributaries from main IC pins as horizontal rows
        #    below the IC. Done BEFORE BFS seeding so that BFS finds these
        #    nets' taps already placed (it will then just queue downstream
        #    pins like GND flags for normal handling).
        row_placements, row_hints = self._place_signal_tributary_rows(
            main, placed[main.ref], cg, placed,
        )
        for ref, conn_pin, ps in row_placements:
            placed[ref] = ps
            for op_pin, _ in sorted(cg.pins_of(ref)):
                if op_pin != conn_pin:
                    queue.append((ref, op_pin))
        routing_hints.update(row_hints)

        # 3. BFS seed: every main-IC pin. Signal pins handled above will
        #    have empty unplaced lists and become no-ops.
        for pin_num, _ in sorted(cg.pins_of(main.ref)):
            queue.append((main.ref, pin_num))

        # 3. BFS
        while queue:
            from_ref, from_pin = queue.popleft()
            from_placed = placed[from_ref]
            net = cg.net_for_pin(from_ref, from_pin)
            if net is None:
                continue
            from_comp = cg.components[from_ref]
            pin_tip = from_placed.pin(from_pin)
            natural_dir = _natural_direction(from_comp, from_placed.rotation,
                                             from_pin, self.lib)

            if net.is_ground:
                # Offset by MIN_STUB so there's a visible wire between the pin
                # and the GND symbol — butt-connection puts the GND triangle
                # inside the component's bbox. The emitter splits paired
                # pin/flag nets so each GND pin routes only to its own flag
                # (avoiding the rail-through-other-net-pin problem).
                flags.append(_make_offset_flag("GND", pin_tip, natural_dir))
                continue

            unplaced = _unplaced_taps(cg, net.name, placed, from_ref)

            # Partition power-net taps: decoupling caps go to a standalone
            # island; everything else stays inline on the rail.
            if net.is_power:
                caps: list[tuple[str, str]] = []
                others: list[tuple[str, str]] = []
                for tap in unplaced:
                    if _decoupling_cap(cg, tap[0]) is not None:
                        caps.append(tap)
                    else:
                        others.append(tap)
            else:
                caps, others = [], unplaced

            # Decoupling-cap island: cap pins are NOT queued (both ends are
            # terminated by the island's own flags).
            if caps:
                island_placements, island_flags = self._place_island(
                    from_placed, pin_tip, caps, cg, placed,
                )
                for ref, _, ps in island_placements:
                    placed[ref] = ps
                flags.extend(island_flags)

            # Fan out non-cap taps inline along natural_dir.
            # For power-rail nets, drop taps by MIN_STUB perpendicular to the
            # rail (so each tap's connecting pin is OFF the rail by one stub).
            # This forces the emitter into rail+drops+junctions routing instead
            # of a single span with mid-wire pin tips (which ERC flags as
            # unconnected since KiCad requires pin tips at wire endpoints or
            # junctions).
            if others:
                perp_offset = MIN_STUB if net.is_power else 0.0
                new_placements, end_cursor = self._fan_out(
                    pin_tip, natural_dir, others, cg, perp_offset, placed,
                )
                for other_ref, conn_pin, ps in new_placements:
                    placed[other_ref] = ps
                    for op_pin, _ in sorted(cg.pins_of(other_ref)):
                        if op_pin != conn_pin:
                            queue.append((other_ref, op_pin))

                # End-of-rail +V flag, ON the rail (at perp_coord), past the
                # last tap's along-axis edge.
                if net.is_power and new_placements:
                    flags.append(_make_rail_end_flag(
                        net.name, pin_tip, natural_dir, end_cursor,
                    ))

            # Source pin's own +V flag when the rail has no inline taps —
            # covers both the "no unplaced at all" case and the "caps absorbed
            # everything" case. Without this the source pin would be electrically
            # connected (via net name) but visually dangling.
            if net.is_power and not others:
                flags.append(_make_offset_flag(net.name, pin_tip, natural_dir))

        return _to_placement(placed, flags, cg, routing_hints)

    # -----------------------------------------------------------------------
    # Fan-out — the core placement primitive
    # -----------------------------------------------------------------------

    def _fan_out(self, source_pin_tip, natural_dir: str,
                 unplaced: list[tuple[str, str]],
                 cg: CircuitGraph,
                 perp_offset: float = 0.0,
                 placed: dict[str, PlacedSymbol] | None = None,
                 ) -> tuple[list[tuple[str, str, PlacedSymbol]], float]:
        """Place each tap along natural_dir. The 'rail' is the axis line
        extending from source_pin_tip in natural_dir; each tap's connecting
        pin sits PERPENDICULAR-OFFSET from this rail by `perp_offset`.

        For natural_dir horizontal: perp axis = y, taps below the rail (+y).
        For natural_dir vertical:   perp axis = x, taps right of the rail (+x).

        If a tap's tentative position would overlap any already-placed bbox,
        the tap is shifted further along natural_dir (one GRID at a time)
        until clear. This preserves equidistant spacing for non-conflicting
        taps but pushes conflicting ones past whatever's in the way.

        Returns (placements, end_cursor) where end_cursor is the along-axis
        position of the last tap's far edge — useful for placing an
        end-of-rail flag past the fan-out.
        """
        d = DIR[natural_dir]
        axis_idx = 0 if d[0] != 0 else 1
        perp_idx = 1 - axis_idx
        axis_sign = d[axis_idx]
        perp_sign = 1.0  # body below rail (horizontal) / right of rail (vertical)

        tip_xy = (source_pin_tip.x, source_pin_tip.y)
        cursor = tip_xy[axis_idx]
        rail_perp = tip_xy[perp_idx]
        connect_perp_target = rail_perp + perp_sign * perp_offset

        results: list[tuple[str, str, PlacedSymbol]] = []
        existing = list((placed or {}).values())

        for other_ref, other_pin in unplaced:
            comp = cg.components[other_ref]
            sym = self.lib.get(comp.lib_id)
            other_gnd = set(cg.gnd_pins(other_ref))
            other_pwr = {p for p, _ in cg.power_pins(other_ref)}
            # Connect-pin rotation rule: face the tap's connecting pin back
            # toward the source. Applies ONLY when:
            #   (a) the component has a power or GND pin (so the non-connect
            #       pin terminates at a local flag, not a shared signal rail
            #       — see L3 in level3 for the failure mode otherwise), AND
            #   (b) the fan-out chain itself is vertical.
            # For a horizontal chain we deliberately skip the rule so the
            # component stays vertical (+V-up / GND-down) perpendicular to
            # the rail. Otherwise the component's two pins are colinear with
            # the rail and the non-connect pin's wire overlaps the rail wire
            # (e.g., D1.K sitting on the SW rail).
            apply_connect = (
                (other_gnd or other_pwr) and natural_dir in ("up", "down")
            )
            connect_pin = other_pin if apply_connect else None
            connect_dir = OPPOSITE[natural_dir] if apply_connect else None
            rot = choose_rotation(
                sym, other_gnd, other_pwr,
                connect_pin=connect_pin,
                connect_dir=connect_dir,
            )
            local = sym.place(0.0, 0.0, rot)
            # Use the label-aware world bbox (relative to origin since we
            # placed at 0,0) so along-axis spacing accounts for refdes/value
            # labels, not just the pin-extent skeleton.
            opp_offset, along_offset = _bbox_offsets(_world_bbox(local), natural_dir)

            # Along-axis: opposite-facing edge sits BBOX_GAP past cursor.
            target_opp_axis = cursor + axis_sign * BBOX_GAP
            body_axis = target_opp_axis - opp_offset

            # Perp-axis: connecting pin lands at connect_perp_target.
            connect_local = local.pin(other_pin)
            connect_perp_local = (connect_local.x, connect_local.y)[perp_idx]
            body_perp = connect_perp_target - connect_perp_local

            body_axis = _snap(body_axis)
            body_perp = _snap(body_perp)

            # Overlap resolution: if the tentative placement collides with any
            # already-placed bbox, shift along natural_dir by GRID until clear.
            for _attempt in range(60):  # cap iterations to avoid infinite loop
                if axis_idx == 0:
                    body_x, body_y = body_axis, body_perp
                else:
                    body_x, body_y = body_perp, body_axis
                candidate = sym.place(body_x, body_y, rot)
                if not _bbox_collides(candidate, existing):
                    break
                body_axis += axis_sign * GRID

            new_placed = sym.place(body_x, body_y, rot)
            results.append((other_ref, other_pin, new_placed))
            existing.append(new_placed)
            cursor = body_axis + along_offset

        return results, cursor

    # -----------------------------------------------------------------------
    # Signal-tributary row
    # -----------------------------------------------------------------------

    def _place_signal_tributary_rows(self, main, main_placed: PlacedSymbol,
                                     cg: CircuitGraph,
                                     placed: dict[str, PlacedSymbol],
                                     ) -> tuple[list[tuple[str, str, PlacedSymbol]],
                                                dict[str, float]]:
        """Place signal-net taps from main-IC pins as horizontal rows below
        the IC body, sorted bottom-up so the lowest source pin gets the closest
        lane. See ROW_GAP_BELOW_BODY / LANE_GAP for spacing constants.

        Eligibility: per side (right / left), a pin contributes iff its net is
        a signal (not power, not GND, not unconnected) and the net has at
        least one unplaced "leaf" tap — a tap whose own connections are
        signal+GND only (no power pin, which would otherwise tie it to the
        power-rail handler). Apply only when 2+ eligible pins on a side; a
        single pin per side stays with the original BFS+fan-out path.

        For each placed tap, record a routing hint pinning the net's rail Y
        to the source pin Y. Without it, the writer's histogram would route
        multi-tap nets (e.g. COMP→{C21, R9}) along the row Y rather than the
        source pin Y, producing rails that crowd the tap bodies.
        """
        results: list[tuple[str, str, PlacedSymbol]] = []
        hints: dict[str, float] = {}

        main_bbox = _world_bbox(main_placed)
        row_y = _snap(main_bbox.max_y + ROW_GAP_BELOW_BODY)

        by_side: dict[str, list] = {"right": [], "left": []}
        for pin_num, _ in sorted(cg.pins_of(main.ref)):
            net = cg.net_for_pin(main.ref, pin_num)
            if net is None or net.is_ground or net.is_power:
                continue
            if net.name.startswith("unconnected-"):
                continue
            unplaced = _unplaced_taps(cg, net.name, placed, main.ref)
            eligible_taps = [
                (ref, pin) for ref, pin in unplaced
                if _row_safe_tap(cg, ref, net.name, main.ref)
            ]
            if not eligible_taps:
                continue
            side = _natural_direction(
                cg.components[main.ref], main_placed.rotation, pin_num, self.lib,
            )
            if side not in by_side:
                continue
            by_side[side].append((pin_num, net, eligible_taps))

        for side, groups in by_side.items():
            if len(groups) < 2:
                continue
            # Sort: bottom-most source pin first (largest screen Y).
            groups.sort(key=lambda g: -main_placed.pin(g[0]).y)
            # Restrict to the bottom cluster: pins whose Y is within a small
            # window of the lowest pin's Y. Pins higher up the side (e.g.,
            # FB sitting near the middle of a 22-pin IC, with COMP/VCC/ILIM/SS
            # all clustered near the bottom) would force a very long rail
            # at their Y that crosses the IC body. Leave those to the
            # downstream BFS+fan-out path.
            bottom_y = main_placed.pin(groups[0][0]).y
            groups = [g for g in groups if bottom_y - main_placed.pin(g[0]).y <= CLUSTER_WINDOW]
            if len(groups) < 2:
                continue

            axis_sign = 1.0 if side == "right" else -1.0
            if side == "right":
                lane_x = _snap(main_bbox.max_x + LANE_START_GAP)
            else:
                lane_x = _snap(main_bbox.min_x - LANE_START_GAP)

            existing = list(placed.values())
            for pin_num, net, eligible_taps in groups:
                source_tip = main_placed.pin(pin_num)
                # Skip any tap that another lane already placed (shared component).
                taps_to_place = [(r, p) for r, p in eligible_taps if r not in placed]
                if not taps_to_place:
                    continue

                hints[net.name] = source_tip.y

                for tap_ref, tap_pin in taps_to_place:
                    comp = cg.components[tap_ref]
                    sym = self.lib.get(comp.lib_id)
                    rot = choose_rotation(
                        sym,
                        set(cg.gnd_pins(tap_ref)),
                        {p for p, _ in cg.power_pins(tap_ref)},
                        connect_pin=tap_pin,
                        connect_dir="up",
                    )
                    local = sym.place(0.0, 0.0, rot)
                    conn_local = local.pin(tap_pin)
                    body_x = _snap(lane_x - conn_local.x)
                    body_y = _snap(row_y - conn_local.y)

                    for _attempt in range(80):
                        cand = sym.place(body_x, body_y, rot)
                        if not _bbox_collides(cand, existing):
                            break
                        body_x = _snap(body_x + axis_sign * GRID)

                    tap_placed = sym.place(body_x, body_y, rot)
                    placed[tap_ref] = tap_placed
                    existing.append(tap_placed)
                    results.append((tap_ref, tap_pin, tap_placed))

                    tap_bbox = _world_bbox(tap_placed)
                    if side == "right":
                        lane_x = _snap(tap_bbox.max_x + LANE_GAP)
                    else:
                        lane_x = _snap(tap_bbox.min_x - LANE_GAP)

        return results, hints

    # -----------------------------------------------------------------------
    # Decoupling-cap island
    # -----------------------------------------------------------------------

    def _place_island(self, source_placed: PlacedSymbol,
                      source_pin_tip,
                      caps: list[tuple[str, str]],
                      cg: CircuitGraph,
                      placed: dict[str, PlacedSymbol],
                      ) -> tuple[list[tuple[str, str, PlacedSymbol]],
                                 list[PlacedPowerFlag]]:
        """Place decoupling caps as standalone islands in a horizontal band
        above the source IC's body. Each cap is vertical (rot=0) with its own
        paired +V flag (above) and GND flag (below) — connected only via
        net-name matching.

        Returns (placements, flags). Cap pins must NOT be queued for further
        BFS — both ends are terminated by the island's flags.
        """
        results: list[tuple[str, str, PlacedSymbol]] = []
        new_flags: list[PlacedPowerFlag] = []
        existing = list(placed.values())

        source_bbox = _world_bbox(source_placed)
        band_y = _snap(source_bbox.min_y - BAND_GAP_ABOVE_BODY)
        band_x_anchor = _snap(source_pin_tip.x)

        for i, (cap_ref, _bfs_pin) in enumerate(caps):
            pair = _decoupling_cap(cg, cap_ref)
            if pair is None:
                continue
            pwr_pin, gnd_pin = pair
            pwr_net = cg.net_for_pin(cap_ref, pwr_pin)
            pwr_name = pwr_net.name if pwr_net else "+V"
            sym = self.lib.get(cg.components[cap_ref].lib_id)

            cap_x = _snap(band_x_anchor + i * CAP_PITCH)
            cap_y = band_y

            for _attempt in range(60):
                candidate = sym.place(cap_x, cap_y, 0.0)
                if not _bbox_collides(candidate, existing):
                    break
                cap_x = _snap(cap_x + GRID)

            placed_sym = sym.place(cap_x, cap_y, 0.0)
            results.append((cap_ref, pwr_pin, placed_sym))
            existing.append(placed_sym)

            pwr_tip = placed_sym.pin(pwr_pin)
            gnd_tip = placed_sym.pin(gnd_pin)

            if pwr_tip.y <= gnd_tip.y:
                new_flags.append(_make_offset_flag(pwr_name, pwr_tip, "up"))
                new_flags.append(_make_offset_flag("GND", gnd_tip, "down"))
            else:
                new_flags.append(_make_offset_flag(pwr_name, pwr_tip, "down"))
                new_flags.append(_make_offset_flag("GND", gnd_tip, "up"))

        return results, new_flags


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _world_bbox(ps: PlacedSymbol) -> BBox:
    """Return the body+pins bbox in world (schematic) coords.

    Uses ksa's SymbolBoundingBoxCalculator (which walks the symbol's
    rectangle/polyline/arc/circle graphics in addition to pin extents).
    This is the right "no-go zone" for overlap detection — our own
    Symbol.bbox is pin-extent-only and misses degenerate bodies (e.g.,
    Device:C's plates extend ±2.16 in X, but pin-extent says W=0).

    Transforms lib coords (math, +Y up) → screen via the same rotate-then-
    Y-invert transform our Symbol layer uses, so the bbox aligns with our
    placed pin positions.
    """
    sym = get_symbol_cache().get_symbol(ps.lib_id)
    if sym is None:
        # Symbol missing from ksa cache (e.g., unusual lib_id). Fall back to
        # padded pin-extent bbox.
        b = ps.bbox
        return BBox(b.min_x - BBOX_PAD, b.min_y - BBOX_PAD,
                    b.max_x + BBOX_PAD, b.max_y + BBOX_PAD)

    lib_bb = SymbolBoundingBoxCalculator.calculate_bounding_box(
        sym, include_properties=False,
    )
    corners = [
        (lib_bb.min_x, lib_bb.min_y),
        (lib_bb.max_x, lib_bb.min_y),
        (lib_bb.max_x, lib_bb.max_y),
        (lib_bb.min_x, lib_bb.max_y),
    ]
    transformed = [_rotate_then_yinv(x, y, ps.rotation) for x, y in corners]
    xs = [ps.origin_x + dx for dx, _ in transformed]
    ys = [ps.origin_y + dy for _, dy in transformed]

    # Expand the bbox to include visible Reference and Value labels. ksa's
    # include_properties=True over-estimates by counting hidden properties
    # (Datasheet/Description) — we just want the labels users see.
    pp = getattr(sym, "property_positions", None) or {}
    for prop_name in ("Reference", "Value"):
        if prop_name not in pp:
            continue
        px, py, _angle = pp[prop_name]
        # Rotate the anchor with the symbol, but draw an axis-aligned text
        # rectangle around the rotated anchor (KiCad keeps labels readable
        # in screen orientation; we conservatively approximate as a square
        # to handle vertical/horizontal labels uniformly).
        adx, ady = _rotate_then_yinv(px, py, ps.rotation)
        ax = ps.origin_x + adx
        ay = ps.origin_y + ady
        for dx in (-LABEL_HALF, LABEL_HALF):
            for dy in (-LABEL_HALF, LABEL_HALF):
                xs.append(ax + dx)
                ys.append(ay + dy)

    return BBox(min_x=min(xs), min_y=min(ys), max_x=max(xs), max_y=max(ys))


def _bbox_collides(candidate: PlacedSymbol,
                   existing: list[PlacedSymbol],
                   margin: float = 0.0) -> bool:
    """True if candidate's body+pins bbox overlaps any in existing.

    Uses ksa's body-graphic-aware bbox via _world_bbox. Margin=0 means
    bodies must not overlap; bodies touching edge-to-edge is the closest
    they can sit.
    """
    cand_b = _world_bbox(candidate)
    for other in existing:
        if cand_b.overlaps(_world_bbox(other), margin=margin):
            return True
    return False


def _bbox_offsets(bbox: BBox, natural_dir: str) -> tuple[float, float]:
    """Local-frame offsets (body at origin) for the bbox edges:
        opp_offset:    edge facing OPPOSITE direction (back toward source)
        along_offset:  edge facing natural_dir (forward along the rail)

    Degenerate bboxes (e.g., Device:C with width=0) are padded so the
    effective half-extent is at least BBOX_PAD (1 GRID). This gives
    visible 2-pin parts a sensible 2.54 mm minimum width.
    """
    cx, cy = bbox.center
    half_right = max(bbox.max_x - cx, BBOX_PAD)
    half_left  = max(cx - bbox.min_x, BBOX_PAD)
    half_down  = max(bbox.max_y - cy, BBOX_PAD)
    half_up    = max(cy - bbox.min_y, BBOX_PAD)

    if natural_dir == "right":
        return (-half_left, half_right)
    if natural_dir == "left":
        return (half_right, -half_left)
    if natural_dir == "down":
        return (-half_up, half_down)
    if natural_dir == "up":
        return (half_down, -half_up)
    raise ValueError(f"unknown direction: {natural_dir!r}")


def _natural_direction(comp, component_rotation: float, pin_num: str,
                       lib: SymbolLibrary) -> str:
    """Direction the pin extends from the body on screen.

    KiCad lib pin angle = direction from TIP back to BODY (the stem
    direction). Extension direction in
    lib coords = (angle + 180) % 360. After component rotation R (CCW
    in lib): + R. After Y-invert for screen: lib north(+Y) → screen up,
    lib south(-Y) → screen down.
    """
    sym = lib.get(comp.lib_id)
    for tup in sym.pins_lib():
        if tup[0] == pin_num:
            pin_angle = tup[5]
            break
    else:
        raise KeyError(f"{comp.ref} ({comp.lib_id}): no pin {pin_num!r}")

    ext_lib = (pin_angle + 180 + component_rotation) % 360
    ext_lib = int(round(ext_lib / 90)) * 90 % 360
    return {0: "right", 90: "up", 180: "left", 270: "down"}[ext_lib]


# ---------------------------------------------------------------------------
# Power flag builders
# ---------------------------------------------------------------------------

def _make_butt_flag(net: str, pin_tip) -> PlacedPowerFlag:
    """Place a #PWR symbol AT the pin tip (butt-connected, no wire needed).

    Used for GND flags. KiCad's GND symbol (rotation=0) has its connection
    point at its origin and the triangle below — placing it at the pin
    coordinate visually caps the pin without overlap. The emitter detects
    butt connections and skips wire routing for these.
    """
    return PlacedPowerFlag(
        net=net, x=_snap(pin_tip.x), y=_snap(pin_tip.y), rotation=0.0,
    )


def _make_offset_flag(net: str, pin_tip, natural_dir: str) -> PlacedPowerFlag:
    """Place a #PWR symbol MIN_STUB past the pin tip in natural_dir."""
    dx, dy = DIR[natural_dir]
    return PlacedPowerFlag(
        net=net,
        x=_snap(pin_tip.x + dx * MIN_STUB),
        y=_snap(pin_tip.y + dy * MIN_STUB),
        rotation=0.0,
    )


def _make_rail_end_flag(net: str, source_pin_tip, natural_dir: str,
                        end_cursor: float) -> PlacedPowerFlag:
    """Place a +V flag at the far end of a horizontal/vertical rail.

    Positioned ON the rail (perpendicular coord = source's perpendicular),
    past the last tap by MIN_STUB along natural_dir. This lets the rail
    wire visibly terminate at the flag without crossing any taps.
    """
    d = DIR[natural_dir]
    axis_idx = 0 if d[0] != 0 else 1
    perp_idx = 1 - axis_idx
    axis_sign = d[axis_idx]

    flag_axis = end_cursor + axis_sign * MIN_STUB
    flag_perp = (source_pin_tip.x, source_pin_tip.y)[perp_idx]

    if axis_idx == 0:
        return PlacedPowerFlag(net=net, x=_snap(flag_axis), y=_snap(flag_perp), rotation=0.0)
    return PlacedPowerFlag(net=net, x=_snap(flag_perp), y=_snap(flag_axis), rotation=0.0)


# ---------------------------------------------------------------------------
# CircuitGraph helpers
# ---------------------------------------------------------------------------

def _largest_ic(cg: CircuitGraph):
    """The component with the most total pins. Ties broken by
    (power+gnd pin count, then alphabetical ref).
    """
    if not cg.components:
        raise ValueError("CircuitGraph has no components")

    def score(ref: str) -> tuple[int, int, str]:
        pin_count = len(cg.pins_of(ref))
        priority = len(cg.gnd_pins(ref)) + len(cg.power_pins(ref))
        # negate ref so alphabetical is highest score
        return (pin_count, priority, ref)

    refs = sorted(cg.components.keys(), key=score, reverse=True)
    return cg.components[refs[0]]


def _row_safe_tap(cg: CircuitGraph, tap_ref: str, source_net: str,
                  main_ref: str) -> bool:
    """True iff `tap_ref` can be safely placed in the tributary row.

    Excludes taps that would conflict with other handlers:
      - Taps with any pin on a POWER net → owned by the power-island /
        rail-fan-out path; placing them in the row would split a power
        network awkwardly.
      - Taps whose non-source pin connects to ANOTHER signal net that also
        touches the main IC (e.g., U3 left-side R3 connects FSW↔SW, C8
        connects BOOT↔SW). Dropping them into the row forces two different
        rails to share an X column at the tap, shorting the nets together.

    GND and power nets on the non-source side are fine (the BFS handles
    them with flags / islands).
    """
    if cg.power_pins(tap_ref):
        return False
    for pin_num, _ in cg.pins_of(tap_ref):
        net = cg.net_for_pin(tap_ref, pin_num)
        if net is None or net.name == source_net:
            continue
        if net.is_ground or net.is_power:
            continue
        if any(node.ref == main_ref for node in net.nodes):
            return False
    return True


def _decoupling_cap(cg: CircuitGraph, ref: str) -> tuple[str, str] | None:
    """If `ref` is a 2-pin Device:C/CP with exactly one pin on a power net and
    one pin on a GND net, return (power_pin, gnd_pin); else None.

    Used to peel decoupling caps off the rail fan-out and place them as
    standalone islands (cap + paired +V flag + paired GND flag) connected to
    the rest of the circuit only by net-name matching.
    """
    comp = cg.components.get(ref)
    if comp is None or comp.kind not in ("C", "CP"):
        return None
    pwr = cg.power_pins(ref)
    gnd = cg.gnd_pins(ref)
    if len(pwr) == 1 and len(gnd) == 1:
        return (pwr[0][0], gnd[0])
    return None


def _unplaced_taps(cg: CircuitGraph, net_name: str,
                   placed: dict[str, PlacedSymbol],
                   exclude_ref: str) -> list[tuple[str, str]]:
    """All (ref, pin) on net_name that aren't yet placed and aren't on
    exclude_ref. Sorted for determinism.
    """
    net = next((n for n in cg.nets if n.name == net_name), None)
    if net is None:
        return []
    taps = [
        (node.ref, node.pin)
        for node in net.nodes
        if node.ref != exclude_ref and node.ref not in placed
    ]
    return sorted(taps)


# ---------------------------------------------------------------------------
# Output packing
# ---------------------------------------------------------------------------

def _to_placement(placed: dict[str, PlacedSymbol],
                  flags: list[PlacedPowerFlag],
                  cg: CircuitGraph,
                  routing_hints: dict[str, float] | None = None) -> Placement:
    components: list[PlacedComponent] = []
    for ref in sorted(placed):
        ps = placed[ref]
        comp = cg.components[ref]
        components.append(PlacedComponent(
            ref=ref,
            lib_id=comp.lib_id,
            value=comp.value,
            footprint=comp.footprint,
            x=ps.origin_x,
            y=ps.origin_y,
            rotation=ps.rotation,
        ))
    return Placement(
        components=components,
        power_flags=flags,
        routing_hints=dict(routing_hints or {}),
    )


def _snap(v: float) -> float:
    return round(v / GRID) * GRID
