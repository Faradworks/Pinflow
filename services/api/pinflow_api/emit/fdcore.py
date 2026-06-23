"""Pure force-directed layout core — the physics engine, kicad-free.

The single source of truth shared by the production placer
(`emit.placers.fdplace`) and the browser debug viewer
(`scripts/dump_layout_graph.py --trace`). It operates on plain dataclasses with
**no** `kicad_sch_api` import, so the same `simulate()` that places a schematic
also dumps the frame trace the viewer animates — there is no JavaScript physics
twin to drift out of sync.

Frame / units. Coordinates are symbol *origins* in the schematic screen frame
(millimetres, Y-down) — the same frame `cplace._Part.origin` and `placed_refs`
use. A pin's world position is `rotate((ox,oy), rot) + (x,y)`; the core owns a
tiny 4-angle rotation. It never re-derives pin geometry — it only rotates the
offsets/facings the wrapper measured from the oriented symbol.

Forces (per-axis / Manhattan, every law saturating so no pair blows up):
  - attraction: each pin → its net centroid (star model) — the wirelength /
    rail-proximity gradient;
  - repulsion: AABB-overlap push with a 2.54 mm margin between non-IC bodies —
    the `rubric` symbol_overlap-gate gradient;
  - fields: power-up / ground-down gravity and role-based left→right flow —
    the orientation + flow metrics, as gentle constant biases.

Integrator: damped Euler with a cooling velocity clamp, so the system
dissipates energy and settles (we want decay, not Verlet's conservation).
Everything is deterministic — derived init, sorted iteration order, fixed
iteration count, pure-Python floats — because the golden score-floors depend
on a given netlist always producing the same layout.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


# --- data --------------------------------------------------------------------

@dataclass(frozen=True)
class Pin:
    """A pin in the node's local frame at rot=0 (screen Y-down). `ox/oy` is the
    offset from the symbol origin; `fx/fy` is the axis-snapped facing unit
    vector (the direction the wire leaves the pin)."""

    num: str
    ox: float
    oy: float
    fx: float
    fy: float


@dataclass
class Node:
    """A placeable part. `x/y` is the symbol origin (mutated by the sim); `rot`
    is 0/90/180/270 (owned by the discrete re-orient step, 0 in v1). A `pinned`
    node is held fixed — the IC anchor in single-IC layouts."""

    ref: str
    hx: float
    hy: float
    pins: list[Pin]
    is_ic: bool
    role: str | None = None      # Role.name (e.g. "INPUT_CAP") or None
    side: str | None = None      # target IC edge "L"/"R" (folded) or None (no bias)
    pinned: bool = False
    x: float = 0.0
    y: float = 0.0
    rot: int = 0


@dataclass
class Edge:
    """One net. `kind` is "ground" | "power"/"rail" | "signal". `pins` are the
    (ref, pin_num) endpoints."""

    net: str
    kind: str
    pins: list[tuple[str, str]]


DEFAULT_GAINS: dict[str, float] = {
    "attract": 1.0,       # star attraction toward net centroids
    "repel": 8.0,         # AABB-overlap push (per mm of penetration)
    "repel_aniso": 1.25,  # axis preference for overlap resolution: 1.0 = pure
                          # least-penetration (isotropic); >1 makes horizontal
                          # separation "cheaper" so collapsed same-net clusters
                          # (cap banks, parallel shunts) fan into a row instead
                          # of a column — choose x unless y-penetration is >K×
                          # smaller (see `_repulsion`). 1.25 swept Pareto-best
                          # across the corpus (mcu_rp2040 .66→.93, nothing
                          # regressed); K≥1.5 trades fixtures non-monotonically.
    "gravity_rail": 0.5,  # constant upward bias on rail-touching parts
    "gravity_gnd": 0.5,   # constant downward bias on ground-touching parts
    "flow": 0.5,          # constant left/right bias by role (input←, output→)
    "side": 1.5,          # one-sided barrier pushing each support past its IC
                          # side edge (real pin data; supersedes `flow` per node)
}


@dataclass
class SimConfig:
    gains: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_GAINS))
    iters: int = 600
    seed: int = 0
    margin: float = 2.54         # symbol_overlap gate margin (mm)
    grid: float = 1.27           # snap grid (final snap by the wrapper; also the
                                 # in-loop quantum when `snap_grid` is on)
    init_spread: float = 1.0     # scale on the per-node seed offset radius in
                                 # `_init_positions`. 1.0 = current tight seeding
                                 # (each support ~3.8–8.9 mm off its connected IC
                                 # pin); >1 starts supports farther out / less
                                 # overlapped so early repulsion is gentler — but
                                 # the iteration/cooling budget is fixed, so too
                                 # wide under-converges (parts stranded in flight).
                                 # 1.0 is byte-exact with the pre-knob seeding.
    best_of: tuple[tuple[float, int], ...] = ()
                                 # opt-in best-of-spread. Each (init_spread, iters)
                                 # is a candidate the *placer* (fdplace) places and
                                 # rubric-scores, keeping the highest. () = single-
                                 # shot (default). The physics core ignores this —
                                 # it's orchestration data fdplace reads. A max over
                                 # candidates can't regress single-shot; pair wide
                                 # spreads with more iters or they under-converge.
    snap_grid: bool = True       # hard-snap every free node to `grid` after each
                                 # integration step (velocity stays continuous).
                                 # Makes the relaxation itself grid-exact, so the
                                 # animation == the placed result and the wrapper's
                                 # trailing grid round is a no-op. Set False for the
                                 # legacy continuous relax + one-shot snap at the end.
    trace: bool = False
    trace_every: int = 0         # 0 → auto (~200 frames)
    reorient_every: int = 0      # 0 → core does no rotation (v1)


@dataclass
class SimResult:
    positions: dict[str, tuple[float, float]]
    rotations: dict[str, int]
    frames: list[dict]


# --- constants ---------------------------------------------------------------

_A_SAT = 25.4        # attraction saturation (10 grid) — force magnitude ceiling
_SIDE_SAT = 12.7     # side-bias saturation (5 grid) — kept below `_A_SAT` so the
                     # side barrier never out-pulls attraction's row alignment
_DAMP = 0.85         # velocity damping per iteration
_VMAX = 5.08         # velocity clamp (2 grid / iter), scaled by the cooling term
_FLOW_BIAS = {"INPUT_CAP": -1.0, "OUTPUT_CAP": 1.0, "DIVIDER_RESISTOR": 1.0}


def _net_w(kind: str) -> float:
    if kind == "ground":
        return 0.3       # GND is symbol/label-dropped downstream; barely route it
    if kind in ("rail", "power"):
        return 0.6       # rails also get gravity; don't double-pull
    return 1.0           # signal


def _is_rail(kind: str) -> bool:
    return kind in ("rail", "power")


# --- helpers -----------------------------------------------------------------

def _nkey(ref: str) -> tuple:
    """Natural sort key (prefix, number, ref) — a stable total order so every
    loop is deterministic regardless of input order."""
    m = re.match(r"^(\D*)(\d*)", ref)
    prefix = m.group(1) if m else ref
    num = int(m.group(2)) if (m and m.group(2)) else -1
    return (prefix, num, ref)


def _sat(e: float, s: float) -> float:
    """Saturating spring shaping: ~linear near 0, asymptotes to ±s."""
    return s * math.tanh(e / s)


def _clamp(v: float, lim: float) -> float:
    if v > lim:
        return lim
    if v < -lim:
        return -lim
    return v


def _rot(ox: float, oy: float, deg: int) -> tuple[float, float]:
    """Rotate a local vector by a multiple of 90° (screen frame)."""
    if deg == 90:
        return (-oy, ox)
    if deg == 180:
        return (-ox, -oy)
    if deg == 270:
        return (oy, -ox)
    return (ox, oy)


def _find_pin(node: Node, num: str) -> Pin | None:
    for p in node.pins:
        if p.num == num:
            return p
    return None


def _pin_world(node: Node, pin: Pin) -> tuple[float, float]:
    rx, ry = _rot(pin.ox, pin.oy, node.rot)
    return (node.x + rx, node.y + ry)


def _facing_world(node: Node, pin: Pin) -> tuple[float, float]:
    return _rot(pin.fx, pin.fy, node.rot)


# --- initial positions -------------------------------------------------------

def _init_positions(nodes: list[Node], edges: list[Edge],
                    by_ref: dict[str, Node], spread: float = 1.0) -> None:
    """Seed each free node near a pinned (IC) pin it connects to, with a
    deterministic per-index offset so coincident seeds separate. No RNG on the
    main path — init is a pure function of the input. `spread` scales the seed
    offset radius (1.0 = baseline tight seeding)."""
    pinned = {n.ref for n in nodes if n.pinned}
    for idx, n in enumerate(nodes):
        if n.pinned:
            continue
        target: tuple[float, float] | None = None
        for e in edges:
            refs = {r for r, _ in e.pins}
            if n.ref not in refs:
                continue
            for r, pnum in e.pins:
                if r in pinned and r != n.ref:
                    host = by_ref[r]
                    hp = _find_pin(host, pnum)
                    if hp is not None:
                        target = _pin_world(host, hp)
                        break
            if target is not None:
                break
        if target is None:
            if pinned:
                cx = sum(by_ref[r].x for r in sorted(pinned)) / len(pinned)
                cy = sum(by_ref[r].y for r in sorted(pinned)) / len(pinned)
            else:
                cx = cy = 0.0
            target = (cx, cy)
        # Deterministic golden-angle spread so identically-seeded supports fan
        # out instead of stacking (which would make repulsion start degenerate).
        ang = idx * 2.399963229728653  # golden angle (rad)
        off = (3.81 + (idx % 5) * 1.27) * spread
        n.x = target[0] + math.cos(ang) * off
        n.y = target[1] + math.sin(ang) * off


# --- force accumulation ------------------------------------------------------

def _attraction(nodes, edges, by_ref, g, fx, fy) -> None:
    ga = g["attract"]
    for e in edges:
        members: list[tuple[Node, float, float]] = []
        for ref, pnum in e.pins:
            n = by_ref.get(ref)
            if n is None:
                continue
            pin = _find_pin(n, pnum)
            if pin is None:
                continue
            wx, wy = _pin_world(n, pin)
            members.append((n, wx, wy))
        if len(members) < 2:
            continue
        cx = sum(m[1] for m in members) / len(members)
        cy = sum(m[2] for m in members) / len(members)
        w = _net_w(e.kind)
        for n, wx, wy in members:
            if n.pinned:
                continue
            fx[n.ref] += ga * w * _sat(cx - wx, _A_SAT)
            fy[n.ref] += ga * w * _sat(cy - wy, _A_SAT)


def _repulsion(nodes, g, margin, fx, fy) -> None:
    """AABB-overlap push along the axis of least penetration (cheapest, axis-
    aligned separation). Between non-IC bodies this is the symbol_overlap-gate
    gradient; the pinned IC is included as an obstacle so supports settle just
    outside its body (their pins still reach the edge pins) instead of
    collapsing onto it — free w.r.t. the gate, which excludes IC pairs anyway.
    A both-pinned pair is a no-op.

    `repel_aniso` (K) tilts the axis choice: x is picked whenever `ox <= oy*K`,
    so K>1 makes horizontal separation "cheaper" than the true least-penetration
    axis. A same-net cluster that attraction has collapsed to a point is ~square
    (ox≈oy), so the bias decides whether it fans into a row (idiomatic — caps
    hanging off a horizontal rail) or a column. The full chosen-axis penetration
    is still resolved, so the pair always ends non-overlapping (stable)."""
    gr = g["repel"]
    aniso = g.get("repel_aniso", 1.0)
    for i in range(len(nodes)):
        a = nodes[i]
        for j in range(i + 1, len(nodes)):
            b = nodes[j]
            if a.pinned and b.pinned:
                continue
            dx = b.x - a.x
            dy = b.y - a.y
            ox = (a.hx + b.hx + margin) - abs(dx)
            oy = (a.hy + b.hy + margin) - abs(dy)
            if ox <= 0 or oy <= 0:
                continue
            if ox <= oy * aniso:
                push = gr * ox
                s = 1.0 if dx >= 0 else -1.0
                if not a.pinned:
                    fx[a.ref] -= s * push
                if not b.pinned:
                    fx[b.ref] += s * push
            else:
                push = gr * oy
                s = 1.0 if dy >= 0 else -1.0
                if not a.pinned:
                    fy[a.ref] -= s * push
                if not b.pinned:
                    fy[b.ref] += s * push


def _fields(nodes, edges, g, fx, fy) -> None:
    """Constant convention biases: rail parts drift up, ground parts down,
    inputs left / outputs right by role."""
    rail_refs: set[str] = set()
    gnd_refs: set[str] = set()
    for e in edges:
        if _is_rail(e.kind):
            rail_refs.update(r for r, _ in e.pins)
        elif e.kind == "ground":
            gnd_refs.update(r for r, _ in e.pins)
    for n in nodes:
        if n.pinned:
            continue
        if n.ref in rail_refs:
            fy[n.ref] -= g["gravity_rail"]
        if n.ref in gnd_refs:
            fy[n.ref] += g["gravity_gnd"]
        # Role flow bias is the coarse fallback: only for nodes without a
        # real-pin side. `_side_bias` (real pin data) owns the horizontal
        # placement of every node that has a side.
        if n.side is None:
            bias = _FLOW_BIAS.get(n.role or "", 0.0)
            if bias:
                fx[n.ref] += g["flow"] * bias


def _side_bias(nodes, g, margin, ic, fx, fy) -> None:
    """One-sided, x-only barrier pulling each support node past the IC edge on
    its assigned side. It enforces *which side* only — never touches `fy`, so
    `_attraction` keeps owning the row (secondary axis) and `_repulsion` fans
    same-side parts out along it. Once a node clears its edge the force is zero,
    so it never fights attraction or collapses a side onto one column."""
    if ic is None:
        return
    right, left = ic.x + ic.hx, ic.x - ic.hx
    for n in nodes:
        if n.pinned or n.side is None:
            continue
        if n.side == "R":
            e = (right + margin + n.hx) - n.x       # >0 ⇒ node is too far left
            if e > 0:
                fx[n.ref] += g["side"] * _sat(e, _SIDE_SAT)
        else:  # "L"
            e = (left - margin - n.hx) - n.x        # <0 ⇒ node is too far right
            if e < 0:
                fx[n.ref] += g["side"] * _sat(e, _SIDE_SAT)


# --- discrete re-orient (v2; inactive when reorient_every == 0) --------------

def _greedy_reorient(nodes, edges, by_ref) -> None:
    """Per free node, pick the rotation in {0,90,180,270} that best aligns its
    pins' facings with the direction to each pin's net centroid. Applied with
    hysteresis so equal-cost orientations don't flip-flop across iterations."""
    centroids = _net_centroids(edges, by_ref)
    pin_nets = _pin_net_index(edges)
    for n in nodes:
        if n.pinned or len(n.pins) < 2:
            continue
        cur = n.rot
        best_rot, best_cost = cur, _orient_cost(n, cur, centroids, pin_nets)
        for cand in (0, 90, 180, 270):
            if cand == cur:
                continue
            c = _orient_cost(n, cand, centroids, pin_nets)
            if c < best_cost:
                best_rot, best_cost = cand, c
        cur_cost = _orient_cost(n, cur, centroids, pin_nets)
        if best_rot != cur and best_cost < cur_cost - 0.15:
            n.rot = best_rot


def _orient_cost(n, rot, centroids, pin_nets) -> float:
    cost = 0.0
    for p in n.pins:
        net = pin_nets.get((n.ref, p.num))
        c = centroids.get(net) if net else None
        if c is None:
            continue
        rx, ry = _rot(p.ox, p.oy, rot)
        px, py = n.x + rx, n.y + ry
        dx, dy = c[0] - px, c[1] - py
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            continue
        fxr, fyr = _rot(p.fx, p.fy, rot)
        cost += 1.0 - (fxr * dx + fyr * dy) / dist
    return cost


def _net_centroids(edges, by_ref) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for e in edges:
        pts = []
        for ref, pnum in e.pins:
            n = by_ref.get(ref)
            if n is None:
                continue
            pin = _find_pin(n, pnum)
            if pin is not None:
                pts.append(_pin_world(n, pin))
        if pts:
            out[e.net] = (sum(p[0] for p in pts) / len(pts),
                          sum(p[1] for p in pts) / len(pts))
    return out


def _pin_net_index(edges) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for e in edges:
        for ref, pnum in e.pins:
            out[(ref, pnum)] = e.net
    return out


# --- the simulation ----------------------------------------------------------

def simulate(nodes: list[Node], edges: list[Edge], cfg: SimConfig) -> SimResult:
    """Relax `nodes` under the force model into a settled layout. Mutates the
    node `x/y/rot` in place and returns the final positions/rotations (plus a
    frame trace when `cfg.trace`). Pinned nodes keep the `x/y` the caller set."""
    g = {**DEFAULT_GAINS, **(cfg.gains or {})}
    nodes = sorted(nodes, key=lambda n: _nkey(n.ref))
    edges = sorted(edges, key=lambda e: e.net)
    by_ref = {n.ref: n for n in nodes}

    _init_positions(nodes, edges, by_ref, cfg.init_spread)
    ic = next((n for n in nodes if n.is_ic and n.pinned), None)
    vx = {n.ref: 0.0 for n in nodes}
    vy = {n.ref: 0.0 for n in nodes}
    frames: list[dict] = []
    trace_every = cfg.trace_every or max(1, cfg.iters // 200)
    snap, grid = cfg.snap_grid, cfg.grid

    for it in range(cfg.iters):
        fx = {n.ref: 0.0 for n in nodes}
        fy = {n.ref: 0.0 for n in nodes}
        _attraction(nodes, edges, by_ref, g, fx, fy)
        _repulsion(nodes, g, cfg.margin, fx, fy)
        _fields(nodes, edges, g, fx, fy)
        _side_bias(nodes, g, cfg.margin, ic, fx, fy)

        cool = max(0.05, 1.0 - it / cfg.iters)
        vmax = _VMAX * cool
        for n in nodes:
            if n.pinned:
                continue
            vx[n.ref] = _clamp((vx[n.ref] + fx[n.ref]) * _DAMP, vmax)
            vy[n.ref] = _clamp((vy[n.ref] + fy[n.ref]) * _DAMP, vmax)
            n.x += vx[n.ref]
            n.y += vy[n.ref]
            if snap:
                # Quantize the position itself (not the velocity): forces keep
                # accumulating continuously, but every captured/served frame —
                # and the final read-out — sits exactly on the grid.
                n.x = round(n.x / grid) * grid
                n.y = round(n.y / grid) * grid

        if (cfg.reorient_every and it >= cfg.iters // 2
                and it % cfg.reorient_every == 0):
            _greedy_reorient(nodes, edges, by_ref)

        if cfg.trace and (it % trace_every == 0 or it == cfg.iters - 1):
            frames.append({
                "iter": it,
                "nodes": [{"ref": n.ref, "x": round(n.x, 3),
                           "y": round(n.y, 3), "rot": n.rot} for n in nodes],
            })

    return SimResult(
        positions={n.ref: (n.x, n.y) for n in nodes},
        rotations={n.ref: n.rot for n in nodes},
        frames=frames,
    )
