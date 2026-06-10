"""Unit checks for the orthogonal router (emit/route.py).

Synthetic, deterministic, no KiCad — verifies the three properties the
router must hold: every net's wires actually connect all its pins, crossings
are minimised when a choice exists, and corners never land on foreign pins.

    cd services/api && .venv/bin/python scripts/test_route.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pinflow_api.emit.route import (  # noqa: E402
    RoutedNet, _corners, _same, count_crossings, route_nets,
)

_fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _fails
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  — {detail}" if detail else ""))
    if not ok:
        _fails += 1


def _connected(rn: RoutedNet) -> bool:
    """True if `rn`'s segments form a single connected component (so every
    pin, an MST-edge endpoint, ends up on one net)."""
    parent: dict[tuple, tuple] = {}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    nodes = set()
    for (p, q) in rn.segments:
        a = (round(p[0], 3), round(p[1], 3))
        b = (round(q[0], 3), round(q[1], 3))
        union(a, b)
        nodes.add(a)
        nodes.add(b)
    return len(nodes) == 0 or len({find(n) for n in nodes}) == 1


def test_connectivity() -> None:
    """A 4-pin net must come out as one connected tree."""
    pins = [(0.0, 0.0), (40.0, 0.0), (0.0, 40.0), (40.0, 40.0)]
    routed = route_nets([("N", pins)], pins)
    rn = routed[0]
    check("4-pin net is fully connected", _connected(rn),
          f"{len(rn.segments)} segments")


def test_crossing_avoidance() -> None:
    """Net Y is a horizontal wire; net X must L past it. One elbow crosses Y,
    the other does not — the router must pick the clear one."""
    y = [(40.0, 50.0), (90.0, 50.0)]      # horizontal wire, x∈[40,90]
    x = [(60.0, 0.0), (20.0, 80.0)]       # L2 elbow at x=60 would cross Y
    routed = route_nets([("X", x), ("Y", y)], x + y)
    n = count_crossings(routed)
    check("router avoids the crossing elbow", n == 0,
          f"{n} cross-net crossings (a wrong elbow choice gives 1)")


def test_corner_legality() -> None:
    """X's horizontal-first L corner lands exactly on a foreign pin; the
    router must route X so no corner sits on that pin."""
    foreign = (40.0, 0.0)                 # a pin of net Y
    x = [(0.0, 0.0), (40.0, 40.0)]        # horiz-first L corner == foreign
    routed = route_nets([("X", x), ("Y", [foreign, (80.0, 0.0)])],
                        x + [foreign, (80.0, 0.0)])
    x_corners = _corners(routed[0].segments) if routed[0].segments else []
    # corners come in pairs per L/Z; flatten is already flat here
    on_pin = any(_same(c, foreign) for c in _all_corners(routed[0]))
    check("no corner on the foreign pin", not on_pin,
          f"corners={[tuple(round(v, 1) for v in c) for c in _all_corners(routed[0])]}")


def _all_corners(rn: RoutedNet) -> list:
    """Every interior vertex across the net's routed edges."""
    # segments are concatenated per edge; a corner is any point shared by two
    # consecutive segments that is not an edge's outer endpoint.
    segs = rn.segments
    corners = []
    for i in range(len(segs) - 1):
        if _same(segs[i][1], segs[i + 1][0]):
            corners.append(segs[i][1])
    return corners


def main() -> int:
    print("router unit checks")
    test_connectivity()
    test_crossing_avoidance()
    test_corner_legality()
    print(f"\n{'all passed' if _fails == 0 else f'{_fails} FAILED'}")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
