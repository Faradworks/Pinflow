"""Verify `place()` is at least as capable as `place_parts()` at PLACEMENT.

`place()` is now the placer for both agent paths (generate + replicate), so a
placement regression versus the parts-bin would silently degrade the generate
flow. This harness guards against that: across a suite of netlists covering
*both* of `place()`'s layout paths — single-IC pin-anchored and the 0/2+-IC
column fallback — it runs both placers and checks the three properties the
parts-bin promises:

    (1) every netlist part placed,
    (2) deterministic `placed_refs` (same netlist → same coordinates),
    (3) no two netlist parts' measured bounding boxes overlap.

`place()` must never be *worse* than `place_parts()` on (1)–(3). It must
additionally pass structural validation and emit connectivity (wires / power
symbols) — that's the "better". Power symbols are excluded from the overlap
check: `place_parts()` emits none, so the fair placement comparison is the
netlist parts only.

    cd services/api
    .venv/bin/python scripts/compare_placers.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.emit import bbox  # noqa: E402
from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.netlist_to_sch import (  # noqa: E402
    PlacerError,
    place,
    place_parts,
)
from pinflow_api.emit.structural_diff import validate_placer_output  # noqa: E402


# --- test netlists — cover both place() paths --------------------------------
# 1-IC buck (pin-anchored path); mirrors test_place_parts.py's _DEFAULT.
_BUCK = {
    "parts": [
        {"refdes": "U1", "lib_id": "Regulator_Switching:TPS628436DRL",
         "value": "TPS628436", "mpn": "TPS628436DRL"},
        {"refdes": "L1", "lib_id": "Device:L", "value": "2.2uH"},
        {"refdes": "C1", "lib_id": "Device:C", "value": "10uF"},
        {"refdes": "C2", "lib_id": "Device:C", "value": "22uF"},
        {"refdes": "C3", "lib_id": "Device:C", "value": "100nF"},
        {"refdes": "R1", "lib_id": "Device:R", "value": "100k"},
        {"refdes": "R2", "lib_id": "Device:R", "value": "31.6k"},
    ],
    "nets": [
        {"name": "+5V", "is_power": True,
         "endpoints": [{"ref": "U1", "pin": "1"}, {"ref": "C1", "pin": "1"}]},
        {"name": "+3V3", "is_power": True,
         "endpoints": [{"ref": "U1", "pin": "6"}, {"ref": "C2", "pin": "1"},
                       {"ref": "R1", "pin": "1"}]},
        {"name": "GND", "is_power": True, "voltage": 0.0,
         "endpoints": [{"ref": "U1", "pin": "3"}, {"ref": "C1", "pin": "2"},
                       {"ref": "C2", "pin": "2"}, {"ref": "R2", "pin": "2"}]},
        {"name": "SW", "endpoints": [{"ref": "U1", "pin": "2"},
                                     {"ref": "L1", "pin": "1"}]},
        {"name": "FB", "endpoints": [{"ref": "U1", "pin": "4"},
                                     {"ref": "R1", "pin": "2"},
                                     {"ref": "R2", "pin": "1"}]},
    ],
}
# 0 ICs — exercises the _place_columns fallback.
_NO_IC = {
    "parts": [
        {"refdes": "R1", "lib_id": "Device:R", "value": "10k"},
        {"refdes": "R2", "lib_id": "Device:R", "value": "1k"},
        {"refdes": "C1", "lib_id": "Device:C", "value": "100nF"},
    ],
    "nets": [
        {"name": "A", "endpoints": [{"ref": "R1", "pin": "1"},
                                    {"ref": "C1", "pin": "1"}]},
        {"name": "B", "endpoints": [{"ref": "R1", "pin": "2"},
                                    {"ref": "R2", "pin": "1"}]},
        {"name": "GND", "is_power": True, "voltage": 0.0,
         "endpoints": [{"ref": "R2", "pin": "2"}, {"ref": "C1", "pin": "2"}]},
    ],
}
# 2 ICs — also the _place_columns fallback (place() pin-anchors only 1 IC).
_TWO_IC = {
    "parts": [
        {"refdes": "U1", "lib_id": "Regulator_Switching:TPS628436DRL", "value": "U1"},
        {"refdes": "U2", "lib_id": "Regulator_Switching:TPS628436DRL", "value": "U2"},
        {"refdes": "C1", "lib_id": "Device:C", "value": "10uF"},
        {"refdes": "C2", "lib_id": "Device:C", "value": "10uF"},
    ],
    "nets": [
        {"name": "+5V", "is_power": True,
         "endpoints": [{"ref": "U1", "pin": "1"}, {"ref": "C1", "pin": "1"}]},
        {"name": "MID", "endpoints": [{"ref": "U1", "pin": "6"},
                                      {"ref": "U2", "pin": "1"}]},
        {"name": "+3V3", "is_power": True,
         "endpoints": [{"ref": "U2", "pin": "6"}, {"ref": "C2", "pin": "1"}]},
        {"name": "GND", "is_power": True, "voltage": 0.0,
         "endpoints": [{"ref": "U1", "pin": "3"}, {"ref": "U2", "pin": "3"},
                       {"ref": "C1", "pin": "2"}, {"ref": "C2", "pin": "2"}]},
    ],
}
# Single part, no nets — the degenerate edge.
_SINGLE = {"parts": [{"refdes": "R1", "lib_id": "Device:R", "value": "10k"}],
           "nets": []}


def _overlap(a: tuple, b: tuple) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _load(sch_text: str):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        p = f.name
        f.write(sch_text)
    try:
        return ksa.load_schematic(p)
    finally:
        os.unlink(p)


def _part_boxes(sch) -> dict[str, tuple]:
    """Per-refdes unioned measured bbox — netlist parts only (drops `#PWR*`)."""
    per: dict[str, list] = {}
    for c in sch.components:
        ref = str(c.reference)
        if ref.startswith("#"):
            continue
        b = bbox.measured_bbox(c)
        if b is not None:
            per.setdefault(ref, []).append(b)
    return {
        r: (min(b[0] for b in bs), min(b[1] for b in bs),
            max(b[2] for b in bs), max(b[3] for b in bs))
        for r, bs in per.items()
    }


def _placement_props(placer, nl: Netlist, title: str) -> dict:
    """Run `placer` twice; return the three parts-bin placement properties."""
    try:
        r1 = placer(nl, title=title)
        r2 = placer(nl, title=title)
    except PlacerError as e:
        return {"ok": False, "error": "PlacerError: " + "; ".join(e.errors)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    expected = {p.refdes for p in nl.parts}
    boxes = _part_boxes(_load(r1.sch_text))
    refs = list(boxes)
    collisions = [
        (refs[i], refs[j])
        for i in range(len(refs))
        for j in range(i + 1, len(refs))
        if _overlap(boxes[refs[i]], boxes[refs[j]])
    ]
    return {
        "ok": True,
        "result": r1,
        "all_placed": expected <= set(r1.placed_refs),
        "missing": sorted(expected - set(r1.placed_refs)),
        "deterministic": r1.placed_refs == r2.placed_refs,
        "no_overlap": not collisions,
        "collisions": collisions,
    }


def _run_case(name: str, payload: dict) -> bool:
    """Run one netlist through both placers; print the comparison; return the
    verdict (place() at least as good as place_parts())."""
    nl = Netlist.model_validate(payload)
    pp = _placement_props(place_parts, nl, name)
    pl = _placement_props(place, nl, name)

    print(f"\n=== {name}  ({len(nl.parts)} parts, {len(nl.nets)} nets) ===")

    def _line(tag: str, p: dict) -> None:
        if not p["ok"]:
            print(f"  {tag:11} FAILED — {p['error']}")
            return
        print(f"  {tag:11} placed={'Y' if p['all_placed'] else 'N'}"
              f"  deterministic={'Y' if p['deterministic'] else 'N'}"
              f"  no_overlap={'Y' if p['no_overlap'] else 'N'}")
        if p["missing"]:
            print(f"              missing: {p['missing']}")
        if p["collisions"]:
            print(f"              overlaps: {p['collisions']}")

    _line("place_parts", pp)
    _line("place()", pl)

    # place() must not be worse than place_parts on the three properties.
    not_worse = pl["ok"] and all(
        (not pp.get(k, False)) or pl.get(k, False)
        for k in ("all_placed", "deterministic", "no_overlap")
    )
    struct_ok = wired = None
    if pl["ok"]:
        r = pl["result"]
        struct_ok = validate_placer_output(nl, r).ok
        sch = _load(r.sch_text)
        n_wire = len(list(sch.wires.all()))
        n_pwr = sum(1 for c in sch.components
                    if str(c.reference).startswith("#PWR"))
        # connectivity is only expected when there are nets to render
        wired = (n_wire > 0 or n_pwr > 0) if nl.nets else True
        print(f"  place() extra: structural_ok={'Y' if struct_ok else 'N'}"
              f"  connectivity={'Y' if wired else 'N'}"
              f"  (wires={n_wire}, power_symbols={n_pwr})")

    verdict = bool(not_worse and struct_ok and wired)
    print(f"  → place() >= place_parts(): {'PASS' if verdict else 'FAIL'}")
    return verdict


def main() -> int:
    # Built-in netlists use KiCad's bundled `Device` / `Regulator_Switching`
    # libraries — run them BEFORE registering any sidecar, since a sidecar
    # `Device.kicad_sym` (a golden's sidecar carries only the symbols that
    # schematic used) shadows the bundled `Device` library process-wide.
    all_ok = True
    for name, payload in [
        ("buck — 1 IC (pin-anchored)", _BUCK),
        ("no IC — 3 passives (fallback)", _NO_IC),
        ("two ICs (fallback)", _TWO_IC),
        ("single part, no nets", _SINGLE),
    ]:
        all_ok &= _run_case(name, payload)

    ex = API_DIR / "tests" / "fixtures" / "golden" / "tps63020.netlist.json"
    if ex.is_file():
        syms = API_DIR / "tests" / "fixtures" / "golden" / "tps63020.netlist.symbols"
        if syms.is_dir():
            try:
                ksa.get_symbol_cache().discover_libraries([str(syms)])
            except Exception as e:  # noqa: BLE001
                print(f"(could not register {syms}: {e})", file=sys.stderr)
        all_ok &= _run_case("tps63020.kicad_sch — 1 IC",
                            json.loads(ex.read_text()))

    print(f"\n{'=' * 60}")
    print("RESULT:", "PASS — place() matches or beats place_parts() everywhere"
          if all_ok else "FAIL — place() regressed vs place_parts()")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
