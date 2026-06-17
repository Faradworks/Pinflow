"""Orthogonal wire router — crossing-minimising, junction-free.

Replaces the placer's star-fan wiring (`netlist_to_sch._place_connectivity`).
Given each net's pin coordinates it produces orthogonal wire segments
connecting them, choosing routes that minimise wire-wire *crossings* — the
readability cost a hand-drawn schematic drives to zero.

Connectivity hazards (probe + the example proved these the hard way). The
router is free to *cross* anything — a crossing is inert, only a readability
cost — but each of the following shorts two nets, so the router must avoid
them all:
  - a wire endpoint/corner coincident with a foreign pin or foreign corner;
  - a segment running collinear-overlapping a foreign segment;
  - a segment passing over a foreign *wired* pin (a pin that carries routed
    wires). Passing over a geometry-free pin — one that only carries a power
    symbol, e.g. a decoupling cap's GND pad — is safe and unavoidable, so the
    over-pin penalty applies only to the pins of the nets being routed.

Topology — each net is wired as a crossing-aware spanning tree over its
pins, grown edge by edge: every step adds the out-of-tree pin whose best
route to the tree scores lowest, so the tree shape itself dodges crossings,
not just the elbow of a pre-chosen edge. Every edge runs pin-to-pin, so
every wire endpoint lands on a pin: connections happen only at pins and no
`(junction)` element is ever needed. Each edge is routed straight, as an L,
a Z, or a detour, chosen to avoid foreign collinear overlap (a short —
heaviest weight) and corners on foreign pins, then to minimise crossings,
body intrusions and length.

v1 scope: greedy (route order = ascending net span; no rip-up/reroute). When
the placement crams unrelated nets into one column the router can only
detour around the overlap, not prevent it — separating those parts is
placement's job.
"""

from __future__ import annotations

from dataclasses import dataclass

Pt = tuple[float, float]
Seg = tuple[Pt, Pt]
Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

_EPS = 1e-6
_GRID = 2.54
_W_OVERLAP = 1000.0   # a foreign collinear overlap — a short; avoid at all cost
_W_KEEPOUT = 500.0    # a segment crossing a hard keep-out (the IC body) — never
                      # acceptable for readability, but still below a short, so
                      # the router detours around the IC yet never trades a
                      # genuine short to do it.
_W_CROSS = 10.0       # one wire-wire crossing — readability only
_W_BODY = 10.0        # one segment cutting through a (soft) passive body — set
                      # equal to _W_CROSS so the router routes AROUND a passive
                      # rather than slicing it, but still never trades a short
                      # (1000) or an IC keep-out (500) to dodge a soft body.
_W_STUB = 6.0         # per missing stub — corner landing flush on a pin
_W_LEN = 0.01         # per mm — a tie-breaker only
_MAX_Z = 6            # cap on Z-route midpoints tried per edge
_STUB_MIN = _GRID     # first/last segment shorter than this is a "no stub"


# --- geometry ---------------------------------------------------------------

def _orient(s: Seg) -> str:
    (x0, y0), (x1, y1) = s
    if abs(y0 - y1) < _EPS:
        return "h"
    if abs(x0 - x1) < _EPS:
        return "v"
    return "d"  # not expected — all router output is orthogonal


def _same(p: Pt, q: Pt) -> bool:
    return abs(p[0] - q[0]) < _EPS and abs(p[1] - q[1]) < _EPS


def _seg_len(s: Seg) -> float:
    (x0, y0), (x1, y1) = s
    return abs(x1 - x0) + abs(y1 - y0)


def _crosses(a: Seg, b: Seg) -> bool:
    """True if orthogonal segments `a` and `b` intersect at a point interior
    to both (a genuine crossing — a shared endpoint or a T does not count)."""
    oa, ob = _orient(a), _orient(b)
    if oa not in ("h", "v") or ob != ("v" if oa == "h" else "h"):
        return False
    h, v = (a, b) if oa == "h" else (b, a)
    hy = h[0][1]
    hx_lo, hx_hi = sorted((h[0][0], h[1][0]))
    vx = v[0][0]
    vy_lo, vy_hi = sorted((v[0][1], v[1][1]))
    return (hx_lo + _EPS < vx < hx_hi - _EPS
            and vy_lo + _EPS < hy < vy_hi - _EPS)


def _overlap(a: Seg, b: Seg) -> bool:
    """True if `a` and `b` are collinear and share a run of positive length —
    which, for two different nets, shorts them together (probe rule)."""
    oa, ob = _orient(a), _orient(b)
    if oa != ob or oa not in ("h", "v"):
        return False
    if oa == "h":
        if abs(a[0][1] - b[0][1]) > _EPS:   # different rows
            return False
        a_lo, a_hi = sorted((a[0][0], a[1][0]))
        b_lo, b_hi = sorted((b[0][0], b[1][0]))
    else:
        if abs(a[0][0] - b[0][0]) > _EPS:   # different columns
            return False
        a_lo, a_hi = sorted((a[0][1], a[1][1]))
        b_lo, b_hi = sorted((b[0][1], b[1][1]))
    return min(a_hi, b_hi) - max(a_lo, b_lo) > _EPS


def _interior(p: Pt, s: Seg) -> bool:
    """True if point `p` lies strictly inside orthogonal segment `s` (on it,
    but not at either endpoint)."""
    (x0, y0), (x1, y1) = s
    o = _orient(s)
    if o == "h":
        return (abs(p[1] - y0) < _EPS
                and min(x0, x1) + _EPS < p[0] < max(x0, x1) - _EPS)
    if o == "v":
        return (abs(p[0] - x0) < _EPS
                and min(y0, y1) + _EPS < p[1] < max(y0, y1) - _EPS)
    return False


def _seg_hits_rect(s: Seg, r: Rect) -> bool:
    """True if `s` passes through the interior of rect `r`."""
    x0, y0, x1, y1 = r
    (sx0, sy0), (sx1, sy1) = s
    o = _orient(s)
    if o == "h":
        lo, hi = sorted((sx0, sx1))
        return (y0 + _EPS < sy0 < y1 - _EPS
                and lo < x1 - _EPS and hi > x0 + _EPS)
    if o == "v":
        lo, hi = sorted((sy0, sy1))
        return (x0 + _EPS < sx0 < x1 - _EPS
                and lo < y1 - _EPS and hi > y0 + _EPS)
    return False


def _span(pts: list[Pt]) -> float:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs)) + (max(ys) - min(ys)) if pts else 0.0


def _dedup(pts: list[Pt]) -> list[Pt]:
    out: list[Pt] = []
    for p in pts:
        if not any(_same(p, q) for q in out):
            out.append(p)
    return out


# --- per-edge routing -------------------------------------------------------

def _between(c0: float, c1: float) -> list[float]:
    """Up to `_MAX_Z` grid-snapped coordinates spread strictly between c0,c1."""
    lo, hi = sorted((c0, c1))
    span = hi - lo
    if span < 2 * _GRID:
        return []
    k = min(_MAX_Z, int(span / _GRID) - 1)
    raw = (lo + span * (i + 1) / (k + 1) for i in range(k))
    return sorted({round(r / _GRID) * _GRID for r in raw})


def _candidates(a: Pt, b: Pt) -> list[list[Seg]]:
    """Orthogonal route options from a to b. For aligned pins: the straight
    wire plus sideways detours (so a straight run that would overlap a
    foreign wire can step aside). Otherwise: the two L elbows plus Z routes."""
    ax, ay = a
    bx, by = b
    if abs(ax - bx) < _EPS:                       # vertically aligned
        out = [[(a, b)]]
        for dx in (_GRID, -_GRID, 2 * _GRID, -2 * _GRID):
            mx = ax + dx
            out.append([(a, (mx, ay)), ((mx, ay), (mx, by)), ((mx, by), b)])
        return out
    if abs(ay - by) < _EPS:                       # horizontally aligned
        out = [[(a, b)]]
        for dy in (_GRID, -_GRID, 2 * _GRID, -2 * _GRID):
            my = ay + dy
            out.append([(a, (ax, my)), ((ax, my), (bx, my)), ((bx, my), b)])
        return out
    out: list[list[Seg]] = [
        [(a, (bx, ay)), ((bx, ay), b)],           # horizontal-first L
        [(a, (ax, by)), ((ax, by), b)],           # vertical-first L
    ]
    for mx in _between(ax, bx):
        out.append([(a, (mx, ay)), ((mx, ay), (mx, by)), ((mx, by), b)])
    for my in _between(ay, by):
        out.append([(a, (ax, my)), ((ax, my), (bx, my)), ((bx, my), b)])
    return out


def _corners(route: list[Seg]) -> list[Pt]:
    """Interior vertices of a route — the shared endpoints between segments."""
    return [route[i][1] for i in range(len(route) - 1)]


def _score(
    route: list[Seg], net: str, placed: list[tuple[str, Seg]],
    foreign_pins: list[Pt], bodies: list[Rect], keepouts: list[Rect],
) -> float:
    # A foreign collinear overlap, or a segment passing over a foreign wired
    # pin, both short two nets — weighted identically and far above the
    # readability costs below.
    overlap = sum(1 for s in route for (pn, e) in placed
                  if pn != net and _overlap(s, e))
    pin_hit = sum(1 for s in route for p in foreign_pins if _interior(p, s))
    cross = sum(1 for s in route for (_pn, e) in placed if _crosses(s, e))
    body = sum(1 for s in route for r in bodies if _seg_hits_rect(s, r))
    keep = sum(1 for s in route for r in keepouts if _seg_hits_rect(s, r))
    length = sum(_seg_len(s) for s in route)
    # Stub penalty: a corner sitting flush on a pin (first or last segment
    # shorter than one grid step) reads as a wire bending at the pin instead
    # of leaving cleanly. A straight 1-seg route has no corner — exempt.
    stub_miss = 0
    if len(route) >= 2:
        if _seg_len(route[0]) + _EPS < _STUB_MIN:
            stub_miss += 1
        if _seg_len(route[-1]) + _EPS < _STUB_MIN:
            stub_miss += 1
    return (_W_OVERLAP * (overlap + pin_hit) + _W_KEEPOUT * keep
            + _W_CROSS * cross + _W_BODY * body
            + _W_STUB * stub_miss + _W_LEN * length)


def _hint_route(a: Pt, b: Pt, hint_y: float) -> list[Seg] | None:
    """A Z-route from a to b whose horizontal trunk sits at `hint_y` — the
    shape an IC-pin → staircase-tap edge wants: leave the IC pin horizontally
    at the source-pin Y (above the row of bodies), then drop vertically at
    the tap's lane X to the tap pin. Returned even if it degenerates to an L
    or a straight segment; the scorer will collapse zero-length segs."""
    ax, ay = a
    bx, by = b
    if abs(ax - bx) < _EPS:
        # Vertically aligned — the hint Y trunk would zig-zag; skip and let
        # the standard candidate set route a straight wire.
        return None
    segs: list[Seg] = []
    if abs(ay - hint_y) > _EPS:
        segs.append((a, (ax, hint_y)))
    segs.append(((ax, hint_y), (bx, hint_y)))
    if abs(by - hint_y) > _EPS:
        segs.append(((bx, hint_y), b))
    # Drop zero-length segments (start/end already at hint_y).
    return [s for s in segs if _seg_len(s) > _EPS] or None


def _outside(c0: float, c1: float, lo: float, hi: float) -> list[float]:
    """Grid-snapped trunk coordinates just past `[lo, hi]` on each side — the
    rail a detour wraps around a keep-out on. Returned only on the side(s) the
    two pins don't already sit outside of, so a route that already clears the
    box isn't handed a pointless wrap."""
    out: list[float] = []
    left = round((lo - _GRID) / _GRID) * _GRID
    if left >= lo:
        left -= _GRID
    right = round((hi + _GRID) / _GRID) * _GRID
    if right <= hi:
        right += _GRID
    if min(c0, c1) > left:
        out.append(left)
    if max(c0, c1) < right:
        out.append(right)
    return out


def _keepout_detours(a: Pt, b: Pt, keepouts: list[Rect]) -> list[list[Seg]]:
    """Z-routes whose trunk is pushed just *outside* a keep-out — the way
    around the IC the between-the-pins L/Z candidates can't express. The
    local candidates only place a vertical trunk at an x between the two pins
    (all inside a wide IC) and a horizontal trunk at a y between them; when
    both pins straddle the chip, every one of those crosses it. Here we add,
    per keep-out, a vertical trunk just left/right of the box and a horizontal
    trunk just above/below it. The scorer still chooses — a wrap only wins
    when it actually dodges the keep-out's `_W_KEEPOUT` and doesn't short."""
    ax, ay = a
    bx, by = b
    out: list[list[Seg]] = []
    for x0, y0, x1, y1 in keepouts:
        for tx in _outside(ax, bx, x0, x1):       # vertical trunk beside the box
            route = [(a, (tx, ay)), ((tx, ay), (tx, by)), ((tx, by), b)]
            out.append([s for s in route if _seg_len(s) > _EPS])
        for ty in _outside(ay, by, y0, y1):       # horizontal trunk above/below
            route = [(a, (ax, ty)), ((ax, ty), (bx, ty)), ((bx, ty), b)]
            out.append([s for s in route if _seg_len(s) > _EPS])
    return [r for r in out if r]


def _route_edge(
    a: Pt, b: Pt, net: str, placed: list[tuple[str, Seg]],
    blocked: list[Pt], foreign_pins: list[Pt], bodies: list[Rect],
    keepouts: list[Rect], hint_y: float | None = None,
) -> list[Seg]:
    """Best orthogonal route a→b: corners clear of `blocked`, then lowest
    score (foreign overlap / over-pin ≫ crossings ≫ body intrusion ≫ length).
    Falls back to the best illegal route only if every candidate has a
    blocked corner.

    `hint_y` (optional) pins the horizontal trunk to a specific Y — used for
    staircase taps where the IC pin's Y is the only safe trunk row, above
    the row of tap bodies. The hint is *one extra candidate*; the scorer
    still chooses, so a hinted route that overlaps a foreign wire loses to
    a non-hinted one that doesn't."""
    cands = _candidates(a, b)
    if hint_y is not None:
        hinted = _hint_route(a, b, hint_y)
        if hinted is not None:
            cands = [hinted, *cands]
    if keepouts:
        cands = cands + _keepout_detours(a, b, keepouts)
    legal = [
        r for r in cands
        if not any(_same(c, p) for c in _corners(r) for p in blocked)
    ]
    return min(legal or cands,
               key=lambda r: _score(r, net, placed, foreign_pins,
                                    bodies, keepouts))


def _route_tree(
    upts: list[Pt], net: str, placed: list[tuple[str, Seg]],
    blocked: list[Pt], foreign_pins: list[Pt], bodies: list[Rect],
    keepouts: list[Rect], hint_y: float | None = None,
) -> tuple[list[Seg], list[Pt]]:
    """Wire `upts` as a crossing-aware spanning tree, grown edge by edge:
    each step adds the out-of-tree pin whose best route to the in-tree pins
    scores lowest — so the tree topology itself dodges crossings, not just
    the elbow of a Manhattan-shortest edge. Appends routed segments to
    `placed` in place (so later edges, of this net and the next, route
    around what is already settled); returns (segments, corners).

    `hint_y`, if given, biases each edge toward routing its horizontal
    trunk at that Y (see `_route_edge`). Useful for staircase taps where the
    IC pin's Y is the only safe trunk row.
    """
    n = len(upts)
    if n < 2:
        return [], []
    in_tree = [False] * n
    in_tree[0] = True
    segs: list[Seg] = []
    corners: list[Pt] = []
    for _ in range(n - 1):
        best: tuple[float, list[Seg], int] | None = None
        for i in range(n):
            if not in_tree[i]:
                continue
            for j in range(n):
                if in_tree[j]:
                    continue
                route = _route_edge(upts[i], upts[j], net, placed,
                                    blocked, foreign_pins, bodies, keepouts,
                                    hint_y)
                sc = _score(route, net, placed, foreign_pins, bodies, keepouts)
                if best is None or sc < best[0]:
                    best = (sc, route, j)
        assert best is not None
        _sc, route, j = best
        in_tree[j] = True
        segs.extend(route)
        corners.extend(_corners(route))
        placed.extend((net, s) for s in route)
    return segs, corners


# --- orchestrator -----------------------------------------------------------

@dataclass
class RoutedNet:
    name: str
    segments: list[Seg]


def route_nets(
    nets: list[tuple[str, list[Pt]]],
    all_pins: list[Pt],
    bodies: list[Rect] | None = None,
    rail_y_hints: dict[str, float] | None = None,
    keepouts: list[Rect] | None = None,
) -> list[RoutedNet]:
    """Route every net. `nets` is (name, pin-coords); `all_pins` is every pin
    in the schematic (corners must dodge foreign ones); `bodies` are component
    bounding boxes to route around (soft). `keepouts` are hard no-go rects (the
    IC body): a segment crossing one is penalised an order above a crossing, so
    the router detours around it but never trades a short to do so. Returns one
    `RoutedNet` per input net.

    `rail_y_hints` (optional): per-net Y values that the router should prefer
    as the horizontal-trunk row for that net. Each edge of a hinted net gets
    a Z-route candidate at that Y as an extra option in the scorer; the
    scorer still picks freely, so the hint only wins when it doesn't
    introduce new crossings or overlaps. Used for the signal-staircase
    archetype, where bodies sit on a row below the IC but each net's wire
    needs to run at the IC pin's Y to clear the row."""
    bodies = list(bodies or [])
    keepouts = list(keepouts or [])
    rail_y_hints = rail_y_hints or {}
    placed: list[tuple[str, Seg]] = []   # (net, segment) routed so far
    placed_corners: list[Pt] = []        # corners of prior nets
    routed: dict[str, list[Seg]] = {}
    # Pins of the nets being routed — "wired" pins; a segment over one of
    # these (foreign to its net) shorts. Geometry-free pins are not in here.
    wired_pins = [p for _, pts in nets for p in pts]

    # Small/local nets first; large spanning nets route around the settled ones.
    for name, pts in sorted(nets, key=lambda n: _span(n[1])):
        upts = _dedup(pts)
        routed[name] = []
        if len(upts) < 2:
            continue
        foreign_all = [
            p for p in all_pins if not any(_same(p, o) for o in upts)
        ]
        foreign_wired = [
            p for p in wired_pins if not any(_same(p, o) for o in upts)
        ]
        blocked = foreign_all + placed_corners
        segs, corners = _route_tree(
            upts, name, placed, blocked, foreign_wired, bodies, keepouts,
            hint_y=rail_y_hints.get(name),
        )
        routed[name] = segs
        placed_corners.extend(corners)

    return [RoutedNet(name, routed[name]) for name, _ in nets]


def count_crossings(routed: list[RoutedNet]) -> int:
    """Wire-wire crossings between segments of *different* nets — the
    readability metric a clean schematic drives to zero."""
    tagged = [(rn.name, s) for rn in routed for s in rn.segments]
    n = 0
    for i in range(len(tagged)):
        for j in range(i + 1, len(tagged)):
            if tagged[i][0] != tagged[j][0] and _crosses(tagged[i][1],
                                                         tagged[j][1]):
                n += 1
    return n


def count_overlaps(routed: list[RoutedNet]) -> int:
    """Collinear overlaps between segments of *different* nets — each one is a
    short. Must be zero; a non-zero count is a routing/placement failure."""
    tagged = [(rn.name, s) for rn in routed for s in rn.segments]
    n = 0
    for i in range(len(tagged)):
        for j in range(i + 1, len(tagged)):
            if tagged[i][0] != tagged[j][0] and _overlap(tagged[i][1],
                                                         tagged[j][1]):
                n += 1
    return n
