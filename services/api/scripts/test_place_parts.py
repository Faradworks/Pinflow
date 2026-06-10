"""Debug/sanity harness for the parts-only placer (generate-path stage 1a).

    cd services/api
    .venv/bin/python scripts/test_place_parts.py            # built-in buck netlist
    .venv/bin/python scripts/test_place_parts.py nl.json     # a netlist JSON dump
    .venv/bin/python scripts/test_place_parts.py --pdf-render # also kicad-cli PDF
    .venv/bin/python scripts/test_place_parts.py --png        # auto-cropped PNG screenshot
    .venv/bin/python scripts/test_place_parts.py --png --open # ...and open it (macOS)
    .venv/bin/python scripts/test_place_parts.py --png --bbox # overlay measured bboxes

No LLM, no network — `place_parts` is pure. Verifies the three properties the
stage promises: (1) every part placed, no labels/wires (parts-only),
(2) placement is deterministic (same netlist → same `placed_refs`),
(3) no two parts' measured bounding boxes overlap. Writes the staged
`.kicad_sch` to /tmp so it can be opened in KiCad / KiCanvas for eyeballing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import kicad_sch_api as ksa

from pinflow_api.builders._common import sch_to_string
from pinflow_api.emit import bbox, layout
from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.netlist_to_sch import place_parts
from pinflow_api.emit.render import RenderError, render_schematic

KCLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"

# Same gitignored output dir render_sch.py uses (services/api/_renders/).
# __file__ is services/api/scripts/test_place_parts.py.
_RENDERS = Path(__file__).resolve().parent.parent / "_renders"

# Project-local scratch dir for the staged .kicad_sch / .pdf dumps (gitignored)
# — keeps debug artifacts inside the repo tree instead of the OS /tmp.
_TMP = Path(__file__).resolve().parent.parent / "_tmp"

# A representative buck cohort: a multi-unit regulator + the passives a
# design_spec netlist would carry. mpn on U1 mimics the generate-path shape.
_DEFAULT = {
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
        {"name": "+5V", "is_power": True, "is_port": True,
         "endpoints": [{"ref": "U1", "pin": "1"}, {"ref": "C1", "pin": "1"}]},
        {"name": "+3V3", "is_power": True, "is_port": True,
         "endpoints": [{"ref": "U1", "pin": "6"}, {"ref": "C2", "pin": "1"},
                       {"ref": "R1", "pin": "1"}]},
        {"name": "GND", "is_power": True, "is_port": True,
         "endpoints": [{"ref": "U1", "pin": "3"}, {"ref": "C1", "pin": "2"},
                       {"ref": "C2", "pin": "2"}, {"ref": "R2", "pin": "2"}]},
        {"name": "SW", "endpoints": [{"ref": "U1", "pin": "2"},
                                     {"ref": "L1", "pin": "1"}]},
        {"name": "FB", "endpoints": [{"ref": "U1", "pin": "4"},
                                     {"ref": "R1", "pin": "2"},
                                     {"ref": "R2", "pin": "1"}]},
    ],
}


def _agg_bboxes(sch_text: str) -> dict[str, tuple]:
    """Reload the placed schematic and union measured bboxes per refdes."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False
    ) as f:
        p = f.name
        f.write(sch_text)
    try:
        sch = ksa.load_schematic(p)
    finally:
        os.unlink(p)
    per_ref: dict[str, list] = {}
    for c in sch.components:
        b = bbox.measured_bbox(c)
        if b is not None:
            per_ref.setdefault(c.reference, []).append(b)
    agg: dict[str, tuple] = {}
    for ref, bs in per_ref.items():
        agg[ref] = (
            min(b[0] for b in bs), min(b[1] for b in bs),
            max(b[2] for b in bs), max(b[3] for b in bs),
        )
    return agg


def _overlap(a: tuple, b: tuple) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def main() -> None:
    args = [a for a in sys.argv[1:]]
    pdf_render = "--pdf-render" in args
    png_render = "--png" in args
    open_png = "--open" in args
    show_bbox = "--bbox" in args  # overlay measured bboxes on the render
    args = [a for a in args if not a.startswith("--")]
    payload = json.loads(Path(args[0]).read_text()) if args else _DEFAULT

    nl = Netlist.model_validate(payload)
    print(f"[1/4] netlist: {len(nl.parts)} parts, {len(nl.nets)} nets")

    r1 = place_parts(nl, title="parts-bin sanity")
    r2 = place_parts(nl, title="parts-bin sanity")

    # (2) determinism — placed_refs, not raw text (ksa UUIDs are random).
    det = r1.placed_refs == r2.placed_refs
    print(f"[2/4] deterministic placed_refs: {det}")
    if not det:
        print("      MISMATCH:", r1.placed_refs, "!=", r2.placed_refs)

    # (1) parts-only — no labels emitted.
    parts_only = not r1.label_specs
    print(f"[3/4] parts-only (no labels): {parts_only}  issues={r1.issues}")

    # Merge into an empty target exactly like add_subcircuit_from_netlist
    # does, so the rendered artifact carries the labeled rectangle frame
    # (drawn at merge time, not by place_parts).
    import kicad_sch_api as ksa
    target = ksa.create_schematic("parts-bin")
    layout.merge_subcircuit(target, r1.sch_text, label="TPS628436 buck")
    merged = sch_to_string(target)
    has_rect = "(rectangle" in merged and "TPS628436 buck" in merged
    print(f"[+]   merged frame present (rectangle + label): {has_rect}")

    # (3) no overlapping measured bboxes (checked on the placer output).
    agg = _agg_bboxes(r1.sch_text)
    refs = list(agg)
    collisions = [
        (i, j)
        for x, i in enumerate(refs)
        for j in refs[x + 1:]
        if _overlap(agg[i], agg[j])
    ]
    print(f"[4/4] components={sorted(agg)}")
    for ref in sorted(agg):
        x0, y0, x1, y1 = agg[ref]
        print(f"      {ref:5} x[{x0:7.2f},{x1:7.2f}] y[{y0:7.2f},{y1:7.2f}]"
              f"  w={x1 - x0:6.2f} h={y1 - y0:6.2f}")
    print(f"      overlaps: {collisions or 'NONE'}")

    _TMP.mkdir(parents=True, exist_ok=True)
    out = _TMP / "pinflow_parts_bin.kicad_sch"
    out.write_text(merged)  # merged = grid + rectangle frame (clean stage)
    print(f"\nwrote {out}")

    # --bbox: overlay each component's measured bbox as a dashed red
    # rectangle on a render-only copy. The staged .kicad_sch above stays
    # clean — this only changes what the PDF/PNG artifacts show, so you
    # can eyeball the non-overlap property the harness asserts.
    render_src = merged
    if show_bbox:
        for ref in sorted(agg):
            x0, y0, x1, y1 = agg[ref]
            target.add_rectangle(
                start=(x0, y0), end=(x1, y1),
                stroke_width=0.2, stroke_type="dash",
                stroke_color=(255, 0, 0, 1.0),
            )
        render_src = sch_to_string(target)
        dbg = _TMP / "pinflow_parts_bin_bbox.kicad_sch"
        dbg.write_text(render_src)
        print(f"wrote {dbg}  (bbox overlay, {len(agg)} boxes)")

    if pdf_render:
        src_file = (
            _TMP / "pinflow_parts_bin_bbox.kicad_sch" if show_bbox else out
        )
        pdf = out.with_suffix(".pdf")
        cp = subprocess.run(
            [KCLI, "sch", "export", "pdf", "-o", str(pdf), str(src_file)],
            capture_output=True, text=True,
        )
        print(f"kicad-cli pdf -> {pdf} (rc={cp.returncode})")
        if cp.returncode != 0:
            print(cp.stderr)

    if png_render:
        png = _RENDERS / "pinflow_parts_bin.png"
        try:
            render_schematic(render_src, png)  # auto-cropped to drawn content
            print(f"png -> {png} ({png.stat().st_size // 1024} KB)")
            if open_png:
                subprocess.run(["open", str(png)], check=False)
        except RenderError as e:
            print(f"png render failed: {e}")

    ok = det and parts_only and has_rect and not collisions and not (
        {p.refdes for p in nl.parts} - set(agg)
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
