"""Invariant guard: no placer draws a wire through an IC body.

A wire that would span an IC side-to-side or cut through its body is dropped
for a same-named net label instead — the relabel pass in
`netlist_to_sch._place_connectivity`, shared by every placer (KiCad merges
same-named labels, so connectivity is preserved). This locks that in: for every
corpus entry, both production placers must emit ZERO wires through any IC body
(`rubric.count_ic_through_wires == 0`).

No LLM, no network — deterministic from the committed JSON.

Run: cd services/api && .venv/bin/python scripts/test_no_ic_wires.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.netlist_to_sch import PlacerError  # noqa: E402
from pinflow_api.emit.placers import get_placer  # noqa: E402
from pinflow_api.emit.rubric import count_ic_through_wires  # noqa: E402

FIX = API_DIR / "tests" / "fixtures"
PLACERS = ("cplace", "fdplace")


def _cases():
    """Yield (name, netlist_path, symbols_dir|None) for both corpora.

    Generated entries come FIRST: they resolve their chip symbols from the
    bundled KiCad libraries, and once a golden's sidecar lib (e.g. mt3608's
    `Regulator_Switching`, holding only the MT3608 symbol) is discovered it
    shadows the bundled lib of the same name for the rest of the process — so
    the bundled-symbol entries must run before any sidecar is registered."""
    for e in json.loads((FIX / "generated_corpus.json").read_text())["entries"]:
        yield (e["name"], FIX / e["netlist"],
               FIX / e["symbols"] if e.get("symbols") else None)
    for e in json.loads((FIX / "golden_corpus.json").read_text())["goldens"]:
        yield (e["name"], FIX / "golden" / f"{e['name']}.netlist.json",
               FIX / e["symbols"] if e.get("symbols") else None)


def main() -> int:
    failures: list[str] = []
    for name, npath, symdir in _cases():
        if symdir is not None:
            ksa.get_symbol_cache().discover_libraries([str(symdir)])
        netlist = Netlist.model_validate(json.loads(npath.read_text()))
        for placer_name in PLACERS:
            try:
                res = get_placer(placer_name)(netlist, title=name)
            except PlacerError as e:
                print(f"  [skip] {name:16} {placer_name:8} placer error: {e}")
                continue
            n = count_ic_through_wires(res.sch_text)
            print(f"  [{'ok ' if n == 0 else 'FAIL'}] {name:16} "
                  f"{placer_name:8} through-IC wires = {n}")
            if n:
                failures.append(f"{name}/{placer_name}: {n} wire(s) through an IC")
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all clear — no wires through any IC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
