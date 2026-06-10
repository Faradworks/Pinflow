"""Smoke-test the LLM-direct placer on netlists that are NOT in the
5-golden corpus (i.e. unseen by the few-shot library).

Compares:
  - cplace baseline (deterministic)
  - cplace with LLM-direct placer (multi-shot + visual self-review)

Renders both to `_renders/unseen.{name}.cplace.png` and
`_renders/unseen.{name}.llm_direct.png` for visual comparison.

Usage:
    cd services/api
    .venv/bin/python scripts/eval_unseen.py
    .venv/bin/python scripts/eval_unseen.py --only tps628436
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
from pinflow_api.emit.placers.llm_placer import plan_layout_constraints  # noqa: E402
from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.netlist_to_sch import PlacerError  # noqa: E402
from pinflow_api.emit.render import render_schematic  # noqa: E402
from pinflow_api.emit.rubric import score  # noqa: E402

API_DIR = Path(__file__).resolve().parents[1]
FIXTURES = API_DIR / "tests" / "fixtures"
RENDERS = API_DIR / "_renders"

# Unseen netlists (not in the 5-golden corpus).
UNSEEN: list[dict] = [
    {
        "name": "tps628436",
        "netlist": FIXTURES / "tps628436.netlist.json",
        "symbols": None,
        "note": "TPS62843 buck regulator (unseen family)",
    },
    {
        "name": "rp2040",
        "netlist": FIXTURES / "rp2040.netlist.json",
        "symbols": None,
        "note": "RP2040 microcontroller (very different topology)",
    },
]


def _discover(symbols_dir: Path | None) -> None:
    if symbols_dir and symbols_dir.is_dir():
        try:
            ksa.get_symbol_cache().discover_libraries([str(symbols_dir)])
        except Exception as e:  # noqa: BLE001
            print(f"  warn: register {symbols_dir}: {e}", file=sys.stderr)


def _run_one(entry: dict) -> dict:
    name = entry["name"]
    _discover(entry.get("symbols"))
    netlist = Netlist.model_validate(
        json.loads(entry["netlist"].read_text())
    )

    print(f"\n{'='*78}\n  unseen: {name}  ·  {entry['note']}\n{'='*78}")

    # --- cplace baseline ---
    t0 = time.perf_counter()
    try:
        base = cplace(netlist, title=name)
        base_rb = score(base.sch_text, netlist)
    except Exception as e:  # noqa: BLE001
        return {"name": name, "error": f"baseline: {e}"}
    base_t = time.perf_counter() - t0
    print(f"  baseline:  {base_rb.total:.3f}  ({base_t:.2f}s)")
    RENDERS.mkdir(parents=True, exist_ok=True)
    render_schematic(base.sch_text, RENDERS / f"unseen.{name}.cplace.png",
                     dpi=200)

    # --- LLM-direct ---
    t0 = time.perf_counter()
    llm_err = None
    llm_rb = None
    try:
        llm = cplace(netlist, title=name,
                     external_emitter=plan_layout_constraints)
        llm_rb = score(llm.sch_text, netlist)
    except PlacerError as e:
        llm_err = "; ".join(e.errors)
    except Exception as e:  # noqa: BLE001
        llm_err = f"{type(e).__name__}: {e}"
    llm_t = time.perf_counter() - t0
    if llm_rb:
        print(f"  llm-direct: {llm_rb.total:.3f}  ({llm_t:.2f}s)")
        render_schematic(llm.sch_text, RENDERS / f"unseen.{name}.llm_direct.png",
                         dpi=200)
    else:
        print(f"  llm-direct: FAILED — {llm_err}  ({llm_t:.2f}s)")

    return {
        "name": name,
        "baseline": base_rb.to_dict(),
        "llm_direct": llm_rb.to_dict() if llm_rb else None,
        "llm_err": llm_err,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", metavar="NAME")
    args = ap.parse_args()

    entries = UNSEEN
    if args.only:
        entries = [e for e in entries if e["name"] == args.only]
        if not entries:
            print(f"error: no unseen entry {args.only!r}", file=sys.stderr)
            return 1

    reports = [_run_one(e) for e in entries]

    print(f"\n{'='*78}\n SUMMARY\n{'='*78}")
    print(f"  {'name':<14}{'cplace':>10}{'LLM-direct':>12}{'Δ':>10}")
    for r in reports:
        if "error" in r:
            print(f"  {r['name']:<14} ERROR: {r['error']}")
            continue
        b = r["baseline"]["total"]
        L = (r.get("llm_direct") or {}).get("total")
        ls = "—" if L is None else f"{L:.3f}"
        ds = "—" if L is None else f"{L-b:+.3f}"
        print(f"  {r['name']:<14}{b:>10.3f}{ls:>12}{ds:>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
