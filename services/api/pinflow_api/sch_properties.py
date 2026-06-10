"""Read user-set symbol properties (MPN, Datasheet, Manufacturer, …) directly from a `.kicad_sch`.

`kicad-cli sch export netlist --format kicadsexpr` strips user-set symbol
properties — only `Sheetname` / `Sheetfile` / `ki_keywords` / `ki_fp_filters`
survive. To recover MPN and friends we parse the schematic file directly.
We use `kicad_sch_api` (already a runtime dep) rather than hand-rolling.

Output is joined with the netlist by refdes inside `graph.builder.build_design_graph`.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import kicad_sch_api as ksa

from pinflow_api.builders._common import sch_to_string

# Internal keys kicad_sch_api stores alongside user properties; not exported.
_INTERNAL_PROP_PREFIX = "__sexp_"

# Properties that are already on the Component object itself; redundant here.
_INTRINSIC = {"Reference", "Value", "Footprint"}

# BOM / sourcing fields that `set_property` / `set_properties` write back and
# that should not be drawn on the canvas. ksa's `set_property` creates a
# property *visible*, and its `set_property_effects` can't hide a freshly-
# added custom property in 0.5.x (errors same-session, silently no-ops after
# reload), so we stamp `(hide yes)` onto the serialized text — the workaround
# `netlist_to_sch._hide_gnd_labels` also uses.
#
# Only these three — they are *non-standard* fields, so a whole-file text
# scan for them is safe: it can only ever hit instance properties resolve_*
# wrote, never the `lib_symbols` symbol definitions. `Description` and
# `Datasheet` are deliberately excluded: every symbol (and every lib_symbols
# entry) carries them, KiCad already treats them as non-graphic metadata, and
# scanning for `Description` is what let the escaped-quote bug corrupt the
# GND symbol's library description.
_HIDDEN_BOM_KEYS = ("MPN", "LCSC", "Manufacturer")


def _hide_bom_properties(sch_text: str) -> str:
    """Stamp `(hide yes)` onto every `_HIDDEN_BOM_KEYS` property block so the
    sourcing metadata doesn't render as on-canvas text. Idempotent — skips a
    block that already has `(hide ...)` as its first child.

    The value-string match is `"(?:[^"\\]|\\.)*"` — a full S-expression
    string including escaped `\\"` quotes. A naive `"[^"]*"` stops at the
    first escaped quote (KiCad power-symbol descriptions carry `\\"GND\\"`
    and similar), splitting the literal and producing an unterminated
    string."""
    keys = "|".join(re.escape(k) for k in _HIDDEN_BOM_KEYS)
    return re.sub(
        r'^(\t*)(\(property "(?:' + keys + r')" "(?:[^"\\]|\\.)*")(?!\n\t*\(hide)',
        lambda m: f"{m.group(1)}{m.group(2)}\n{m.group(1)}\t(hide yes)",
        sch_text,
        flags=re.MULTILINE,
    )


def parse_properties(sch_path: Path) -> dict[str, dict[str, str]]:
    """Return `{refdes: {property_name: value, ...}}` for each component.

    Multi-unit symbols (e.g. the RP2040 in KLC) appear as multiple Component
    objects with the same reference; we merge their property dicts (last-write
    wins, which is fine because properties are symbol-instance-level and
    identical across units in practice).
    """
    sch = ksa.load_schematic(str(sch_path))
    result: dict[str, dict[str, str]] = {}

    for comp in sch.components:
        ref = getattr(comp, "reference", None)
        if not ref:
            continue
        props = getattr(comp, "properties", None) or {}
        clean: dict[str, str] = {}
        for k, v in props.items():
            if k.startswith(_INTERNAL_PROP_PREFIX) or k in _INTRINSIC:
                continue
            # kicad_sch_api wraps each property as {name, value, hidden, at, effects}.
            # Older shapes might store the value directly — handle both.
            value_str = v["value"] if isinstance(v, dict) and "value" in v else v
            if value_str:
                clean[k] = str(value_str)
        if ref in result:
            result[ref].update(clean)
        else:
            result[ref] = clean

    return result


def get_mpn(props_for_ref: dict[str, str], value_fallback: str | None = None) -> str | None:
    """Resolve an MPN from a single component's property dict.

    Checks `MPN` first (the convention we ask users to set). Falls back to
    `Manufacturer Part Number` (KiCad's BOM column convention from earlier
    versions). Returns `value_fallback` if neither is set — common when the
    user puts the MPN in the Value field.
    """
    for key in ("MPN", "Manufacturer Part Number", "manufacturer_part_number"):
        v = props_for_ref.get(key)
        if v:
            return v
    return value_fallback


def set_property(source: str, refdes: str, key: str, value: str) -> str:
    """Set a user property on a symbol and return the rewritten schematic text.

    Used by `resolve_mpn` to write MPN/Manufacturer/Datasheet back onto the
    staged schematic. The `(property "private" ...)` corruption hazard in
    `kicad_sch_api` 0.5.x is handled by `sch_to_string`'s strip pass.

    Raises `KeyError` if no component with that refdes exists.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        tmp_in = Path(f.name)
        f.write(source)
    try:
        sch = ksa.load_schematic(str(tmp_in))
        target = None
        for comp in sch.components:
            if getattr(comp, "reference", None) == refdes:
                target = comp
                break
        if target is None:
            raise KeyError(f"no component with refdes {refdes!r}")
        target.set_property(key, value)
        return _hide_bom_properties(
            sch_to_string(sch, preserve_lib_symbols_from=source)
        )
    finally:
        tmp_in.unlink(missing_ok=True)


def set_properties(source: str, updates: dict[str, dict[str, str]]) -> str:
    """Batch form of `set_property`: apply many `{refdes: {key: value}}`
    mutations in one schematic load + serialize.

    `resolve_parts` writes MPN/LCSC/Manufacturer/Description onto every
    under-resolved component — doing that through `set_property` would reload
    and re-serialize the whole schematic once per (refdes, key), which is
    O(N·M) parses. This loads once, mutates all, serializes once.

    Refdeses absent from the schematic are skipped silently (a single missing
    part shouldn't sink a batch resolve). Returns the rewritten schematic text;
    returns `source` unchanged if `updates` is empty.
    """
    if not updates:
        return source
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        tmp_in = Path(f.name)
        f.write(source)
    try:
        sch = ksa.load_schematic(str(tmp_in))
        by_ref: dict[str, object] = {}
        for comp in sch.components:
            ref = getattr(comp, "reference", None)
            if ref and ref not in by_ref:
                by_ref[ref] = comp
        for refdes, props in updates.items():
            comp = by_ref.get(refdes)
            if comp is None:
                continue
            for key, value in props.items():
                comp.set_property(key, value)
        return _hide_bom_properties(
            sch_to_string(sch, preserve_lib_symbols_from=source)
        )
    finally:
        tmp_in.unlink(missing_ok=True)
