"""Exercise the LLM refiner (pass 2) on a netlist — before/after, end to end.

Runs the deterministic placer (`place()`) for the baseline, then `refine()`
— the gated LLM re-layout — and renders both so you can eyeball the
difference. The connectivity gate (`structural_diff`) and ERC guard mean a
refined result is, by construction, no worse than the baseline; this script
just makes the *visible* improvement (or the fallback) easy to inspect.

    cd services/api
    .venv/bin/python scripts/test_refine.py tests/fixtures/golden/tps63020.netlist.json
    .venv/bin/python scripts/test_refine.py tests/fixtures/golden/tps63020.netlist.json --open

Hits the Anthropic API (one call per refiner attempt) — needs `.env` with
ANTHROPIC_API_KEY. `--symbols DIR` registers sidecar libraries; with no flag
it auto-detects `<netlist>.symbols/`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.netlist_to_sch import PlacerError, place  # noqa: E402
from pinflow_api.emit.refine import refine  # noqa: E402
from pinflow_api.emit.render import render_schematic  # noqa: E402
from pinflow_api.emit.structural_diff import validate_placer_output  # noqa: E402

_RENDERS = API_DIR / "_renders"


def main() -> int:
    args = sys.argv[1:]
    do_open = "--open" in args
    syms_arg = None
    if "--symbols" in args:
        syms_arg = Path(args[args.index("--symbols") + 1])
    positional = [a for a in args if not a.startswith("--")
                  and (syms_arg is None or a != str(syms_arg))]
    if not positional:
        print("usage: test_refine.py <netlist.json> [--symbols DIR] [--open]",
              file=sys.stderr)
        return 1
    nl_path = Path(positional[0]).resolve()
    if not nl_path.is_file():
        print(f"error: {nl_path} not found", file=sys.stderr)
        return 1

    syms = syms_arg
    if syms is None:
        guess = nl_path.with_suffix(".symbols")
        if guess.is_dir():
            syms = guess
    if syms is not None and syms.is_dir():
        ksa.get_symbol_cache().discover_libraries([str(syms)])
        print(f"registered sidecar libs from {syms}/")

    nl = Netlist.model_validate(json.loads(nl_path.read_text()))
    print(f"netlist: {len(nl.parts)} parts, {len(nl.nets)} nets")

    # --- pass 1: deterministic placer ------------------------------------
    try:
        baseline = place(nl, title=nl_path.stem)
    except PlacerError as e:
        print("PlacerError:", "; ".join(e.errors), file=sys.stderr)
        return 3
    base_vr = validate_placer_output(nl, baseline)
    print(f"[1] place(): structural ok={base_vr.ok}  "
          f"placed={len(baseline.placed_refs)}")

    # --- pass 2: gated LLM re-layout -------------------------------------
    print("[2] refine(): calling the LLM refiner ...")
    refined, report = refine(nl, baseline, extra_lib_path=syms)
    print(f"    attempts={report.attempts}  refined={report.refined}")
    for note in report.notes:
        print(f"    - {note}")

    _RENDERS.mkdir(exist_ok=True)
    # Dump the last build() the model emitted — lets you see what it produced
    # (and, on a fallback, what got rejected) without burning another call.
    if report.last_code:
        dump = _RENDERS / f"{nl_path.stem}.refined_attempt.py"
        dump.write_text(report.last_code)
        print(f"    last attempt's build() → {dump}")

    # --- render --------------------------------------------------------
    def _render(text: str, path: Path) -> bool:
        try:
            render_schematic(text, path)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"    render failed for {path.name}: {e}", file=sys.stderr)
            return False

    base_png = _RENDERS / f"{nl_path.stem}.baseline.png"
    refn_png = _RENDERS / f"{nl_path.stem}.refined.png"
    rendered = [base_png, refn_png]
    ok_b = _render(baseline.sch_text, base_png)
    ok_r = _render(refined.sch_text, refn_png)
    print(f"\nbaseline render: {base_png if ok_b else '(failed)'}")
    print(f"refined  render: {refn_png if ok_r else '(failed)'}"
          + ("" if report.refined else "  (== baseline; refiner fell back)"))
    # Always render the last attempt's actual schematic — on a fallback this
    # is the only way to see the orthogonalized re-layout the gate rejected.
    if not report.refined and report.last_sch:
        att_png = _RENDERS / f"{nl_path.stem}.refined_attempt.png"
        if _render(report.last_sch, att_png):
            print(f"last attempt render: {att_png}")
            rendered.append(att_png)
    if do_open and ok_b and ok_r:
        subprocess.run(["open", *[str(p) for p in rendered]], check=False)

    return 0  # fallback is a valid, safe outcome


if __name__ == "__main__":
    raise SystemExit(main())
