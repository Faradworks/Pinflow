"""Force-directed placer — experimental engine atop `emit.fdcore`.

Mirrors `cplace`'s scaffold (park+measure → anchor IC → orient → measure) but
replaces the constraint emit + per-axis `solve` with an iterative relaxation in
`emit.fdcore`, then snaps the settled layout to the alignment grid the rubric
rewards, repairs residual coincidence deterministically, and reuses cplace's
downstream router/serialize seam verbatim. The force core is kicad-free and
shared with the browser debug viewer, so the same physics that places here is
what the viewer animates.

v1 scope: single-IC. The IC is pinned at (IC_X, IC_Y) and orientation is
delegated to cplace's `_orient_all`; the force core decides positions only
(`Node.rot` stays 0). Zero/multi-IC defers to the legacy column placer (as
cplace does) until the v2 multi-IC path lands.
"""

from __future__ import annotations

from statistics import median

from pinflow_api.builders._common import sch_to_string
from pinflow_api.emit import fdcore
from pinflow_api.emit.classify import NetKind, Role
from pinflow_api.emit.layout_tree import Archetype, LayoutTree, build_layout_tree
from pinflow_api.emit.netlist import Netlist, is_ground_net_name
from pinflow_api.emit.netlist_to_sch import (
    PlacerError,
    PlacerResult,
    _hide_gnd_labels,
    _horizontal_field_angle,
    _natural_key,
    _place_and_measure,
    _place_connectivity,
    _place_no_connects,
    _rewrite_property_at,
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
    _move_to,
    _reposition_fields,
    _translate,
)
from pinflow_api.emit.placers.cplace import (
    _Part,
    _horiz_refs,
    _measure_part,
    _orient_all,
)

import kicad_sch_api as ksa

_GRID = 1.27          # rubric off_grid grid; pin offsets are 1.27-multiples
_ROW_TOL = 2.54       # "meant to align" band, matching rubric._ROW_TOL


# --- graph construction (parts -> fdcore nodes/edges) ------------------------

def _facing(off: tuple[float, float]) -> tuple[float, float]:
    """Axis-snapped outward facing of a pin from its offset — pins point away
    from the symbol origin. Matches the `pin.rotation`-derived facing (verified
    equivalent) without needing the rotation, and is correct in the oriented
    frame `_measure_part` reports."""
    ox, oy = off
    if ox == 0 and oy == 0:
        return (0.0, 0.0)
    if abs(ox) >= abs(oy):
        return (1.0 if ox > 0 else -1.0, 0.0)
    return (0.0, 1.0 if oy > 0 else -1.0)


def _net_kind(nc) -> str:
    if nc is None:
        return "signal"
    if nc.kind == NetKind.GROUND:
        return "ground"
    if nc.kind == NetKind.RAIL:
        return "rail"
    return "signal"


def _build_graph(parts: dict[str, _Part], tree: LayoutTree, netlist: Netlist,
                 anchor: str) -> tuple[list[fdcore.Node], list[fdcore.Edge]]:
    plan = tree.plan
    nodes: list[fdcore.Node] = []
    for ref in sorted(parts, key=_natural_key):
        p = parts[ref]
        pins = [fdcore.Pin(num, off[0], off[1], *_facing(off))
                for num, off in sorted(p.pin_off.items())]
        role = plan.parts[ref].role if ref in plan.parts else None
        nodes.append(fdcore.Node(
            ref=ref,
            # symmetric half-extents centred on the origin; max() of the two
            # sides is conservative (slightly over-separates — gate-safe).
            hx=max(p.leftext, p.rightext),
            hy=max(p.topext, p.botext),
            pins=pins,
            is_ic=(ref == anchor),
            role=(role.name if role is not None else None),
            pinned=(ref == anchor),
            x=p.origin[0],
            y=p.origin[1],
        ))

    edges: list[fdcore.Edge] = []
    for net in netlist.nets:
        eps = [(ep.ref, ep.pin) for ep in net.endpoints if ep.ref in parts]
        if len(eps) < 2:
            continue
        edges.append(fdcore.Edge(net=net.name,
                                 kind=_net_kind(plan.nets.get(net.name)),
                                 pins=eps))
    return nodes, edges


# --- finalize: align snap -> grid snap -> anti-stack -------------------------

def _snap_groups(pos: dict[str, tuple[float, float]], tree: LayoutTree,
                 parts: dict[str, _Part], anchor: str) -> set[str]:
    """Archetype-aware coherence snap — the chain_coherence / alignment / spacing
    targets. Compacts each feedback divider into a tight vertical stack at its
    centroid (rail/ground gravity otherwise splits the two legs apart), and snaps
    each rail cap-bank onto a shared row. Returns the refs it placed so the
    generic prefix-align skips them. Mutates `pos`."""
    plan = tree.plan
    handled: set[str] = set()
    # Dense-IC layouts (a SIGNAL_STAIRCASE and DIVIDER_STACK on one side — the
    # pattern cplace defers to greedy) can't take an even-pitch bank spread
    # without colliding the staircase; there, keep the force positions row-
    # aligned only.
    staircase_sides = {g.side for g in tree.groups
                       if g.archetype == Archetype.SIGNAL_STAIRCASE}
    divider_sides = {g.side for g in tree.groups
                     if g.archetype == Archetype.DIVIDER_STACK}
    dense_ic = bool(staircase_sides & divider_sides)

    # Feedback / shunt dividers → tight vertical stack (high leg above low leg,
    # already oriented rail→tap→ground by _orient_all).
    for div in getattr(plan, "dividers", []) or []:
        hi, lo = div.high_refdes, div.low_refdes
        if hi not in pos or lo not in pos:
            continue
        mx = median([pos[hi][0], pos[lo][0]])
        midy = (pos[hi][1] + pos[lo][1]) / 2
        gap = parts[hi].botext + parts[lo].topext + STACK_GAP
        pos[hi] = (mx, midy - gap / 2)
        pos[lo] = (mx, midy + gap / 2)
        handled.update((hi, lo))

    # Shunt branches (rail→…→GND chains, e.g. an LED indicator) → tidy vertical
    # column at the chain's centroid. chain_coherence wants tightness on one
    # axis; a shared-X column delivers it.
    for g in tree.groups:
        if g.archetype != Archetype.SHUNT_BRANCH:
            continue
        members = [r for r in g.members if r in pos and r not in handled]
        if len(members) < 2:
            continue
        members.sort(key=lambda r: pos[r][1])
        mx = median([pos[r][0] for r in members])
        total = (sum(parts[r].topext + parts[r].botext for r in members)
                 + STACK_GAP * (len(members) - 1))
        y = sum(pos[r][1] for r in members) / len(members) - total / 2
        for r in members:
            y += parts[r].topext
            pos[r] = (mx, y)
            y += parts[r].botext + STACK_GAP
        handled.update(members)

    # Rail cap-banks → even-pitched line on a shared axis (the caps' decoupling
    # run off the rail). Even pitch is the `spacing` metric's target; a wide-
    # enough pitch also clears the field labels that otherwise overlap when the
    # forces pack a big bank tight (the dominant `label_collision` source).
    for g in tree.groups:
        if g.archetype != Archetype.RAIL_CAP_BANK:
            continue
        caps = [r for r in g.members
                if r in pos and r not in handled
                and plan.parts[r].role != Role.SERIES_ELEMENT]
        if len(caps) < 2:
            continue
        xs = [pos[r][0] for r in caps]
        ys = [pos[r][1] for r in caps]
        horizontal = (max(xs) - min(xs)) >= (max(ys) - min(ys))
        if dense_ic:
            # Shared-row align only — no pitch spread (would hit the staircase).
            my = median(ys)
            for r in caps:
                pos[r] = (pos[r][0], my)
            handled.update(caps)
            continue
        # Order along the run axis; redistribute at uniform pitch about the
        # bank's current centroid (so it stays where the forces parked it).
        caps.sort(key=lambda r: pos[r][0] if horizontal else pos[r][1])
        if horizontal:
            pitch = max(parts[r].leftext + parts[r].rightext for r in caps) + CAP_GAP
            cx = sum(xs) / len(xs)
            my = median(ys)
            start = cx - (len(caps) - 1) * pitch / 2
            for i, r in enumerate(caps):
                pos[r] = (start + i * pitch, my)
        else:
            pitch = max(parts[r].topext + parts[r].botext for r in caps) + CAP_GAP
            cy = sum(ys) / len(ys)
            mx = median(xs)
            start = cy - (len(caps) - 1) * pitch / 2
            for i, r in enumerate(caps):
                pos[r] = (mx, start + i * pitch)
        handled.update(caps)
    return handled


def _align_snap(pos: dict[str, tuple[float, float]], anchor: str,
                skip: set[str]) -> None:
    """Within each refdes-prefix cohort (minus group-handled refs), snap members
    already sharing an axis (cross-coord spread ≤ `_ROW_TOL`) onto that axis's
    median — the `alignment` metric's target. Mutates `pos`."""
    cohorts: dict[str, list[str]] = {}
    for ref in pos:
        if ref == anchor or ref in skip:
            continue
        prefix = ref.rstrip("0123456789")
        cohorts.setdefault(prefix, []).append(ref)
    for members in cohorts.values():
        if len(members) < 2:
            continue
        xs = [pos[r][0] for r in members]
        ys = [pos[r][1] for r in members]
        # Shared column (tight X) → snap X to median; shared row → snap Y.
        if max(xs) - min(xs) <= _ROW_TOL:
            mx = median(xs)
            for r in members:
                pos[r] = (mx, pos[r][1])
        if max(ys) - min(ys) <= _ROW_TOL:
            my = median(ys)
            for r in members:
                pos[r] = (pos[r][0], my)


def _finalize_positions(result: fdcore.SimResult, tree: LayoutTree,
                        parts: dict[str, _Part],
                        anchor: str) -> dict[str, tuple[float, float]]:
    """Settled force positions → final, grid-snapped, coincidence-free origins.
    Order is load-bearing: group snap → prefix align → grid snap → anti-stack."""
    pos = {ref: result.positions[ref] for ref in result.positions}
    handled = _snap_groups(pos, tree, parts, anchor)
    _align_snap(pos, anchor, handled)

    # Grid-snap support origins to 1.27 so pins (1.27-multiple offsets) land
    # on-grid and meet the IC's pins straight.
    for ref in pos:
        if ref == anchor:
            continue
        x, y = pos[ref]
        pos[ref] = (round(x / _GRID) * _GRID, round(y / _GRID) * _GRID)

    # Anti-stack (ported from cplace): two parts must never share an origin —
    # coincident pins silently merge nets and fail the topology check. Stagger
    # duplicates deterministically.
    taken: set[tuple[float, float]] = set()
    if anchor in parts:
        a = parts[anchor]
        taken.add((round(a.origin[0], 2), round(a.origin[1], 2)))
    for ref in sorted(pos, key=_natural_key):
        if ref == anchor:
            continue
        x, y = pos[ref]
        while (round(x, 2), round(y, 2)) in taken:
            y += COL_GAP
        taken.add((round(x, 2), round(y, 2)))
        pos[ref] = (x, y)
    return pos


# --- label pass (cosmetic; never moves a part) -------------------------------

_CW = 1.1     # approx mm per character at KiCad's 1.27 mm default text
_LH = 1.6     # approx line height (mm)


def _overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _block_dims(ref: str, val: str) -> tuple[float, float]:
    w = max(len(ref), len(val), 2) * _CW + 0.8
    return w, 2 * _LH + 0.8


def _place_labels(text: str, sch, parts: dict[str, _Part], anchor: str,
                  label_specs, netlist: Netlist) -> str:
    """Greedy label placement — for each part, stack its Reference/Value on the
    side (above/below/left/right of the body, preferring outward from the IC)
    that overlaps the fewest net labels and already-placed fields. The rubric's
    `label_collision` scores text-on-text only, so bodies/wires aren't avoided;
    net labels and other fields are. Pure text rewrite — parts never move."""
    values = {p.refdes: (p.value or p.refdes) for p in netlist.parts}
    # Obstacles that count: visible net labels (GND labels are hidden later).
    obstacles: list[tuple[float, float, float, float]] = []
    for ls in label_specs:
        if is_ground_net_name(ls.net_name):
            continue
        x, y = ls.position
        w = max(2.0, len(ls.net_name) * _CW)
        obstacles.append((x - w / 2, y - _LH, x + w / 2, y + _LH))

    # IC keeps its established clear band above the body; record that block.
    text = _reposition_fields(text, sch, anchor, above=True)
    ic = parts[anchor]
    icw, ich = _block_dims(anchor, values.get(anchor, anchor))
    obstacles.append((ic.origin[0] - icw / 2, ic.origin[1] - ic.topext - ich,
                      ic.origin[0] + icw / 2, ic.origin[1] - ic.topext))
    ic_cx, ic_cy = ic.origin

    for ref in sorted(parts, key=_natural_key):
        if ref == anchor:
            continue
        p = parts[ref]
        comp = sch.components.get(ref)
        if comp is None:
            continue
        ox, oy = p.origin
        w, h = _block_dims(ref, values.get(ref, ref))
        bcx, bcy = ox + (p.rightext - p.leftext) / 2, oy + (p.botext - p.topext) / 2
        g = 0.9
        cand = {
            "above": (bcx, oy - p.topext - g - h / 2),
            "below": (bcx, oy + p.botext + g + h / 2),
            "left":  (ox - p.leftext - g - w / 2, bcy),
            "right": (ox + p.rightext + g + w / 2, bcy),
        }
        dx, dy = ox - ic_cx, oy - ic_cy
        if abs(dx) >= abs(dy):
            order = (["right", "above", "below", "left"] if dx >= 0
                     else ["left", "above", "below", "right"])
        else:
            order = (["below", "left", "right", "above"] if dy >= 0
                     else ["above", "left", "right", "below"])

        best, best_sc = None, 1e9
        for side in order:
            cx, cy = cand[side]
            box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            sc = sum(1 for o in obstacles if _overlap(box, o))
            if sc < best_sc:
                best, best_sc = (cx, cy, box), sc
                if sc == 0:
                    break
        cx, cy, box = best
        angle = _horizontal_field_angle(getattr(comp, "rotation", 0) or 0)
        text, ri = _rewrite_property_at(
            text, f'(property "Reference" "{ref}"', 0, cx, cy - _LH, angle)
        if ri != -1:
            text, _ = _rewrite_property_at(
                text, '(property "Value"', ri, cx, cy + _LH, angle)
        obstacles.append(box)
    return text


# --- build + public entry ----------------------------------------------------

def _prepare(netlist: Netlist, tree: LayoutTree, title: str):
    """Shared scaffold — cplace._build_once's pre-solve half: park + measure
    parts, anchor the IC at (IC_X, IC_Y), orient via `_orient_all`, re-measure
    post-orientation, build the fdcore graph. Reused by both the placer and the
    viewer trace so they relax identical geometry."""
    sch = ksa.create_schematic(title)
    issues: list[str] = []
    anchor = tree.anchor

    horiz = _horiz_refs(tree)
    ordered = sorted(netlist.parts, key=lambda p: _natural_key(p.refdes))
    cells = _place_and_measure(sch, ordered, issues, rotate_horizontal=horiz)
    by_ref = {c.refdes: c for c in cells}
    if anchor not in by_ref:
        raise PlacerError([f"IC {anchor} failed to place"])

    placed_refs: dict[str, tuple[float, float]] = {}
    _move_to(by_ref[anchor], IC_X, IC_Y, placed_refs)
    _orient_all(tree, by_ref, sch.components.get(anchor))

    parts: dict[str, _Part] = {}
    for ref, cell in by_ref.items():
        mp = _measure_part(cell)
        if mp is not None:
            parts[ref] = mp
    if anchor not in parts:
        raise PlacerError([f"could not measure IC {anchor}"])

    nodes, edges = _build_graph(parts, tree, netlist, anchor)
    return sch, issues, placed_refs, parts, nodes, edges


def _build_once(netlist: Netlist, tree: LayoutTree, title: str, wiring: str,
                cfg: fdcore.SimConfig | None = None) -> PlacerResult:
    plan = tree.plan
    anchor = tree.anchor
    cfg = cfg or fdcore.SimConfig()
    sch, issues, placed_refs, parts, nodes, edges = _prepare(netlist, tree, title)

    # ---- the seam: force solve replaces cplace's emit + solve ----
    result = fdcore.simulate(nodes, edges, cfg)
    positions = _finalize_positions(result, tree, parts, anchor)

    for ref in sorted(parts, key=_natural_key):
        p = parts[ref]
        if ref == anchor:
            placed_refs.setdefault(ref, (_snap(p.origin[0] - p.leftext),
                                         _snap(p.origin[1] - p.topext)))
            continue
        tx, ty = positions[ref]
        _translate(p.cell, tx - p.origin[0], ty - p.origin[1], placed_refs)
        # Refresh origin to the solved position: the cosmetic `_place_labels`
        # pass below reads `p.origin`, and a stale parked origin (near 0) would
        # strand every label far from its translated body.
        p.origin = (tx, ty)

    # ---- shared downstream (mirrors cplace._build_once) ----
    label_specs = _place_connectivity(sch, netlist, plan, placed_refs, issues,
                                      wiring=wiring)
    _place_no_connects(sch, netlist, placed_refs, issues)
    text = _hide_gnd_labels(sch_to_string(sch), netlist)
    text = _place_labels(text, sch, parts, anchor, label_specs, netlist)

    return PlacerResult(sch_text=text, issues=issues,
                        placed_refs=placed_refs, label_specs=label_specs)


def fdplace(netlist: Netlist, *, title: str = "Subcircuit",
            tree: LayoutTree | None = None,
            cfg: fdcore.SimConfig | None = None) -> PlacerResult:
    """Force-directed placer entry point. Single-IC scope (v1); zero/multi-IC
    defers to the legacy column placer. Wires with the crossing-minimising
    router, falling back to label-only wiring if the router corrupts topology —
    the same guard as cplace."""
    errors = netlist.validate_self()
    if errors:
        raise PlacerError(errors)
    if tree is None:
        tree = build_layout_tree(netlist)
    if tree.anchor is None:
        return place(netlist, title=title)

    routed = _build_once(netlist, tree, title, "router", cfg=cfg)
    if _topology_intact(netlist, routed):
        return routed
    fallback = _build_once(netlist, tree, title, "labels", cfg=cfg)
    fallback.issues.append(
        "router output failed the connectivity check — fell back to "
        "label-only wiring"
    )
    return fallback


def trace_layout(netlist: Netlist, *, title: str = "Subcircuit",
                 tree: LayoutTree | None = None,
                 cfg: fdcore.SimConfig | None = None) -> dict:
    """Run the production force sim with per-iteration capture and return the
    debug-viewer payload: the node/edge graph, the config, the convergence
    `frames`, and the final post-snap origins. The *same* `_prepare` +
    `fdcore.simulate` the placer uses, so the viewer animates the real
    algorithm. Single-IC only (the v1 placer scope)."""
    if tree is None:
        tree = build_layout_tree(netlist)
    if tree.anchor is None:
        raise PlacerError(["trace_layout requires a single-IC netlist"])
    base = cfg or fdcore.SimConfig()
    cfg = fdcore.SimConfig(gains=base.gains, iters=base.iters, seed=base.seed,
                           margin=base.margin, grid=base.grid, trace=True,
                           reorient_every=base.reorient_every)

    _, _, _, parts, nodes, edges = _prepare(netlist, tree, title)
    result = fdcore.simulate(nodes, edges, cfg)
    snapped = _finalize_positions(result, tree, parts, tree.anchor)

    return {
        "name": title,
        "graph": {
            "nodes": [{
                "ref": n.ref, "is_ic": n.is_ic, "pinned": n.pinned,
                "role": n.role, "hx": round(n.hx, 3), "hy": round(n.hy, 3),
                "pins": [{"num": p.num, "ox": p.ox, "oy": p.oy,
                          "fx": p.fx, "fy": p.fy} for p in n.pins],
            } for n in nodes],
            "edges": [{"net": e.net, "kind": e.kind,
                       "endpoints": [{"ref": r, "pin": p} for r, p in e.pins]}
                      for e in edges],
        },
        "config": {"gains": cfg.gains, "iters": cfg.iters,
                   "margin": cfg.margin, "grid": cfg.grid},
        "frames": result.frames,
        "snapped": {k: [round(v[0], 3), round(v[1], 3)]
                    for k, v in snapped.items()},
    }
