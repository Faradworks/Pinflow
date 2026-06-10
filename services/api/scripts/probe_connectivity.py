"""Phase 0 of the wire-router exploration — establish KiCad's connectivity rules.

A collision-aware router's correctness model depends entirely on knowing
*exactly* when KiCad's netlister treats geometry as electrically connected.
The session has conflicting evidence, so this probe asks KiCad directly:
builds minimal schematics (each wire joining two real resistor pins, so it
forms a genuine multi-pin net) and reads back `kicad-cli sch export netlist`.

Q1 — two wires that CROSS (an X, no junction): do their nets merge?
     If NO, the router may let wires cross freely and only avoid junctions —
     a far easier problem than full crossing-avoidance.
Q2 — a wire passing OVER a foreign pin at midspan: does the pin join the net?
     If NO, the router may route a wire straight across a foreign pin.

    cd services/api && .venv/bin/python scripts/probe_connectivity.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.builders._common import sch_to_string  # noqa: E402
from pinflow_api.emit.netlist_to_sch import _pin_xy  # noqa: E402
from pinflow_api.kicad_cli import export_netlist  # noqa: E402
from pinflow_api.netlist import parse_kicadsexpr  # noqa: E402


def _nets(sch) -> dict[str, list[tuple[str, str]]]:
    """KiCad's own netlister output for `sch`: {net_name: [(ref, pin), …]}."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        f.write(sch_to_string(sch))
        tmp = Path(f.name)
    try:
        return parse_kicadsexpr(export_netlist(tmp)).nets
    finally:
        tmp.unlink(missing_ok=True)


def _net_of(nets: dict, ref: str) -> str | None:
    """Name of the net any of `ref`'s pins sits on."""
    for name, pins in nets.items():
        if any(r == ref for r, _p in pins):
            return name
    return None


def _R(sch, ref: str, pos: tuple[float, float]):
    return sch.components.add(lib_id="Device:R", reference=ref,
                              value="1k", position=pos)


def q1_crossing() -> None:
    """Wire A (R1—R2, horizontal) crosses wire B (R3—R4, vertical), no
    junction. R1/R3 on the same net ⇒ a bare crossing merges nets."""
    sch = ksa.create_schematic("q1")
    r1, r2 = _R(sch, "R1", (40.0, 100.0)), _R(sch, "R2", (110.0, 100.0))
    r3, r4 = _R(sch, "R3", (75.0, 55.0)), _R(sch, "R4", (75.0, 145.0))
    p1, p2 = _pin_xy(r1, "1"), _pin_xy(r2, "1")    # same Y → horizontal wire
    p3, p4 = _pin_xy(r3, "2"), _pin_xy(r4, "1")    # same X → vertical wire
    sch.add_wire(p1, p2)
    sch.add_wire(p3, p4)
    crosses = (min(p1[0], p2[0]) < p3[0] < max(p1[0], p2[0])
               and min(p3[1], p4[1]) < p1[1] < max(p3[1], p4[1]))
    nets = _nets(sch)
    na, nb = _net_of(nets, "R1"), _net_of(nets, "R3")
    print("Q1 — two wires crossing (X, no junction)")
    print(f"   wire A (R1—R2) horizontal at y={p1[1]}")
    print(f"   wire B (R3—R4) vertical   at x={p3[0]}")
    print(f"   wires geometrically cross at "
          f"({p3[0]}, {p1[1]}): {crosses}")
    print(f"   R1 net={na!r}")
    print(f"   R3 net={nb!r}")
    if not crosses:
        print("   INCONCLUSIVE — coordinates do not cross.\n")
    elif na is not None and na == nb:
        print("   ⇒ a bare crossing MERGES nets — the router must avoid "
              "crossings.\n")
    else:
        print("   ⇒ a bare crossing keeps nets SEPARATE — the router only "
              "needs to avoid junctions, not crossings.\n")


def q2_wire_over_pin() -> None:
    """Wire W (R4—R5, horizontal) passes over R3's pin 1 at midspan.
    R3 on W's net ⇒ a wire crossing a pin connects to it."""
    sch = ksa.create_schematic("q2")
    r4, r5 = _R(sch, "R4", (40.0, 100.0)), _R(sch, "R5", (110.0, 100.0))
    r3 = _R(sch, "R3", (75.0, 100.0))
    p4, p5 = _pin_xy(r4, "1"), _pin_xy(r5, "1")
    p3 = _pin_xy(r3, "1")
    sch.add_wire(p4, p5)                            # wire W
    on_w = (abs(p3[1] - p4[1]) < 0.01
            and min(p4[0], p5[0]) < p3[0] < max(p4[0], p5[0]))
    nets = _nets(sch)
    n3, n4 = _net_of(nets, "R3"), _net_of(nets, "R4")
    print("Q2 — wire passing over a foreign pin at midspan")
    print(f"   wire W (R4—R5) at y={p4[1]}; R3 pin1 at {p3}")
    print(f"   R3 pin1 lies on W: {on_w}")
    print(f"   R3 net={n3!r}")
    print(f"   R4 net={n4!r}")
    if not on_w:
        print("   INCONCLUSIVE — R3 pin1 is not on wire W.\n")
    elif n3 is not None and n3 == n4:
        print("   ⇒ a wire over a pin CONNECTS — the router must not route "
              "across foreign pins.\n")
    else:
        print("   ⇒ a wire over a pin does NOT connect — the router may "
              "route straight across foreign pins.\n")


def q3_corner_on_pin() -> None:
    """Net X (R1—R2) is routed as two segments meeting at a corner placed
    exactly on R3's pin (R3 is on net Y = R3—R4). R1 and R3 on one net ⇒ a
    wire corner landing on a foreign pin connects."""
    sch = ksa.create_schematic("q3")
    r1, r2 = _R(sch, "R1", (40.0, 60.0)), _R(sch, "R2", (130.0, 60.0))
    r3, r4 = _R(sch, "R3", (85.0, 100.0)), _R(sch, "R4", (85.0, 150.0))
    p1, p2 = _pin_xy(r1, "1"), _pin_xy(r2, "1")
    corner = _pin_xy(r3, "1")
    sch.add_wire(p1, corner)                        # net X, segment 1
    sch.add_wire(corner, p2)                        # net X, segment 2
    sch.add_wire(_pin_xy(r3, "2"), _pin_xy(r4, "1"))   # net Y
    nets = _nets(sch)
    nx, ny = _net_of(nets, "R1"), _net_of(nets, "R3")
    print("Q3 — a wire corner landing on a foreign pin")
    print(f"   net X corner placed on R3 pin1 at {corner}")
    print(f"   R1 net={nx!r}   R3 net={ny!r}")
    if nx is not None and nx == ny:
        print("   ⇒ corner-on-pin CONNECTS — the router must keep corners "
              "off foreign pins.\n")
    else:
        print("   ⇒ corner-on-pin does NOT connect (unexpected).\n")


def q4_t_junction() -> None:
    """A wire endpoint lands on the midspan of net X's wire (a T). R3 on net
    X ⇒ an endpoint touching a foreign wire connects."""
    sch = ksa.create_schematic("q4")
    r1, r2 = _R(sch, "R1", (40.0, 100.0)), _R(sch, "R2", (130.0, 100.0))
    r3 = _R(sch, "R3", (85.0, 140.0))
    p1, p2 = _pin_xy(r1, "1"), _pin_xy(r2, "1")
    sch.add_wire(p1, p2)                            # net X wire
    p3 = _pin_xy(r3, "1")
    tee = (p3[0], p1[1])                            # on X's wire, above R3
    sch.add_wire(p3, tee)                           # R3 stub, endpoint on X
    nets = _nets(sch)
    n1, n3 = _net_of(nets, "R1"), _net_of(nets, "R3")
    print("Q4 — a wire endpoint on a foreign wire's midspan (a T)")
    print(f"   R3 stub ends at {tee}, on wire X")
    print(f"   R1 net={n1!r}   R3 net={n3!r}")
    if n1 is not None and n1 == n3:
        print("   ⇒ T-junction CONNECTS — the router must keep endpoints off "
              "foreign wires.\n")
    else:
        print("   ⇒ T-junction does NOT connect (unexpected).\n")


def q5_collinear_overlap() -> None:
    """Two wires of different nets share a collinear, overlapping run. Merged
    ⇒ the router must never let a wire overlap a foreign wire."""
    sch = ksa.create_schematic("q5")
    r1, r2 = _R(sch, "R1", (40.0, 100.0)), _R(sch, "R2", (90.0, 100.0))
    r3, r4 = _R(sch, "R3", (75.0, 100.0)), _R(sch, "R4", (125.0, 100.0))
    p1, p2 = _pin_xy(r1, "1"), _pin_xy(r2, "1")
    p3, p4 = _pin_xy(r3, "1"), _pin_xy(r4, "1")
    sch.add_wire(p1, p2)                            # net X: x p1..p2
    sch.add_wire(p3, p4)                            # net Y: x p3..p4, same y
    nets = _nets(sch)
    nx, ny = _net_of(nets, "R1"), _net_of(nets, "R3")
    print("Q5 — two collinear wires of different nets overlapping")
    print(f"   X x∈[{p1[0]},{p2[0]}]  Y x∈[{p3[0]},{p4[0]}]  same y={p1[1]}")
    print(f"   R1 net={nx!r}   R3 net={ny!r}")
    if nx is not None and nx == ny:
        print("   ⇒ collinear overlap MERGES — the router must never overlap "
              "a foreign wire.\n")
    else:
        print("   ⇒ collinear overlap does NOT merge (unexpected).\n")


def main() -> int:
    q1_crossing()
    q2_wire_over_pin()
    q3_corner_on_pin()
    q4_t_junction()
    q5_collinear_overlap()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
