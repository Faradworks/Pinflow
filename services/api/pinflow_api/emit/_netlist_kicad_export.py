"""Minimal Pinflow `Netlist` → KiCad `.net` S-expression exporter.

Used by `emit.placers.greedy` to hand Pinflow's structured netlist into the
in-process greedy engine (`emit.placers.greedy_engine`), whose parser
expects KiCad's stock netlist format. The exporter emits only what
`greedy_engine.parse.netlist` reads — the `(components ...)` block (refdes
/ value / footprint / libsource) and the `(nets ...)` block (name / nodes)
— and skips the design header, `libparts`, `libraries`, and per-pin
metadata that the parser drops anyway.
"""

from __future__ import annotations

from pinflow_api.emit.netlist import Netlist


def netlist_to_kicad_net(netlist: Netlist) -> str:
    """Render `netlist` as a KiCad-format S-expression netlist string.

    The format is compatible with what `kicad-cli sch export netlist
    --format kicadsexpr` produces, modulo the metadata blocks that the
    /examples parser ignores anyway."""
    lines: list[str] = ['(export']
    lines.append('  (version "E")')
    lines.append('  (design (source "pinflow"))')

    lines.append('  (components')
    for p in netlist.parts:
        lib, _, part = p.lib_id.partition(":")
        if not part:
            # lib_id without a colon (rare). Stuff the whole string into the
            # part name with a dummy lib — /examples will still join them
            # into a lib_id string when needed.
            lib, part = "Pinflow", p.lib_id
        lines.append(f'    (comp (ref "{p.refdes}")')
        lines.append(f'      (value "{p.value or p.refdes}")')
        if p.footprint:
            lines.append(f'      (footprint "{p.footprint}")')
        lines.append(
            f'      (libsource (lib "{lib}") (part "{part}")))'
        )
    lines.append('  )')

    lines.append('  (nets')
    for i, n in enumerate(netlist.nets, start=1):
        lines.append(f'    (net (code "{i}") (name "{n.name}")')
        for ep in n.endpoints:
            lines.append(
                f'      (node (ref "{ep.ref}") (pin "{ep.pin}"))'
            )
        lines.append('    )')
    lines.append('  )')

    lines.append(')')
    return '\n'.join(lines)
