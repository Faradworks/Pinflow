"""Fetch a chip's KiCad symbol from LCSC/EasyEDA via easyeda2kicad.

Used when a chip isn't in KiCad's bundled symbol libraries (e.g. AP2120, niche
LCSC parts). The user provides an LCSC part code in the prompt; we shell out to
easyeda2kicad, cache the result, and surface a synthetic lib_id that
kicad-sch-api can resolve when given the cache directory.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

# Cache lives next to services/api so it persists across reloads but stays
# scoped to the API service. Gitignored.
_CACHE_DIR = Path(__file__).resolve().parent.parent / "_easyeda_cache"


class EasyedaSymbol(NamedTuple):
    lcsc_id: str
    symbol_name: str
    lib_path: Path  # the .kicad_sym file path; the file's stem is the lib name


_LCSC_PATTERN = re.compile(r"\bC\d{3,10}\b")


def detect_lcsc_codes(text: Optional[str]) -> list[str]:
    """Extract LCSC part codes (Cxxxxx) from free-form user text."""
    if not text:
        return []
    return list(dict.fromkeys(_LCSC_PATTERN.findall(text)))


def fetch_lcsc_symbol(lcsc_id: str) -> EasyedaSymbol:
    """Fetch a symbol from LCSC via easyeda2kicad. Caches locally; idempotent."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lib_path = _CACHE_DIR / f"{lcsc_id}.kicad_sym"

    if lib_path.is_file():
        symbol_name = _extract_symbol_name(lib_path.read_text())
        if symbol_name:
            return EasyedaSymbol(lcsc_id=lcsc_id, symbol_name=symbol_name, lib_path=lib_path)

    cli = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "easyeda2kicad"
    if not cli.is_file():
        raise RuntimeError(f"easyeda2kicad CLI not found at {cli}")

    result = subprocess.run(
        [
            str(cli),
            "--lcsc_id", lcsc_id,
            "--symbol",
            "--output", str(lib_path),
            "--overwrite",
            "--use-cache",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 or not lib_path.is_file():
        raise RuntimeError(
            f"easyeda2kicad failed for {lcsc_id}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )

    # Tool prints "Symbol name : <name>" on success
    m = re.search(r"Symbol name\s*:\s*(\S+)", result.stdout)
    symbol_name = m.group(1) if m else _extract_symbol_name(lib_path.read_text())
    if not symbol_name:
        raise RuntimeError(f"could not parse symbol name from {lcsc_id} fetch output")

    return EasyedaSymbol(lcsc_id=lcsc_id, symbol_name=symbol_name, lib_path=lib_path)


def cache_dir() -> Path:
    return _CACHE_DIR


def _extract_symbol_name(lib_text: str) -> Optional[str]:
    m = re.search(r'^\s*\(symbol\s+"([^"]+)"', lib_text, re.MULTILINE)
    return m.group(1) if m else None
