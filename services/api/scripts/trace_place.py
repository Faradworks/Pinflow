"""Step-by-step trace of the netlist→schematic placer, for debugging.

Sibling to `scripts/trace_chat.py` (which traces the agent loop). Where that
script opens up the LLM tool-use loop, this one opens up `emit.netlist_to_sch`
— the deterministic placer that turns a `Netlist` IR into a placed
`.kicad_sch`. It prints every intermediate artifact the placer computes so
you can see *where* a bad layout comes from instead of only seeing the final
schematic.

The placer is a single pure function (`place()`), so this is not a trace
*sink* threaded through production code — it re-walks the same public/helper
functions the placer uses (`_bucket_parts`, `_split_decouplers`,
`_real_unit_count`) for the display-only stages, and reads `placed_refs`,
`issues`, and `label_specs` straight off the real `PlacerResult`. So every
number shown is the placer's own — nothing is re-derived approximately.

Stages printed (each is an "artifact"):
    0  input netlist           parts + nets tables
    1  self-validation         Netlist.validate_self() — gate
    2  symbol resolution       per lib_id: found in libs? unit count?
    3  bucketing               ICs / connectors / per-IC cohorts + decouplers
    4  placement               placed_refs (refdes → x,y) + placer issues
    5  power symbols           which rails got a power:* symbol vs labels-only
    6  labels                  label_specs (ref, pin, net, position)
    7  serialized output       the .kicad_sch written to disk
    8  structural validation   validate_placer_output() — round-trip check
    9  render                  PNG screenshot of the placed schematic

Usage:
    cd services/api
    .venv/bin/python scripts/trace_place.py tests/fixtures/golden/tps63020.netlist.json
    .venv/bin/python scripts/trace_place.py foo.netlist.json --step      # pause per stage
    .venv/bin/python scripts/trace_place.py foo.netlist.json --open      # open the PNG
    .venv/bin/python scripts/trace_place.py foo.netlist.json --no-render  # skip stage 9

`--symbols DIR` registers sidecar `.kicad_sym` libraries before placing; with
no flag it auto-detects `<netlist>.symbols/` next to the input (the layout
`sch_to_netlist.py` writes), so a netlist using project-local symbols just
works. `--step` single-steps: press Enter to advance, `q` to stop. It is
auto-disabled when stdin is not a TTY.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]   # services/api
sys.path.insert(0, str(API_DIR))

from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.classify import classify  # noqa: E402
from pinflow_api.emit.netlist_to_sch import (  # noqa: E402
    PlacerError,
    PlacerResult,
    _power_lib_id,
    _real_unit_count,
    place,
)
from pinflow_api.emit.structural_diff import validate_placer_output  # noqa: E402


# --- tiny presentation helpers ------------------------------------------------

_STEP = False  # set from --step; gated on TTY


def header(n: int, title: str) -> None:
    bar = "=" * 72
    print(f"\n{bar}\n  STEP {n} — {title}\n{bar}")


def pause() -> None:
    """Block until the user advances. No-op unless --step and a TTY."""
    if not _STEP:
        return
    try:
        ans = input("    [Enter] continue · [q] quit  ").strip().lower()
    except EOFError:
        return
    if ans == "q":
        print("aborted.")
        raise SystemExit(0)


def table(rows: list[list[str]], headers: list[str]) -> None:
    """Print a left-aligned fixed-width table."""
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    fmt = "    " + "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*[str(c) for c in row]))


# --- stages -------------------------------------------------------------------

def stage_input(nl: Netlist, path: Path) -> None:
    header(0, "INPUT NETLIST")
    print(f"  source: {path}")
    print(f"  parts={len(nl.parts)}  nets={len(nl.nets)}  "
          f"ports={len(nl.ports())}")
    print()
    table(
        [[p.refdes, p.lib_id, p.value or "—", p.mpn or "—"] for p in nl.parts],
        ["refdes", "lib_id", "value", "mpn"],
    )
    print()
    table(
        [[
            n.name,
            "pwr" if n.is_power else "",
            f"{n.voltage}V" if n.voltage is not None else "",
            "port" if n.is_port else "",
            str(len(n.endpoints)),
        ] for n in nl.nets],
        ["net", "kind", "V", "port", "#ep"],
    )
    pause()


def stage_self_validation(nl: Netlist) -> None:
    header(1, "SELF-VALIDATION  (Netlist.validate_self)")
    errors = nl.validate_self()
    if not errors:
        print("    ok — no structural errors")
    else:
        print(f"    {len(errors)} error(s) — placer will raise PlacerError:")
        for e in errors:
            print(f"      - {e}")
    pause()


def stage_symbols(nl: Netlist) -> None:
    header(2, "SYMBOL RESOLUTION  (per unique lib_id)")
    seen: dict[str, list[str]] = {}
    for p in nl.parts:
        seen.setdefault(p.lib_id, []).append(p.refdes)
    rows: list[list[str]] = []
    import kicad_sch_api as ksa
    for lib_id in sorted(seen):
        try:
            ksa.get_symbol_info(lib_id)
            found = "yes"
        except Exception as e:
            found = f"NO ({type(e).__name__})"
        units = _real_unit_count(lib_id)
        rows.append([
            lib_id, found, str(units),
            ",".join(sorted(seen[lib_id])),
        ])
    table(rows, ["lib_id", "in libs?", "units", "used by"])
    if any(r[1].startswith("NO") for r in rows):
        print("\n    ! a 'NO' here means place() will raise PlacerError on add().")
        print("      Register the symbol's library with --symbols.")
    pause()


def stage_classify(nl: Netlist) -> None:
    header(3, "CLASSIFICATION  (classify → LayoutPlan: roles + net kinds)")
    plan = classify(nl)
    s = plan.summary()
    print(f"  ICs: {plan.ics or '—'}")
    print()
    print("  parts by role:")
    for role, refs in s["parts_by_role"].items():
        print(f"    {role:<18} {', '.join(refs)}")
    print()
    print("  nets by kind:")
    for kind, names in s["nets_by_kind"].items():
        print(f"    {kind:<14} {', '.join(names)}")
    if s["dividers"]:
        print()
        print("  dividers:")
        for d in s["dividers"]:
            print(f"    {d}")
    print()
    print("  → place() will use the "
          + ("pin-anchored grammar" if len(plan.ics) == 1
             else "measured-column fallback"))
    pause()


def stage_placement(result: PlacerResult) -> None:
    header(4, "PLACEMENT  (placed_refs — refdes → mm position)")
    table(
        [[ref, f"{x:.2f}", f"{y:.2f}"]
         for ref, (x, y) in sorted(result.placed_refs.items())],
        ["refdes", "x", "y"],
    )
    if result.issues:
        print(f"\n  placer issues ({len(result.issues)}):")
        for i in result.issues:
            print(f"    - {i}")
    else:
        print("\n  no placer issues.")
    pause()


def stage_power(nl: Netlist, result: PlacerResult) -> None:
    header(5, "POWER SYMBOLS  (rails → power:* symbol vs labels-only)")
    power_nets = sorted((n for n in nl.nets if n.is_power), key=lambda n: n.name)
    if not power_nets:
        print("    (no power nets in this netlist)")
    else:
        table(
            [[n.name, _power_lib_id(n.name) or "— (labels only)"]
             for n in power_nets],
            ["rail", "symbol"],
        )
    pause()


def stage_labels(result: PlacerResult) -> None:
    header(6, "LABELS  (label_specs — one per endpoint pin)")
    per_net: dict[str, int] = {}
    for s in result.label_specs:
        per_net[s.net_name] = per_net.get(s.net_name, 0) + 1
    table(
        [[s.ref, s.pin, s.net_name, f"{s.position[0]:.2f}", f"{s.position[1]:.2f}"]
         for s in result.label_specs],
        ["ref", "pin", "net", "x", "y"],
    )
    print(f"\n  {len(result.label_specs)} labels across {len(per_net)} nets:")
    for net, count in sorted(per_net.items()):
        print(f"    {net}: {count}")
    pause()


def stage_serialize(result: PlacerResult, out_path: Path) -> None:
    header(7, "SERIALIZED OUTPUT  (.kicad_sch written to disk)")
    out_path.write_text(result.sch_text)
    lines = result.sch_text.count("\n") + 1
    print(f"    wrote {out_path}")
    print(f"    {len(result.sch_text):,} bytes · {lines:,} lines")
    pause()


def stage_validate(nl: Netlist, result: PlacerResult) -> bool:
    header(8, "STRUCTURAL VALIDATION  (validate_placer_output — round-trip)")
    vr = validate_placer_output(nl, result)
    print(f"    ok = {vr.ok}")
    if vr.errors:
        print(f"    errors ({len(vr.errors)}):")
        for e in vr.errors:
            print(f"      - {e}")
    if vr.warnings:
        print(f"    warnings ({len(vr.warnings)}):")
        for w in vr.warnings:
            print(f"      - {w}")
    if not vr.errors and not vr.warnings:
        print("    clean — every part and label round-tripped through save/load.")
    pause()
    return vr.ok


def stage_render(out_path: Path, do_open: bool) -> None:
    header(9, "RENDER  (PNG screenshot of the placed schematic)")
    from pinflow_api.emit.render import render_schematic

    renders_dir = API_DIR / "_renders"
    renders_dir.mkdir(exist_ok=True)
    png = renders_dir / (out_path.stem + ".png")
    try:
        render_schematic(out_path, png, dpi=300)
        print(f"    wrote {png}")
        if do_open:
            subprocess.run(["open", str(png)], check=False)
    except Exception as e:
        print(f"    ! render failed: {e}", file=sys.stderr)
    pause()


# --- driver -------------------------------------------------------------------

def main() -> int:
    global _STEP
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("netlist", type=Path, help="Path to a Netlist JSON file")
    ap.add_argument(
        "-o", "--out", type=Path,
        help="Output .kicad_sch (default: <netlist>.replayed.kicad_sch)",
    )
    ap.add_argument("--title", default=None, help="Block title (default: stem)")
    ap.add_argument(
        "--symbols", type=Path, default=None,
        help="Sidecar .kicad_sym dir; auto-detects <netlist>.symbols/.",
    )
    ap.add_argument(
        "--step", action="store_true",
        help="Pause after each stage (auto-disabled when stdin is not a TTY).",
    )
    ap.add_argument("--no-render", action="store_true", help="Skip stage 9.")
    ap.add_argument("--open", action="store_true", help="Open the rendered PNG.")
    args = ap.parse_args()

    _STEP = args.step and sys.stdin.isatty()
    if args.step and not _STEP:
        print("(--step ignored: stdin is not a TTY)", file=sys.stderr)

    nl_path = args.netlist.resolve()
    if not nl_path.is_file():
        print(f"error: {nl_path} not found", file=sys.stderr)
        return 1

    out_path = args.out or nl_path.with_name(nl_path.stem + ".replayed.kicad_sch")
    title = args.title or nl_path.stem

    # Register sidecar symbol libraries before anything touches the cache.
    symbols_dir = args.symbols
    if symbols_dir is None:
        guess = nl_path.with_suffix(".symbols")
        if guess.is_dir():
            symbols_dir = guess
    if symbols_dir is not None and symbols_dir.is_dir():
        import kicad_sch_api as ksa
        try:
            ksa.get_symbol_cache().discover_libraries([symbols_dir])
            n = len(sorted(symbols_dir.glob("*.kicad_sym")))
            print(f"registered {n} sidecar lib(s) from {symbols_dir}/")
        except Exception as e:
            print(f"! discover_libraries failed: {e}", file=sys.stderr)

    nl = Netlist.model_validate(json.loads(nl_path.read_text()))

    stage_input(nl, nl_path)
    stage_self_validation(nl)
    stage_symbols(nl)
    stage_classify(nl)

    # The placement run itself — the one place a PlacerError can surface.
    try:
        result = place(nl, title=title)
    except PlacerError as e:
        header(4, "PLACEMENT — FAILED")
        print("    PlacerError:")
        for err in e.errors:
            print(f"      - {err}")
        print("\n    (stages 5-9 skipped — no PlacerResult to inspect)")
        return 3

    stage_placement(result)
    stage_power(nl, result)
    stage_labels(result)
    stage_serialize(result, out_path)
    ok = stage_validate(nl, result)
    if not args.no_render:
        stage_render(out_path, args.open)

    print(f"\n{'=' * 72}")
    print(f"  DONE — {out_path.name}  ·  structural ok={ok}")
    print("=" * 72)
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
