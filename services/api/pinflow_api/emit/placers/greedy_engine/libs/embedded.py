"""Lift symbol definitions out of a .kicad_sch's embedded lib_symbols block.

A KiCad schematic file carries copies of every symbol it uses. For custom or
project-local symbols that don't exist in the stock KiCad install, this
embedded copy IS the authoritative definition.

This module extracts those embedded definitions, rewrites each as a standalone
.kicad_sym file in a temp directory, and registers the resulting library with
kicad-sch-api's symbol cache. After calling `register_embedded_symbols(...)`,
the SymbolLibrary resolver can find these symbols by their normal lib_id.
"""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from pathlib import Path

import sexpdata


def _is_named_list(node, name: str) -> bool:
    return (
        isinstance(node, list)
        and len(node) > 0
        and isinstance(node[0], sexpdata.Symbol)
        and node[0].value() == name
    )


def _find_block(parsed: list, name: str) -> list | None:
    for item in parsed:
        if _is_named_list(item, name):
            return item
    return None


def _to_sexpr_str(node) -> str:
    """Render a parsed sexp node back to a string. We use sexpdata.dumps but it
    has a quirk: it uppercases nothing and quotes strings normally; works fine
    for KiCad files which are already in this style."""
    return sexpdata.dumps(node)


def _strip_prefix_from_symbol(symbol_node: list, prefix: str) -> list:
    """Rewrite the symbol's name from 'PREFIX:NAME' to just 'NAME' (in place style)."""
    # symbol_node looks like: [Symbol('symbol'), 'PREFIX:NAME', ...rest...]
    full_name = symbol_node[1]
    if not isinstance(full_name, str):
        return symbol_node
    if full_name.startswith(prefix + ":"):
        new_name = full_name[len(prefix) + 1:]
        return [symbol_node[0], new_name] + list(symbol_node[2:])
    return symbol_node


def extract_lib_symbols(sch_path: str | Path) -> dict[str, list]:
    """Parse the .kicad_sch and return {lib_id: parsed_symbol_node}.

    The returned nodes are sexpdata-parsed lists that can be re-rendered.
    """
    text = Path(sch_path).read_text()
    parsed = sexpdata.loads(text)
    lib_symbols = _find_block(parsed, "lib_symbols")
    if lib_symbols is None:
        return {}

    result: dict[str, list] = {}
    for entry in lib_symbols[1:]:
        if not _is_named_list(entry, "symbol"):
            continue
        full_name = entry[1]
        if not isinstance(full_name, str):
            continue
        result[full_name] = entry
    return result


def register_embedded_symbols(sch_path: str | Path, cache, temp_root: Path | None = None) -> list[str]:
    """Extract every embedded symbol from sch_path and register them with `cache`
    (a kicad-sch-api SymbolLibraryCache).

    Returns the list of lib_ids that were registered.
    """
    entries = extract_lib_symbols(sch_path)
    if not entries:
        return []

    if temp_root is None:
        # Stable per-schematic temp dir so we can re-run without exploding disk.
        sch_path = Path(sch_path).resolve()
        temp_root = Path(tempfile.gettempdir()) / "schematic_agent_embedded_syms"
    temp_root.mkdir(parents=True, exist_ok=True)
    sch_id = Path(sch_path).stem
    out_dir = Path(temp_root) / sch_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Group symbols by lib name (the part before the colon in the lib_id).
    by_lib: dict[str, list[list]] = defaultdict(list)
    for full_name, node in entries.items():
        if ":" not in full_name:
            # Unusual case — skip (shouldn't happen for well-formed schematics).
            continue
        lib_name, part_name = full_name.split(":", 1)
        # Rewrite the symbol's name from "LIB:PART" to just "PART" so the file
        # parses as a normal library with the lib name coming from the filename.
        rewritten = _strip_prefix_from_symbol(node, lib_name)
        by_lib[lib_name].append(rewritten)

    registered: list[str] = []
    for lib_name, symbols in by_lib.items():
        # Build a (kicad_symbol_lib ...) wrapper.
        wrapper = [
            sexpdata.Symbol("kicad_symbol_lib"),
            [sexpdata.Symbol("version"), 20231120],
            [sexpdata.Symbol("generator"), "schematic_agent"],
        ] + symbols
        out_file = out_dir / f"{lib_name}.kicad_sym"
        out_file.write_text(_to_sexpr_str(wrapper))
        cache.add_library_path(str(out_file))
        for s in symbols:
            registered.append(f"{lib_name}:{s[1]}")

    return registered
