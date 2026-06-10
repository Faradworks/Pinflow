"""Replay a Netlist JSON through the placer and write the result as a
standalone `.kicad_sch` you can open directly in KiCad's schematic editor.

This is the iteration loop for `emit/netlist_to_sch.py`:
    edit placer → run this on a fixture → open the .replayed.kicad_sch in
    KiCad → eyeball / diff → tweak placer → repeat.

The Netlist IR is *position-free*, so this is NOT a byte-faithful round-trip
of the source schematic — geometry, wire routing, and label rotation are all
synthesized fresh by the placer. What survives is the topology (parts + nets
+ endpoints), and that's what the structural diff confirms.

Usage:
    cd services/api
    .venv/bin/python scripts/netlist_to_sch.py /abs/path/to/foo.netlist.json
    # → writes /abs/path/to/foo.replayed.kicad_sch
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.netlist_to_sch import PlacerError, place  # noqa: E402
from pinflow_api.emit.structural_diff import validate_placer_output  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("netlist", type=Path, help="Path to a Netlist JSON file")
    ap.add_argument(
        "-o", "--out", type=Path,
        help="Output .kicad_sch path (default: <netlist>.replayed.kicad_sch)",
    )
    ap.add_argument(
        "--title", default=None,
        help="Title for the placed block rectangle (default: input filename stem)",
    )
    ap.add_argument(
        "--symbols", type=Path, default=None,
        help=(
            "Directory of .kicad_sym files to register with kicad-sch-api "
            "before placing. Auto-detects <netlist>.symbols/ next to the input."
        ),
    )
    args = ap.parse_args()

    nl_path = args.netlist.resolve()
    if not nl_path.is_file():
        print(f"error: {nl_path} not found", file=sys.stderr)
        return 1

    out_path = args.out or nl_path.with_name(nl_path.stem + ".replayed.kicad_sch")
    title = args.title or nl_path.stem

    symbols_dir = args.symbols
    if symbols_dir is None:
        # Default companion: <netlist>.symbols/ written by sch_to_netlist.py.
        guess = nl_path.with_suffix(".symbols")
        if guess.is_dir():
            symbols_dir = guess

    if symbols_dir is not None and symbols_dir.is_dir():
        import kicad_sch_api as ksa
        try:
            ksa.get_symbol_cache().discover_libraries([symbols_dir])
            sym_files = sorted(symbols_dir.glob("*.kicad_sym"))
            print(f"  registered {len(sym_files)} sidecar lib(s) from {symbols_dir}/")
        except Exception as exc:
            print(f"  ! discover_libraries failed: {exc}", file=sys.stderr)

    nl = Netlist.model_validate(json.loads(nl_path.read_text()))
    self_errors = nl.validate_self()
    if self_errors:
        print("error: netlist self-validation failed:", file=sys.stderr)
        for e in self_errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    try:
        result = place(nl, title=title)
    except PlacerError as e:
        print("PlacerError:", file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        return 3

    out_path.write_text(result.sch_text)
    print(f"wrote {out_path}  ({len(result.sch_text):,} bytes)")
    print(f"  placed_refs={len(result.placed_refs)}  labels={len(result.label_specs)}")
    if result.issues:
        print(f"  placer issues ({len(result.issues)}):")
        for issue in result.issues:
            print(f"    - {issue}")

    vr = validate_placer_output(nl, result)
    print(f"  structural validation: ok={vr.ok}")
    if vr.errors:
        print(f"  errors ({len(vr.errors)}):")
        for err in vr.errors:
            print(f"    - {err}")
    if vr.warnings:
        print(f"  warnings ({len(vr.warnings)}):")
        for w in vr.warnings:
            print(f"    - {w}")

    return 0 if vr.ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
