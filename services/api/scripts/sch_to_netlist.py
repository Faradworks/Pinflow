"""Export a `.kicad_sch` to the placer's Netlist IR as JSON.

Read-side pipeline:
    kicad-cli sch export netlist → parse_kicadsexpr → ParsedNetlist
                                                   ↓ (+ parse_properties for MPN)
                                                   build_design_graph
                                                   ↓
                                                   DesignGraph
                                                   ↓ (this script)
                                                   emit.netlist.Netlist (JSON on disk)

Whole-schematic export: every component in, no boundary, no ports. Keep
original net names (KiCad-auto `Net-(...)` and `/foo` names round-trip
verbatim — diffs will churn, but the topology is faithful). Drop
`unconnected-(...)` markers, which are no-connect annotations, not real nets.

Usage:
    cd services/api
    .venv/bin/python scripts/sch_to_netlist.py /abs/path/to/foo.kicad_sch
    # → writes /abs/path/to/foo.netlist.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "services" / "api"))

from pinflow_api.builders._common import _matching_close  # noqa: E402
from pinflow_api.emit.netlist import (  # noqa: E402
    Netlist,
    NetlistEndpoint,
    NetlistNet,
    NetlistPart,
)
from pinflow_api.graph import build_design_graph  # noqa: E402
from pinflow_api.graph.models import NetType  # noqa: E402
from pinflow_api.kicad_cli import export_netlist  # noqa: E402
from pinflow_api.netlist import parse_kicadsexpr  # noqa: E402
from pinflow_api.sch_properties import parse_properties  # noqa: E402
from pinflow_api.sym_lib import build_pinflow_lib  # noqa: E402


_NO_CONNECT_PREFIX = "unconnected-"


def _extract_lib_symbols_block(sch_text: str) -> str | None:
    """Return the verbatim `(lib_symbols ...)` block, or None if absent."""
    needle = "(lib_symbols"
    start = sch_text.find(needle)
    if start < 0:
        return None
    end = _matching_close(sch_text, start)
    if end < 0:
        return None
    return sch_text[start : end + 1]


def _iter_lib_symbols_entries(block: str):
    """Yield each top-level `(symbol "<lib>:<sym>" ...)` inside `(lib_symbols ...)`.

    Top-level here = direct children of `(lib_symbols ...)`. Nested unit
    symbols (the `_0_0`, `_1_1` blocks) are inside their parent's body and
    are yielded as part of that parent's verbatim text.
    """
    # Skip the opening "(lib_symbols" plus any whitespace.
    i = block.find("(lib_symbols") + len("(lib_symbols")
    n = len(block)
    while i < n:
        # Find the next "(symbol" at depth 1.
        j = block.find("(symbol", i)
        if j < 0:
            return
        # Confirm it's at depth 1 — count parens between i and j.
        depth = 1  # we're inside (lib_symbols
        for k in range(i, j):
            c = block[k]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
        if depth != 1:
            # We descended into a child; skip past this match.
            i = j + 1
            continue
        end = _matching_close(block, j)
        if end < 0:
            return
        yield block[j : end + 1]
        i = end + 1


def _symbol_top_name(symbol_text: str) -> str | None:
    """Extract the top-level symbol name from `(symbol "..." ...)`."""
    i = symbol_text.find("(symbol")
    if i < 0:
        return None
    q1 = symbol_text.find('"', i)
    if q1 < 0:
        return None
    q2 = symbol_text.find('"', q1 + 1)
    if q2 < 0:
        return None
    return symbol_text[q1 + 1 : q2]


def _rewrite_top_symbol_name(symbol_text: str, new_name: str) -> str:
    """Replace the first quoted token after `(symbol ` with `new_name`.

    Leaves nested unit-symbol names alone — they don't carry the `lib:`
    prefix and the placer doesn't reference them by name.
    """
    i = symbol_text.find("(symbol")
    q1 = symbol_text.find('"', i)
    q2 = symbol_text.find('"', q1 + 1)
    return symbol_text[: q1 + 1] + new_name + symbol_text[q2:]


def lift_embedded_symbols(sch_path: Path, out_dir: Path) -> dict:
    """Lift `(lib_symbols ...)` into one `.kicad_sym` per library.

    Symbols in `(lib_symbols ...)` are named `"<lib>:<sym>"`. Standalone
    `.kicad_sym` files name their top-level symbols `"<sym>"` (the library
    name comes from the filename). We split by `<lib>` and rewrite the
    top-level name when writing.

    Returns a stats dict `{libs: {<lib>: count}, files_written: [Path,...]}`.
    """
    sch_text = sch_path.read_text()
    block = _extract_lib_symbols_block(sch_text)
    if block is None:
        return {"libs": {}, "files_written": []}

    by_lib: dict[str, list[str]] = {}
    skipped_no_colon: list[str] = []
    for sym_text in _iter_lib_symbols_entries(block):
        full_name = _symbol_top_name(sym_text) or ""
        if ":" not in full_name:
            # Some power symbols / locally-defined parts may not be `lib:sym`.
            # Skip — KiCad's symbol picker resolves them from elsewhere.
            skipped_no_colon.append(full_name)
            continue
        lib, _, sym = full_name.partition(":")
        rewritten = _rewrite_top_symbol_name(sym_text, sym)
        by_lib.setdefault(lib, []).append(rewritten)

    out_dir.mkdir(parents=True, exist_ok=True)
    files_written: list[Path] = []
    for lib, symbols in sorted(by_lib.items()):
        lib_path = out_dir / f"{lib}.kicad_sym"
        lib_path.write_text(build_pinflow_lib(symbols))
        files_written.append(lib_path)

    return {
        "libs": {lib: len(syms) for lib, syms in by_lib.items()},
        "files_written": files_written,
        "skipped_no_colon": skipped_no_colon,
    }


def sch_to_netlist(sch_path: Path) -> tuple[Netlist, dict]:
    """Return (Netlist, stats) for the given schematic.

    `stats` is a small dict of counts useful for the CLI summary line.
    """
    netlist_text = export_netlist(sch_path)
    parsed = parse_kicadsexpr(netlist_text)

    try:
        props_by_ref = parse_properties(sch_path)
    except Exception as exc:
        print(f"  ! parse_properties failed: {exc} — continuing with empty props",
              file=sys.stderr)
        props_by_ref = {}

    graph = build_design_graph(parsed, props_by_ref, profiles_by_mpn={})

    # KiCad emits one `unconnected-(...)` net per no-connect X marker; capture
    # the pin behind each so the placer can re-emit the X (else ERC would
    # later flag the pin as an accidental dangler).
    no_connects: dict[str, list[str]] = {}
    for name, net in graph.nets.items():
        if name.startswith(_NO_CONNECT_PREFIX):
            for pc in net.pins:
                no_connects.setdefault(pc.component_ref, []).append(
                    pc.pin_number
                )

    parts: list[NetlistPart] = []
    missing_lib_id: list[str] = []
    for refdes in sorted(graph.components.keys()):
        c = graph.components[refdes]
        if not c.lib_id:
            missing_lib_id.append(refdes)
        parts.append(
            NetlistPart(
                refdes=refdes,
                lib_id=c.lib_id or "",
                value=c.value or "",
                footprint=c.footprint or "",
                mpn=c.mpn,
                no_connect_pins=sorted(no_connects.get(refdes, [])),
            )
        )

    nets: list[NetlistNet] = []
    for name in sorted(graph.nets.keys()):
        if name.startswith(_NO_CONNECT_PREFIX):
            continue  # captured above as no_connect_pins
        net = graph.nets[name]
        endpoints = [
            NetlistEndpoint(ref=pc.component_ref, pin=pc.pin_number)
            for pc in net.pins
        ]
        if not endpoints:
            continue
        nets.append(
            NetlistNet(
                name=name,
                is_power=net.net_type in (NetType.POWER, NetType.GROUND),
                voltage=net.voltage,
                endpoints=endpoints,
                is_port=False,  # whole-schematic export — no boundary
            )
        )

    netlist = Netlist(parts=parts, nets=nets)
    stats = {
        "parts": len(parts),
        "nets": len(nets),
        "missing_lib_id": missing_lib_id,
        "no_connects": sum(len(v) for v in no_connects.values()),
    }
    return netlist, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sch", type=Path, help="Path to a .kicad_sch file")
    ap.add_argument(
        "-o", "--out", type=Path,
        help="Output path (default: <sch>.netlist.json next to the input)",
    )
    ap.add_argument(
        "--symbols-out", type=Path,
        help=(
            "Directory for sidecar .kicad_sym files lifted from the source "
            "schematic's (lib_symbols ...) block. Defaults to <out>.symbols/ "
            "next to the netlist JSON. Pass --no-symbols to skip."
        ),
    )
    ap.add_argument(
        "--no-symbols", action="store_true",
        help="Skip the embedded-symbols lift step.",
    )
    args = ap.parse_args()

    sch_path = args.sch.resolve()
    if not sch_path.is_file():
        print(f"error: {sch_path} not found", file=sys.stderr)
        return 1

    out_path = args.out or sch_path.with_suffix(".netlist.json")
    symbols_out = (
        None
        if args.no_symbols
        else (args.symbols_out or out_path.with_suffix(".symbols"))
    )

    netlist, stats = sch_to_netlist(sch_path)

    self_errors = netlist.validate_self()
    if self_errors:
        print("WARN — structural issues in extracted netlist:", file=sys.stderr)
        for e in self_errors:
            print(f"  - {e}", file=sys.stderr)

    out_path.write_text(json.dumps(netlist.model_dump(), indent=2))
    print(f"wrote {out_path}")
    print(
        f"  parts={stats['parts']}  nets={stats['nets']}  "
        f"no_connects={stats['no_connects']}"
    )
    if stats["missing_lib_id"]:
        print(
            f"  ! {len(stats['missing_lib_id'])} parts have empty lib_id "
            f"(placer will reject these): {stats['missing_lib_id'][:5]}"
            + ("..." if len(stats["missing_lib_id"]) > 5 else "")
        )

    if symbols_out is not None:
        sym_stats = lift_embedded_symbols(sch_path, symbols_out)
        if sym_stats["files_written"]:
            print(f"  symbols → {symbols_out}/")
            for lib, count in sorted(sym_stats["libs"].items()):
                print(f"    {lib}.kicad_sym  ({count} symbol{'s' if count != 1 else ''})")
            if sym_stats["skipped_no_colon"]:
                print(
                    f"  ! skipped {len(sym_stats['skipped_no_colon'])} symbols "
                    "with no lib: prefix (likely local-only definitions)"
                )
        else:
            print("  (no (lib_symbols ...) block found — nothing to lift)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
