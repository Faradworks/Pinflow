"""LLM-direct placer vs. deterministic cplace baseline.

For each golden in the corpus:
  1. Score the hand-drawn golden (ceiling).
  2. Run cplace baseline (deterministic — fast, free, plateaus on dense ICs).
  3. Run cplace + LLM-direct (best-of-3) (slower, $, closes the hard cases).
  4. Print side-by-side scores; render both to `_renders/`.

Usage:
    cd services/api
    .venv/bin/python scripts/eval_llm_placer.py            # whole corpus
    .venv/bin/python scripts/eval_llm_placer.py --only tps61088
    .venv/bin/python scripts/eval_llm_placer.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.emit.placers.cplace import cplace  # noqa: E402
from pinflow_api.emit.placers.llm_placer import cplace_with_llm_best_of  # noqa: E402
from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.netlist_to_sch import PlacerError  # noqa: E402
from pinflow_api.emit.render import render_schematic  # noqa: E402
from pinflow_api.emit.rubric import score  # noqa: E402

FIXTURES = API_DIR / "tests" / "fixtures"
MANIFEST = FIXTURES / "golden_corpus.json"
RENDERS = API_DIR / "_renders"


def _discover(symbols_dir: Path | None) -> None:
    if symbols_dir and symbols_dir.is_dir():
        try:
            ksa.get_symbol_cache().discover_libraries([str(symbols_dir)])
        except Exception as e:  # noqa: BLE001
            print(f"  warn: register {symbols_dir}: {e}", file=sys.stderr)


def _load_netlist(path: Path) -> Netlist:
    return Netlist.model_validate(json.loads(path.read_text()))


def _run_one(entry: dict) -> dict:
    name = entry["name"]
    _discover(FIXTURES / entry["symbols"] if entry.get("symbols") else None)
    netlist = _load_netlist(FIXTURES / entry["netlist"])

    print(f"\n{'='*78}\n {name}  ·  {entry.get('note', '')}\n{'='*78}")

    # --- golden score (for ceiling reference) ---
    golden_text = (FIXTURES / entry["sch"]).read_text()
    golden_rb = score(golden_text, netlist)
    print(f"  golden:    {golden_rb.total:.3f}")

    # --- baseline cplace ---
    t0 = time.perf_counter()
    try:
        base = cplace(netlist, title=name)
        base_rb = score(base.sch_text, netlist)
    except Exception as e:  # noqa: BLE001
        return {"name": name, "error": f"baseline: {e}"}
    base_t = time.perf_counter() - t0
    print(f"  baseline:  {base_rb.total:.3f}  ({base_t:.2f}s)")
    RENDERS.mkdir(parents=True, exist_ok=True)
    render_schematic(base.sch_text, RENDERS / f"{name}.cplace.png", dpi=200)

    # --- LLM-direct (best of 3) ---
    t0 = time.perf_counter()
    llm_result = None
    llm_err = None
    llm_rb = None

    def _on_attempt(i, total):
        if total is None:
            print(f"    best_of[{i}]: failed")
        else:
            print(f"    best_of[{i}]: {total:.3f}")

    try:
        llm_result = cplace_with_llm_best_of(
            netlist, n=3, title=name, on_attempt=_on_attempt,
        )
        llm_rb = score(llm_result.sch_text, netlist)
    except PlacerError as e:
        llm_err = "; ".join(e.errors)
    except Exception as e:  # noqa: BLE001
        llm_err = f"{type(e).__name__}: {e}"
    llm_t = time.perf_counter() - t0
    if llm_rb:
        print(f"  llm-direct (best of 3): {llm_rb.total:.3f}  "
              f"({llm_t:.2f}s)")
        render_schematic(llm_result.sch_text,
                         RENDERS / f"{name}.llm_direct.png", dpi=200)
    else:
        print(f"  llm-direct: FAILED — {llm_err}  ({llm_t:.2f}s)")

    return {
        "name": name,
        "golden": golden_rb.to_dict(),
        "baseline": base_rb.to_dict(),
        "llm_direct": llm_rb.to_dict() if llm_rb else None,
        "llm_err": llm_err,
        "base_t": base_t, "llm_t": llm_t,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", metavar="NAME")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    goldens = json.loads(MANIFEST.read_text()).get("goldens", [])
    if args.only:
        goldens = [g for g in goldens if g.get("name") == args.only]
        if not goldens:
            print(f"error: no entry {args.only!r}", file=sys.stderr)
            return 1

    reports = [_run_one(g) for g in goldens]

    if args.json:
        print(json.dumps({"reports": reports}, indent=2))
    else:
        print(f"\n{'='*78}\n SUMMARY\n{'='*78}")
        print(f"  {'name':<14}{'golden':>10}{'cplace':>10}"
              f"{'LLM-direct':>12}")
        for r in reports:
            if "error" in r:
                print(f"  {r['name']:<14} ERROR: {r['error']}")
                continue
            g = (r.get("golden") or {}).get("total")
            b = r["baseline"]["total"]
            L = (r.get("llm_direct") or {}).get("total")
            gs = "—" if g is None else f"{g:.3f}"
            ls = "—" if L is None else f"{L:.3f}"
            print(f"  {r['name']:<14}{gs:>10}{b:>10.3f}{ls:>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
