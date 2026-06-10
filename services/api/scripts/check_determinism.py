"""Verify cplace is byte-deterministic across runs.

Re-runs cplace on each golden in the corpus N times and asserts every run
produces the same `(kicad_sch ...)` text. cplace is constraint-based and
designed to be deterministic; this script is a regression net for
nondeterminism creeping in via set iteration order, hash randomization,
dict ordering, or floating-point ordering changes.

Greedy is NOT tested — it uses BFS visit order and is not guaranteed
byte-stable across Python versions / dict orderings. If you want to add
greedy here, expect occasional false positives. Cplace is the contract.

Usage:
    cd services/api
    .venv/bin/python scripts/check_determinism.py                # all goldens
    .venv/bin/python scripts/check_determinism.py --only tps61088 # one
    .venv/bin/python scripts/check_determinism.py --runs 5       # default 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.placers.cplace import cplace  # noqa: E402

FIXTURES = API_DIR / "tests" / "fixtures"
MANIFEST = FIXTURES / "golden_corpus.json"
DEFAULT_RUNS = 20


_UUID_RE = __import__("re").compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'
)


def _sig(sch_text: str) -> str:
    """Hash the schematic with UUIDs normalised. ksa regenerates random
    UUIDs per symbol / wire / junction on every emit — those changes are
    KiCad-required (no two placements share a UUID) and don't reflect
    placement nondeterminism. Strip them before hashing so the hash
    reflects geometry + topology only."""
    canon = _UUID_RE.sub("UUID", sch_text)
    return hashlib.sha256(canon.encode()).hexdigest()


def _run_one(name: str, netlist: Netlist, syms_dir: Path | None,
              runs: int) -> tuple[bool, int, str]:
    if syms_dir is not None and syms_dir.is_dir():
        ksa.get_symbol_cache().discover_libraries([str(syms_dir.resolve())])
    hashes: set[str] = set()
    last = ""
    for _ in range(runs):
        r = cplace(netlist, title=name)
        h = _sig(r.sch_text)
        hashes.add(h)
        last = h
    return len(hashes) == 1, len(hashes), last[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", metavar="NAME",
                    help="check just this corpus entry")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                    help=f"runs per golden (default {DEFAULT_RUNS})")
    args = ap.parse_args()

    if not MANIFEST.is_file():
        print(f"error: corpus manifest not found at {MANIFEST}",
              file=sys.stderr)
        return 1
    goldens = json.loads(MANIFEST.read_text()).get("goldens", [])
    if args.only:
        goldens = [g for g in goldens if g.get("name") == args.only]
        if not goldens:
            print(f"error: no corpus entry named {args.only!r}",
                  file=sys.stderr)
            return 1

    all_ok = True
    for entry in goldens:
        name = entry["name"]
        netlist = Netlist.model_validate(json.loads(
            (FIXTURES / entry["netlist"]).read_text()
        ))
        syms = FIXTURES / entry["symbols"] if entry.get("symbols") else None
        ok, n_distinct, h = _run_one(name, netlist, syms, args.runs)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:<12} {args.runs} runs → "
              f"{n_distinct} distinct  ({h})")
        if not ok:
            all_ok = False

    print()
    print("deterministic" if all_ok else "NONDETERMINISTIC — runs diverged")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
