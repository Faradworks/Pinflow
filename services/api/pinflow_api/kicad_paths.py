"""Resolve KiCad installation paths across platforms, with a user-settable
override for the symbol-library directory.

Why this exists: KiCad's bundled symbol libraries live in different places per
OS and per install method (standalone `.app` vs. system package vs. a custom
prefix). The old code hardcoded the macOS path in two modules; this centralizes
resolution and gives the user an escape hatch when their libraries aren't where
we guess (see `set_symbol_lib_override`).

Symbol-dir precedence:
  1. Runtime override set via the UI / API (persisted in `local_config`)
  2. `KICAD_SYMBOL_DIR` env / `settings.kicad_symbol_dir`
  3. First existing platform-default candidate

When the override is set we honor it even if it doesn't currently exist, so the
status surface can report the miss back to the user instead of silently falling
back to a default that also doesn't have their part.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from . import local_config
from .settings import settings

_OVERRIDE_KEY = "kicad_symbol_dir"


def _default_candidates() -> list[Path]:
    """Platform-default symbol-library directories, in priority order."""
    if sys.platform == "darwin":
        return [Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols")]
    if os.name == "nt":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        base = Path(pf) / "KiCad"
        cands: list[Path] = []
        # KiCad installs under a versioned dir, e.g. C:\Program Files\KiCad\10.0.
        # Prefer the newest version present.
        if base.is_dir():
            for ver in sorted(base.iterdir(), reverse=True):
                cands.append(ver / "share" / "kicad" / "symbols")
        cands.append(base / "share" / "kicad" / "symbols")
        return cands
    # Linux / other POSIX.
    return [
        Path("/usr/share/kicad/symbols"),
        Path("/usr/local/share/kicad/symbols"),
    ]


def symbol_lib_override() -> Optional[str]:
    """The user-set or env-provided symbol-dir override, or None."""
    ov = local_config.get(_OVERRIDE_KEY)
    if ov:
        return str(ov)
    if settings.kicad_symbol_dir:
        return settings.kicad_symbol_dir
    return None


def symbol_lib_dir() -> Path:
    """Resolve the symbol-library directory.

    Override wins (honored even if missing, so the UI can flag it); otherwise
    the first existing platform default, falling back to the first candidate so
    callers always get a concrete Path to report. Callers still guard with
    `.is_dir()` — a missing directory is a valid, reportable state.
    """
    ov = symbol_lib_override()
    if ov:
        return Path(ov).expanduser()
    candidates = _default_candidates()
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0] if candidates else Path("/")


def set_symbol_lib_override(dir_path: Optional[str]) -> None:
    """Persist (or clear, when None/empty) the symbol-dir override and drop the
    resolver's warm index so the next lookup re-scans the new directory."""
    local_config.set(_OVERRIDE_KEY, dir_path or None)
    _invalidate_caches()


def _invalidate_caches() -> None:
    # Imported lazily to avoid a circular import at module load.
    from . import symbol_resolver

    symbol_resolver._index.cache_clear()


def symbol_lib_status() -> dict:
    """Snapshot for the `/kicad/symbol-library` endpoint and settings UI."""
    d = symbol_lib_dir()
    exists = d.is_dir()
    count = len(list(d.glob("*.kicad_sym"))) if exists else 0
    return {
        "dir": str(d),
        "exists": exists,
        "symbol_count": count,
        "override": symbol_lib_override(),
        "defaults": [str(c) for c in _default_candidates()],
    }
