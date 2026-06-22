"""Project-local symbol library: extract a single symbol from KiCad's bundled
libraries and merge it into a project-level `pinflow.kicad_sym`.

KiCad reads symbol libraries on demand from the symbol picker, so writing to
`pinflow.kicad_sym` while a project is open is safe and visible immediately on
the next "Add Symbol" dialog open — no schematic in-memory state to clobber.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import kicad_paths
from .builders._common import _matching_close
from .easyeda import cache_dir as _easyeda_cache_dir

_LIB_HEADER = (
    "(kicad_symbol_lib\n"
    '\t(version 20251024)\n'
    '\t(generator "pinflow")\n'
    '\t(generator_version "10.0")\n'
)


def extract_symbol_text(lib_id: str) -> str:
    """Read the named symbol's `(symbol "Name" ...)` block verbatim.

    lib_id format: `<library>:<symbol>`. Search order:
      1. KiCad bundled libs at `_BUNDLED_SYMS/<library>.kicad_sym`
      2. easyeda cache at `_easyeda_cache_dir()/<library>.kicad_sym` (for
         LCSC-fetched symbols)
    """
    library, _, symbol_name = lib_id.partition(":")
    if not library or not symbol_name:
        raise ValueError(f"invalid lib_id {lib_id!r}, expected <library>:<symbol>")
    candidates = [
        kicad_paths.symbol_lib_dir() / f"{library}.kicad_sym",
        _easyeda_cache_dir() / f"{library}.kicad_sym",
    ]
    src = next((p for p in candidates if p.is_file()), None)
    if src is None:
        raise FileNotFoundError(
            f"lib {library!r} not found in bundled libs or easyeda cache"
        )
    text = src.read_text()
    needle = f'(symbol "{symbol_name}"'
    start = text.find(needle)
    if start < 0:
        raise KeyError(f"{symbol_name!r} not in {src.name}")
    end = _matching_close(text, start)
    if end < 0:
        raise ValueError("unbalanced symbol block")
    return text[start : end + 1]


def build_pinflow_lib(symbol_texts: list[str]) -> str:
    """Wrap a sequence of `(symbol ...)` blocks into a complete .kicad_sym file."""
    body = "\n".join(f"\t{s}" for s in symbol_texts)
    return f"{_LIB_HEADER}{body}\n)\n"


def merge_symbol_into_lib(existing_lib_text: str, new_symbol_text: str) -> str:
    """Return updated lib text with `new_symbol_text` added (replacing any
    existing entry for the same symbol). Idempotent.
    """
    new_name = _symbol_name(new_symbol_text)
    if new_name is None:
        raise ValueError("could not parse symbol name from new entry")

    existing_blocks = list(_iter_top_level_symbols(existing_lib_text))
    kept = [
        block for block in existing_blocks if _symbol_name(block) != new_name
    ]
    return build_pinflow_lib(kept + [new_symbol_text])


def _iter_top_level_symbols(lib_text: str):
    """Yield each `(symbol "Name" ...)` block at the top level of a kicad_symbol_lib.
    Top level here = direct children of the `(kicad_symbol_lib ...)` form, not
    nested unit symbols.
    """
    start = lib_text.find("(kicad_symbol_lib")
    if start < 0:
        return
    end = _matching_close(lib_text, start)
    if end < 0:
        return

    # Step into the body
    body_start = lib_text.find("\n", start)
    body_start = body_start + 1 if body_start != -1 else start + len("(kicad_symbol_lib")

    pos = body_start
    while pos < end:
        while pos < end and lib_text[pos] in " \t\n\r":
            pos += 1
        if pos >= end or lib_text[pos] != "(":
            break
        form_close = _matching_close(lib_text, pos)
        if form_close < 0 or form_close > end:
            break
        head_match = re.match(r"\(\s*([A-Za-z_][\w]*)", lib_text[pos:])
        head = head_match.group(1) if head_match else ""
        if head == "symbol":
            yield lib_text[pos : form_close + 1]
        pos = form_close + 1


def _symbol_name(symbol_text: str) -> str | None:
    m = re.match(r'\(symbol\s+"([^"]+)"', symbol_text)
    return m.group(1) if m else None
