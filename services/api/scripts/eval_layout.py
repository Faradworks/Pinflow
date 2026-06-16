"""Layout-quality eval harness — score the placer against the golden corpus.

Step 1 of the placer rebuild: you cannot tune a layout engine you cannot
measure. This harness pairs the rubric scorer
(`pinflow_api.emit.rubric`) with the golden corpus (`tests/fixtures/
golden_corpus.json`) and reports, per golden, the gap between the hand-drawn
schematic and what `place()` regenerates from the same netlist.

    cd services/api
    .venv/bin/python scripts/eval_layout.py                # whole golden corpus
    .venv/bin/python scripts/eval_layout.py --only tps63020 --render
    .venv/bin/python scripts/eval_layout.py --json > report.json
    .venv/bin/python scripts/eval_layout.py \
        --manifest tests/fixtures/generated_corpus.json --render  # prompt-derived corpus

For each golden it discovers the entry's symbol libraries, scores the golden
`.kicad_sch`, regenerates from the golden's extracted netlist, scores that,
and prints a side-by-side rubric comparison — the golden is the ceiling, the
delta is the work left to do. `--placer` picks the engine (any name in the
`emit.placers` registry — `auto`, `cplace`, `greedy`, `legacy`, `llm_placer`,
plus whatever you register); default `cplace`. `auto` is the production gate
(cplace for well-coordinated topologies; falls to greedy on dense ICs where
cplace's independent archetype emitters produce inter-archetype body
overlaps). `greedy` is /examples' BFS placer as a subprocess — visually best
on dense ICs but gate-fails on sparse-IC topologies (e.g. mt3608). `legacy`
is the older column placer kept as a regression reference.

`--manifest` selects the corpus (default `golden_corpus.json`). The
prompt-derived `generated_corpus.json` has netlist-only entries (no hand-drawn
`sch` ceiling): those score the regeneration alone, no golden column.

Ad-hoc modes (no corpus):

    --sch FILE [--netlist FILE] [--symbols DIR]   score one schematic
    --netlist FILE [--symbols DIR]                regenerate + score the regen

This is an *eval*, not a pass/fail gate (that is `scripts/compare_placers.py`)
— it always exits 0 unless it cannot run at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

import kicad_sch_api as ksa  # noqa: E402

from pinflow_api.emit.netlist import Netlist  # noqa: E402
from pinflow_api.emit.placers import get_placer, list_placers  # noqa: E402
from pinflow_api.emit.netlist_to_sch import PlacerError  # noqa: E402
from pinflow_api.emit.rubric import RubricScore, score  # noqa: E402

FIXTURES = API_DIR / "tests" / "fixtures"
MANIFEST = FIXTURES / "golden_corpus.json"
RENDERS = API_DIR / "_renders"
_WHITE_BG = False  # set from --white in main(); read by _render


# --- formatting --------------------------------------------------------------

def _num(v: float | None) -> str:
    """Compact number — integer when whole, else 2 dp; em dash for None."""
    if v is None:
        return "—"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}"


def _sc(s: float | None) -> str:
    return "n/a" if s is None else f"{s:.2f}"


def _gate(v: bool | None) -> str:
    return {True: "pass", False: "FAIL", None: "n/a"}[v]


def _print_comparison(name: str, note: str, golden: RubricScore,
                      regen: RubricScore | None, regen_err: str | None) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n {name}  ·  {note}\n{bar}")

    print(f"  {'gate':<22}{'golden':>10}{'regen':>12}")
    for g in golden.gates:
        r = _gate(regen.gates.get(g)) if regen else "—"
        print(f"  {g:<22}{_gate(golden.gates[g]):>10}{r:>12}")

    print(f"\n  {'metric':<20}{'wt':>6}{'g.raw':>8}{'g.score':>9}"
          f"{'r.raw':>8}{'r.score':>9}{'Δscore':>9}")
    for m in golden.metrics:
        rm = regen.metric(m.name) if regen else None
        rraw = _num(rm.raw) if rm else "—"
        rsc = _sc(rm.score) if rm else "—"
        delta = (
            f"{rm.score - m.score:+.2f}"
            if rm and rm.score is not None and m.score is not None
            else "—"
        )
        print(f"  {m.name:<20}{m.weight:>6.2f}{_num(m.raw):>8}"
              f"{_sc(m.score):>9}{rraw:>8}{rsc:>9}{delta:>9}")

    print("  " + "-" * 76)
    rtot = f"{regen.total:.3f}" if regen else "—"
    gap = f"{regen.total - golden.total:+.3f}" if regen else "—"
    print(f"  {'TOTAL':<20}{'':>6}{'':>8}{golden.total:>9.3f}"
          f"{'':>8}{rtot:>9}{gap:>9}")

    if regen_err:
        print(f"\n  ⚠ regeneration failed — {regen_err}")
    for src, rb in (("golden", golden), ("regen", regen)):
        for n in (rb.notes if rb else []):
            print(f"  · {src}: {n}")


def _print_single(title: str, rb: RubricScore) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n {title}\n{bar}")
    print(f"  gates: " + "  ".join(
        f"{g}={_gate(v)}" for g, v in rb.gates.items()))
    print(f"\n  {'metric':<22}{'weight':>8}{'raw':>9}{'score':>9}   detail")
    for m in rb.metrics:
        print(f"  {m.name:<22}{m.weight:>8.2f}{_num(m.raw):>9}{_sc(m.score):>9}"
              f"   {m.detail}")
    print("  " + "-" * 76)
    print(f"  {'TOTAL':<22}{'':>8}{'':>9}{rb.total:>9.3f}")
    for n in rb.notes:
        print(f"  · {n}")


# --- corpus runners ----------------------------------------------------------

def _discover(symbols_dir: Path | None) -> None:
    """Register an entry's sidecar symbol libraries with the ksa cache so
    `classify` (pinmap) and bbox measurement can resolve project-local parts."""
    if symbols_dir and symbols_dir.is_dir():
        try:
            ksa.get_symbol_cache().discover_libraries([str(symbols_dir)])
        except Exception as e:  # noqa: BLE001
            print(f"  warn: could not register {symbols_dir}: {e}",
                  file=sys.stderr)


def _load_netlist(path: Path) -> Netlist:
    return Netlist.model_validate(json.loads(path.read_text()))


# Every registered placer is selectable — adding an experimental engine to the
# `emit.placers` registry makes it runnable here with no edit to this script.
_PLACER_NAMES = tuple(list_placers())


def _regenerate(netlist: Netlist, title: str, placer,
                source_schematic: Path | None = None
                ) -> tuple[str | None, str | None]:
    """Run a placer; return (sch_text, error) — exactly one is non-None.
    `source_schematic` is forwarded to placers that accept it (greedy /
    auto-when-gated-to-greedy) so /examples can lift project-local
    lib_symbols out of the golden's `.kicad_sch`."""
    kw = {"title": title}
    if source_schematic is not None:
        kw["source_schematic"] = str(source_schematic)
    try:
        return placer(netlist, **kw).sch_text, None
    except TypeError:
        # Placer doesn't accept source_schematic — retry without it.
        try:
            return placer(netlist, title=title).sch_text, None
        except PlacerError as e:
            return None, "PlacerError: " + "; ".join(e.errors)
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
    except PlacerError as e:
        return None, "PlacerError: " + "; ".join(e.errors)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _render(text: str, name: str) -> None:
    """Best-effort PNG render into `_renders/` — never fatal to the eval."""
    try:
        from pinflow_api.emit.render import render_schematic
        out = render_schematic(text, RENDERS / f"{name}.png", dpi=200,
                               white_background=_WHITE_BG)
        print(f"  rendered → {out}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  warn: render {name} failed: {e}", file=sys.stderr)


def _run_entry(entry: dict, do_render: bool, verbose: bool,
               placer, placer_name: str) -> dict:
    """Score one corpus entry with a placer's regeneration.

    Two manifest shapes are supported. Golden entries carry a hand-drawn
    `sch` — the ceiling — and we print the golden-vs-regen delta. Netlist-only
    entries (the prompt-derived `generated_corpus.json`) have no `sch`: there
    is no ceiling, so we score only the regeneration. `score_floor`, when
    present, rides through to the JSON report for the gate in check_all.py."""
    name = entry["name"]
    _discover(FIXTURES / entry["symbols"] if entry.get("symbols") else None)

    netlist = _load_netlist(FIXTURES / entry["netlist"])

    sch_path = FIXTURES / entry["sch"] if entry.get("sch") else None
    golden = score(sch_path.read_text(), netlist) if sch_path else None

    regen_text, regen_err = _regenerate(netlist, name, placer,
                                         source_schematic=sch_path)
    regen = score(regen_text, netlist) if regen_text else None

    if verbose:
        if golden is not None:
            tag = "hand-drawn" if entry.get("hand_drawn") else "regression input"
            _print_comparison(f"{name}  ({placer_name})",
                              f"{tag}  ·  {entry.get('note', '')}",
                              golden, regen, regen_err)
        elif regen is not None:
            _print_single(f"{name}  ({placer_name})  ·  {entry.get('note', '')}",
                          regen)
        else:
            print(f"\n  ⚠ {name}: regeneration failed — {regen_err}")

    if do_render:
        if sch_path is not None:
            _render(sch_path.read_text(), f"{name}.golden")
        if regen_text:
            _render(regen_text, f"{name}.{placer_name}")

    return {
        "name": name,
        "golden": golden.to_dict() if golden else None,
        "regen": regen.to_dict() if regen else None,
        "regen_error": regen_err,
        "score_floor": entry.get("score_floor"),
    }


# --- main --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", metavar="NAME",
                    help="score just this corpus entry")
    ap.add_argument("--sch", type=Path,
                    help="ad-hoc: score this .kicad_sch (no corpus)")
    ap.add_argument("--netlist", type=Path,
                    help="ad-hoc: netlist JSON — paired with --sch to score "
                         "against it, or alone to regenerate + score")
    ap.add_argument("--symbols", type=Path,
                    help="ad-hoc: sidecar symbols dir to discover")
    ap.add_argument("--manifest", type=Path, default=MANIFEST,
                    help="corpus manifest to score (default: golden_corpus.json). "
                         "Point at generated_corpus.json for the prompt-derived "
                         "netlist corpus.")
    ap.add_argument("--placer", choices=_PLACER_NAMES, default="cplace",
                    help=f"placer to regenerate with (default: cplace; one of "
                         f"{', '.join(_PLACER_NAMES)})")
    ap.add_argument("--render", action="store_true",
                    help="also write PNG renders into _renders/")
    ap.add_argument("--white", action="store_true",
                    help="render on a white background (not KiCad's cream fill)")
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON on stdout")
    args = ap.parse_args()
    global _WHITE_BG
    _WHITE_BG = args.white

    # --- ad-hoc modes --------------------------------------------------------
    if args.sch or (args.netlist and not args.only):
        if args.sch and args.netlist:
            _discover(args.symbols)
            nl = _load_netlist(args.netlist)
            rb = score(args.sch.read_text(), nl)
            (print(json.dumps(rb.to_dict(), indent=2)) if args.json
             else _print_single(f"{args.sch.name}  (vs {args.netlist.name})", rb))
        elif args.sch:
            _discover(args.symbols)
            rb = score(args.sch.read_text())
            (print(json.dumps(rb.to_dict(), indent=2)) if args.json
             else _print_single(f"{args.sch.name}  (geometry only)", rb))
        else:  # --netlist alone — regenerate and score
            _discover(args.symbols)
            nl = _load_netlist(args.netlist)
            text, err = _regenerate(nl, args.netlist.stem,
                                     get_placer(args.placer))
            if err:
                print(f"regeneration failed — {err}", file=sys.stderr)
                return 1
            rb = score(text, nl)
            if args.render:
                _render(text, f"{args.netlist.stem}.regen")
            (print(json.dumps(rb.to_dict(), indent=2)) if args.json
             else _print_single(f"{args.netlist.name}  (regenerated)", rb))
        return 0

    # --- corpus mode ---------------------------------------------------------
    if not args.manifest.is_file():
        print(f"error: corpus manifest not found at {args.manifest}",
              file=sys.stderr)
        return 1
    manifest = json.loads(args.manifest.read_text())
    # golden_corpus.json keys the list under "goldens"; generated_corpus.json
    # under "entries". Accept either so one loader serves both.
    entries = manifest.get("entries") or manifest.get("goldens") or []
    if args.only:
        entries = [e for e in entries if e.get("name") == args.only]
        if not entries:
            print(f"error: no corpus entry named {args.only!r}", file=sys.stderr)
            return 1

    placer = get_placer(args.placer)
    reports = [_run_entry(e, args.render, not args.json, placer, args.placer)
               for e in entries]

    if args.json:
        print(json.dumps({"reports": reports}, indent=2))
    else:
        print(f"\n{'=' * 78}")
        print(f" scored {len(reports)} entr(y/ies) with {args.placer}.")
        for r in reports:
            if not r["regen"]:
                print(f"   {r['name']:<16} regen FAILED — {r['regen_error']}")
            elif r["golden"]:
                g, rg = r["golden"]["total"], r["regen"]["total"]
                print(f"   {r['name']:<16} golden {g:.3f}  →  regen {rg:.3f}"
                      f"  (gap {rg - g:+.3f})")
            else:
                rg = r["regen"]["total"]
                floor = r.get("score_floor")
                tail = f"  (floor {floor})" if floor is not None else ""
                print(f"   {r['name']:<16} regen {rg:.3f}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
