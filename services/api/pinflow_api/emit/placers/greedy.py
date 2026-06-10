"""Greedy placer — BFS-driven layout for dense-IC subcircuits.

In-process port of the `/examples` schematic generator. Handles the cap-
island + signal-staircase + power-at-edge idiom out of the box; selected
by `get_placer('auto')` when the layout tree shows a `SIGNAL_STAIRCASE`
and a `DIVIDER_STACK` on the same IC side (the dense-IC pattern where
cplace's independent archetype emitters produce inter-archetype body
overlaps).

The engine code lives in `pinflow_api.emit.placers.greedy_engine` (a
verbatim port of `/examples/generator/`, with internal imports repointed
into the Pinflow namespace). This wrapper plumbs Pinflow's `Netlist` →
engine, runs the BFS placer, and packages the engine's `.kicad_sch`
output as a `PlacerResult`.

Trade-offs vs. cplace:

  - Greedy is NOT byte-deterministic across runs (BFS visits parts in
    iteration order; ordering subtleties don't always converge).
  - Greedy gate-fails on at least one corpus golden (mt3608: wires
    through bodies, broken connectivity, rubric 0.193). That's why the
    gate is `cplace ELSE greedy` not the reverse — cplace's failure
    mode is visual ugliness, greedy's is structural breakage.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from pinflow_api.emit._netlist_kicad_export import netlist_to_kicad_net
from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.netlist_to_sch import (
    LabelSpec,
    PlacerError,
    PlacerResult,
)
from pinflow_api.emit.placers.greedy_engine.emit import write_schematic
from pinflow_api.emit.placers.greedy_engine.parse import parse_netlist
from pinflow_api.emit.placers.greedy_engine.place.greedy import GreedyPlacer
from pinflow_api.emit.placers.greedy_engine.symbols import SymbolLibrary


# Default KiCad symbol library search path on macOS. The engine accepts
# multiple extra paths; we pass this if it exists. Override platform-
# specific paths in a follow-up when Pinflow ships on Linux/Windows.
_DEFAULT_KICAD_SYMBOLS = Path(
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"
)


def _extract_placed_refs(sch_text: str) -> dict[str, tuple[float, float]]:
    """Walk `(symbol ... (at X Y rot) ... (property "Reference" "X") ...)`
    blocks in the schematic text and return refdes → origin position.
    Used by the downstream structural validator (`validate_placer_output`)
    to confirm every netlist part landed somewhere."""
    placed: dict[str, tuple[float, float]] = {}
    pattern = re.compile(
        r'\(symbol\s+\(lib_id\s+"[^"]+"\)\s+\(at\s+([\d.-]+)\s+([\d.-]+)\s+[\d.-]+\)'
        r'[\s\S]*?\(property\s+"Reference"\s+"([^"]+)"',
        re.MULTILINE,
    )
    for m in pattern.finditer(sch_text):
        x, y, ref = m.group(1), m.group(2), m.group(3)
        if ref.startswith("#"):           # power symbols (`#PWR`) — skip
            continue
        placed[ref] = (float(x), float(y))
    return placed


def greedy(netlist: Netlist, *, title: str = "Subcircuit",
            source_schematic: Path | str | None = None) -> PlacerResult:
    """Run the in-process greedy engine on `netlist` and return its output
    as a `PlacerResult`. Raises `PlacerError` on parse / placement /
    emission failure.

    `source_schematic` — optional path to a `.kicad_sch` whose embedded
    `(lib_symbols ...)` should be lifted into the symbol library (when
    the netlist references project-local symbols not in stock KiCad).
    Pass the golden's `.kicad_sch` for the corpus, or
    `state.active_sch_path` for the agent flow."""

    # The engine consumes a KiCad .net file path; serialise the Pinflow
    # Netlist via the existing exporter and stage it in a temp file. Same
    # is needed for the output schematic (the engine writes to a path).
    # Both temp files are cleaned up on the way out.
    with tempfile.NamedTemporaryFile(
            suffix=".net", mode="w", delete=False) as f:
        f.write(netlist_to_kicad_net(netlist))
        net_path = Path(f.name)
    out_path = Path(tempfile.mktemp(suffix=".kicad_sch"))

    try:
        try:
            cg = parse_netlist(net_path)
        except Exception as e:
            raise PlacerError([f"greedy parse failed: {e}"])

        extra_paths = (
            [str(_DEFAULT_KICAD_SYMBOLS)]
            if _DEFAULT_KICAD_SYMBOLS.is_dir() else None
        )
        lib = SymbolLibrary(extra_paths=extra_paths)
        if source_schematic:
            src = Path(source_schematic).resolve()
            if src.is_file():
                lib.load_embedded_from(src)

        try:
            placer = GreedyPlacer(lib)
            placement = placer.place(cg)
        except Exception as e:
            raise PlacerError([f"greedy placement failed: {e}"])

        try:
            stats = write_schematic(
                placement, cg, lib, out_path, title=title,
            )
        except Exception as e:
            raise PlacerError([f"greedy emit failed: {e}"])

        if not out_path.is_file():
            raise PlacerError(["greedy produced no output schematic"])
        sch_text = out_path.read_text()
    finally:
        for p in (net_path, out_path):
            try:
                p.unlink()
            except OSError:
                pass

    placed_refs = _extract_placed_refs(sch_text)
    return PlacerResult(
        sch_text=sch_text,
        issues=[
            f"greedy: {len(placement.components)} parts, "
            f"{len(placement.power_flags)} flags, "
            f"{stats['wires']} wires, {stats['junctions']} junctions",
        ],
        placed_refs=placed_refs,
        label_specs=[],          # the engine wires explicitly; no labels to validate
    )
