"""Map a chip name (e.g. "TPS62843") to its KiCad library lib_id (e.g.
"Regulator_Switching:TPS628436DRL") by scanning the bundled symbol libraries.

For chips not in KiCad's bundled libraries (e.g. AP2120, niche LCSC parts),
fall back to fetching via easyeda2kicad — the user provides an LCSC part code
in the prompt; we cache the fetched .kicad_sym in `_easyeda_cache/` and emit
a synthetic lib_id pointing at that cache.

kicad-sch-api's `search_symbols` is cache-based and useless for upfront
resolution, so we build our own one-shot index. ~222 files / ~50MB of regex
scans, runs in <1s; `functools.lru_cache` keeps it warm for the process lifetime.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, Optional

from .easyeda import EasyedaSymbol, cache_dir as _easyeda_cache_dir, fetch_lcsc_symbol

if TYPE_CHECKING:
    from .graph.models import DesignGraph

# macOS KiCad 10 bundled libs. Same hardcoded path as sym_lib.py — generalize together.
_BUNDLED_SYMS = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols")


class ResolvedSymbol(NamedTuple):
    lib_id: str
    extra_lib_path: Optional[Path]  # set when the symbol's .kicad_sym is outside bundled libs
    source: Literal["bundled", "easyeda", "in_project"]

# Top-level symbol definitions look like `\t(symbol "Name"` at the start of a line.
# Unit subdefinitions look like `\t\t(symbol "Name_<unit>_<bodystyle>"` — deeper
# indented. Filter unit subdefs by name suffix `_\d+_\d+`.
_SYMBOL_LINE = re.compile(r'^\t\(symbol "([^"]+)"', re.MULTILINE)
_UNIT_SUFFIX = re.compile(r"_\d+_\d+$")


@functools.lru_cache(maxsize=1)
def _index() -> dict[str, str]:
    """Map upper-cased symbol name → first lib_id encountered."""
    out: dict[str, str] = {}
    if not _BUNDLED_SYMS.is_dir():
        return out
    for lib_file in sorted(_BUNDLED_SYMS.glob("*.kicad_sym")):
        try:
            text = lib_file.read_text()
        except Exception:
            continue
        for m in _SYMBOL_LINE.finditer(text):
            name = m.group(1)
            if _UNIT_SUFFIX.search(name):
                continue
            key = name.upper()
            if key not in out:
                out[key] = f"{lib_file.stem}:{name}"
    return out


def resolve(
    chip_name: str,
    package_hint: Optional[str] = None,
    lcsc_codes: Optional[list[str]] = None,
) -> Optional[ResolvedSymbol]:
    """Resolve a chip to a `ResolvedSymbol`. Strategy:

    1. If LCSC codes were provided in the user's prompt, try fetching via
       easyeda2kicad. First successful fetch wins.
    2. Otherwise look up KiCad's bundled symbol libraries by chip name (with
       package-code disambiguation when relevant).
    3. Return None if nothing matches — caller should surface a helpful error
       suggesting an LCSC code.
    """
    if lcsc_codes:
        for code in lcsc_codes:
            try:
                fetched = fetch_lcsc_symbol(code)
            except RuntimeError:
                continue
            return ResolvedSymbol(
                lib_id=f"{fetched.lib_path.stem}:{fetched.symbol_name}",
                extra_lib_path=fetched.lib_path.parent,
                source="easyeda",
            )

    bundled_id = _resolve_bundled(chip_name, package_hint)
    if bundled_id:
        return ResolvedSymbol(lib_id=bundled_id, extra_lib_path=None, source="bundled")

    return None


def resolve_from_design_graph(
    chip_name: str,
    design_graph: Optional["DesignGraph"],
    project_dir: Optional[Path] = None,
) -> Optional[ResolvedSymbol]:
    """Reuse a lib_id from a placed component whose MPN/Value matches `chip_name`.

    Short-circuits the bundled/LCSC lookup when the user's schematic already has
    a placed component for this part — the netlist parser stashes its lib_id on
    `Component.lib_id`, and the symbol's `.kicad_sym` is usually sitting in the
    project directory (e.g. installed via a previous easyeda2kicad fetch). We
    locate that file so `discover_libraries(extra_lib_path)` can make the LLM
    builder's `sch.components.add(lib_id=…)` call succeed.

    Returns None if no placed component matches, or if the matched component's
    lib_id has no on-disk `.kicad_sym` we can point the builder at.
    """
    if design_graph is None:
        return None
    needle = _canon(chip_name)
    if not needle or len(needle) < 4:
        return None

    for comp in design_graph.components.values():
        if not comp.lib_id:
            continue
        for field in (comp.mpn, comp.value):
            cand = _canon(field)
            if not cand or len(cand) < 4:
                continue
            if not (
                cand == needle
                or cand.startswith(needle)
                or needle.startswith(cand)
            ):
                continue
            located = _locate_lib_id(comp.lib_id, project_dir)
            if located is not None:
                return located
            break  # this component matched but had no backing file — try the next
    return None


def _canon(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _locate_lib_id(
    lib_id: str, project_dir: Optional[Path]
) -> Optional[ResolvedSymbol]:
    """Find an on-disk `.kicad_sym` containing the symbol referenced by lib_id.

    Search order: bundled → project dir → easyeda cache. Returns None when no
    file actually holds the named symbol — symbols that only live in a
    schematic's `(lib_symbols ...)` embed have no standalone file to point
    `discover_libraries` at, so they fail this check and fall through.
    """
    library, _, symbol_name = lib_id.partition(":")
    if not library or not symbol_name:
        return None
    needle = f'(symbol "{symbol_name}"'

    bundled = _BUNDLED_SYMS / f"{library}.kicad_sym"
    if _file_contains(bundled, needle):
        return ResolvedSymbol(lib_id=lib_id, extra_lib_path=None, source="bundled")

    if project_dir is not None:
        proj_file = project_dir / f"{library}.kicad_sym"
        if _file_contains(proj_file, needle):
            return ResolvedSymbol(
                lib_id=lib_id, extra_lib_path=project_dir, source="in_project"
            )

    cache = _easyeda_cache_dir() / f"{library}.kicad_sym"
    if _file_contains(cache, needle):
        return ResolvedSymbol(
            lib_id=lib_id, extra_lib_path=cache.parent, source="easyeda"
        )

    return None


def _file_contains(path: Path, needle: str) -> bool:
    if not path.is_file():
        return False
    try:
        return needle in path.read_text()
    except OSError:
        return False


# Back-compat shim — old callers expecting just a string lib_id.
def resolve_lib_id(chip_name: str, package_hint: Optional[str] = None) -> Optional[str]:
    r = resolve(chip_name, package_hint)
    return r.lib_id if r else None


def _resolve_bundled(chip_name: str, package_hint: Optional[str]) -> Optional[str]:
    idx = _index()
    if not idx:
        return None

    upper = chip_name.upper()

    if upper in idx:
        return idx[upper]

    candidates = [
        (name, lib_id) for name, lib_id in idx.items() if name.startswith(upper)
    ]
    if not candidates:
        return None

    if package_hint:
        m = re.search(r"\(([A-Z]{2,4})\)", package_hint)
        if m:
            code = m.group(1)
            preferred = [c for c in candidates if c[0].endswith(code)]
            if preferred:
                preferred.sort(key=lambda c: len(c[0]))
                return preferred[0][1]

    candidates.sort(key=lambda c: len(c[0]))
    return candidates[0][1]


def list_index() -> dict[str, str]:
    """Expose the index for debugging."""
    return dict(_index())


def get_symbol_pins(resolved: ResolvedSymbol) -> Optional[list[dict]]:
    """Read pin definitions off a resolved symbol via kicad-sch-api's cache.

    Returns `[{number, name, type}, ...]` (string values throughout), or None
    if the symbol can't be loaded. Honors `resolved.extra_lib_path` by
    registering it with `discover_libraries` first — needed for easyeda /
    in-project symbols that aren't in the bundled KiCad libs.
    """
    import kicad_sch_api as ksa

    cache = ksa.get_symbol_cache()
    if resolved.extra_lib_path is not None:
        try:
            cache.discover_libraries([resolved.extra_lib_path])
        except Exception:
            pass  # discover_libraries is best-effort; get_symbol may still hit bundled
    try:
        symbol_def = cache.get_symbol(resolved.lib_id)
    except Exception:
        return None
    if symbol_def is None:
        return None
    out: list[dict] = []
    for pin in symbol_def.pins:
        pin_type = pin.pin_type.value if hasattr(pin.pin_type, "value") else str(pin.pin_type)
        out.append({"number": str(pin.number), "name": str(pin.name), "type": pin_type})
    return out
