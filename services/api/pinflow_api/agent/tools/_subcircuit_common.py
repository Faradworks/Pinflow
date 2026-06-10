"""Shared helpers for the subcircuit-insert tools.

`load_target_schematic` resolves the staged-or-real `.kicad_sch` for the
active path; `resolve_and_validate_for_variant` resolves a KiCad symbol for
the chosen variant and verifies its pin shape matches the cached pintable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import kicad_sch_api as ksa

from pinflow_api import easyeda, staging, symbol_resolver
from pinflow_api.datasheet_parse import VariantCandidate
from pinflow_api.symbol_resolver import ResolvedSymbol

if TYPE_CHECKING:
    from pinflow_api.graph.models import DesignGraph
    from pinflow_api.profile import ComponentProfile


class SymbolMismatch(RuntimeError):
    """Raised when no resolved symbol's pin shape matches the chosen pintable.

    `detail` carries a structured payload suitable for surfacing in tool
    results (which variants were tried, what pin numbers were missing, the
    lib_ids that came back from the resolver).
    """

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


def load_target_schematic(real_path: Path) -> tuple[ksa.Schematic, str]:
    """Return `(schematic, source_text)` for the active path — staged if present.

    `source_text` is the raw `.kicad_sch` text we loaded from; pass it to
    `sch_to_string(..., preserve_lib_symbols_from=source_text)` so inline-only
    symbol definitions (copy-pasted parts, embedded vendor symbols) survive
    the kicad-sch-api round-trip (which otherwise rebuilds `(lib_symbols ...)`
    from its on-disk cache and drops anything not resolvable on disk).
    """
    stage = staging.get(real_path)
    if stage is not None and stage.temp_path.is_file():
        sch = ksa.load_schematic(str(stage.temp_path))
        return sch, stage.temp_path.read_text(encoding="utf-8")
    if real_path.stat().st_size == 0 or real_path.read_text().strip() == "":
        return ksa.Schematic.create(), ""
    text = real_path.read_text(encoding="utf-8")
    sch = ksa.load_schematic(str(real_path))
    return sch, text


def _pintable_pin_numbers(pintable) -> set[str]:
    """Pin numbers in the datasheet pintable, normalized to strings."""
    return {str(p.number) for p in pintable}


def _try_resolve(
    chip_name: str,
    package_hint: Optional[str],
    project_dir: Optional[Path],
    design_graph: Optional["DesignGraph"],
) -> Optional[ResolvedSymbol]:
    """Try design-graph reuse first, then bundled-lib resolve."""
    if design_graph is not None:
        reused = symbol_resolver.resolve_from_design_graph(
            chip_name=chip_name,
            design_graph=design_graph,
            project_dir=project_dir,
        )
        if reused is not None:
            return reused
    return symbol_resolver.resolve(chip_name=chip_name, package_hint=package_hint)


def resolve_and_validate_for_variant(
    profile: "ComponentProfile",
    *,
    chosen_variant: Optional[VariantCandidate],
    project_dir: Optional[Path],
    design_graph: Optional["DesignGraph"],
    lcsc_codes: Optional[list[str]] = None,
) -> tuple[ResolvedSymbol, list[dict], Optional[VariantCandidate]]:
    """Resolve a symbol for the chosen variant and verify pin-shape match.

    Strategy:
      1. Try resolving `chosen_variant`'s orderable_part + package_hint. If the
         resolved symbol's pin numbers cover *that variant's* pintable, return.
      2. Otherwise iterate `profile.available_variants` (skipping the already-
         tried one) — for each, try the resolver with that orderable_part +
         package_hint. Each candidate's pin-match target is resolved per
         variant via `profile.pintable_for(variant)`: package families can
         genuinely renumber pins (SOT vs BGA), so the needle is variant-
         specific, not a single shared set.
      3. Fall back once to the bare `profile.mpn` (no variant suffix) with the
         profile's own `package` hint — covers the single-variant case;
         needle is the legacy `pintable` (`pintable_for(None)`).
      4. If `lcsc_codes` was passed, try each via `easyeda2kicad` (drop one
         in the easyeda cache, validate pins against the target variant's
         pintable). First match wins.
      5. Raise `SymbolMismatch` if nothing matched.

    Returns `(resolved, symbol_pins, variant_used)`. `variant_used` is None
    when the fallback bare-MPN or LCSC resolve was the one that succeeded.
    """
    tried: list[dict] = []

    def _attempt(
        chip_name: str,
        package_hint: Optional[str],
        variant: Optional[VariantCandidate],
    ) -> Optional[tuple[ResolvedSymbol, list[dict]]]:
        # Needle is resolved per variant: package families can renumber pins.
        # `pintable_for(None)` degrades to the legacy singular pintable.
        needle_pins = _pintable_pin_numbers(profile.pintable_for(variant))
        resolved = _try_resolve(chip_name, package_hint, project_dir, design_graph)
        if resolved is None:
            tried.append({"chip": chip_name, "package_hint": package_hint, "result": "no_symbol"})
            return None
        symbol_pins = symbol_resolver.get_symbol_pins(resolved)
        if symbol_pins is None:
            tried.append({"chip": chip_name, "lib_id": resolved.lib_id, "result": "no_pins_introspected"})
            return None
        symbol_numbers = {p["number"] for p in symbol_pins}
        missing = needle_pins - symbol_numbers
        if missing:
            tried.append({
                "chip": chip_name, "lib_id": resolved.lib_id,
                "result": "pin_mismatch",
                "missing_pins": sorted(missing),
                "symbol_pin_count": len(symbol_numbers),
                "pintable_pin_count": len(needle_pins),
                "variant": variant.package_code if variant else None,
            })
            return None
        return (resolved, symbol_pins)

    candidates: list[VariantCandidate] = []
    if chosen_variant is not None:
        candidates.append(chosen_variant)
    for v in profile.available_variants:
        if chosen_variant is None or v.package_code != chosen_variant.package_code:
            candidates.append(v)

    for variant in candidates:
        package_hint = f"{variant.package} ({variant.package_code})"
        attempt = _attempt(variant.orderable_part, package_hint, variant)
        if attempt is not None:
            resolved, symbol_pins = attempt
            return resolved, symbol_pins, variant

    # Single-variant or unannotated case — bare MPN + profile.package.
    attempt = _attempt(profile.mpn, profile.package, None)
    if attempt is not None:
        resolved, symbol_pins = attempt
        return resolved, symbol_pins, None

    # LCSC fallback — caller passes candidate codes after running an
    # MPN search (see pinflow_api.parts). easyeda2kicad fetches the
    # symbol into the cache; we wrap it in a ResolvedSymbol and validate.
    # No VariantCandidate for an LCSC code → validate against the variant the
    # user is targeting (`pintable_for(chosen_variant)`; legacy when None).
    needle_pins = _pintable_pin_numbers(profile.pintable_for(chosen_variant))
    for code in (lcsc_codes or []):
        try:
            fetched = easyeda.fetch_lcsc_symbol(code)
        except Exception as e:
            tried.append({"lcsc_code": code, "result": "fetch_failed", "error": str(e)[:200]})
            continue
        resolved = ResolvedSymbol(
            lib_id=f"{fetched.lib_path.stem}:{fetched.symbol_name}",
            extra_lib_path=fetched.lib_path.parent,
            source="easyeda",
        )
        symbol_pins = symbol_resolver.get_symbol_pins(resolved)
        if symbol_pins is None:
            tried.append({"lcsc_code": code, "lib_id": resolved.lib_id, "result": "no_pins_introspected"})
            continue
        missing = needle_pins - {p["number"] for p in symbol_pins}
        if missing:
            tried.append({
                "lcsc_code": code, "lib_id": resolved.lib_id,
                "result": "pin_mismatch",
                "missing_pins": sorted(missing),
                "symbol_pin_count": len(symbol_pins),
                "pintable_pin_count": len(needle_pins),
            })
            continue
        return resolved, symbol_pins, None

    raise SymbolMismatch(
        message=(
            f"No KiCad symbol resolved for {profile.mpn} that covers the "
            f"datasheet pintable's {len(needle_pins)} pin numbers."
        ),
        detail={
            "mpn": profile.mpn,
            "pintable_pin_count": len(needle_pins),
            "attempts": tried,
            "hint": (
                "Provide an LCSC code in your next message, install a symbol "
                "via install_symbol_to_project, or pick a different variant "
                "with variant_hint."
            ),
        },
    )
