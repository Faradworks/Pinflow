"""Tool: search_parts — keyword + filter search across the LCSC catalogue.

Backed by the Pinflow cloud parts API via `parts.search_keyword`
(Phase 1: tsvector + pg_trgm, blended with preferred/basic/in-stock
boosts). Returns the same 8-key candidate dict shape that
`parse_datasheet`'s LCSC fallback produces, so the model can hand a
chosen `lcsc_code` back to `parse_datasheet(lcsc_code=...)` or
`add_subcircuit_from_netlist`.

When the user isn't signed in (cloud mode), returns
`{"status": "signin_required", ...}` so the model offers a free sign-in;
when the backend is otherwise unreachable, `{"status": "unavailable", ...}`.
Either way the model surfaces the limitation instead of pretending a result.
"""

from __future__ import annotations

from typing import Optional

from pinflow_api import parts as parts_facade

SCHEMA = {
    "name": "search_parts",
    "description": (
        "Keyword search across the LCSC parts catalogue. Use for "
        "function-driven queries the user phrases without an exact MPN, "
        "e.g. '3.3V LDO with shutdown SOT-23-5', '24-bit ADC SPI', "
        "'low-side gate driver SOIC-8'. Returns ranked candidates with "
        "lcsc_code, mpn, manufacturer, package, description, stock, "
        "basic, preferred. Hand a chosen lcsc_code to parse_datasheet "
        "via the lcsc_code= argument. For exact MPN lookup, prefer "
        "parse_datasheet directly (it does its own MPN → LCSC lookup)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "Free-text query. Combine function, voltage, package, "
                    "and notable features, e.g. '3.3V LDO shutdown SOT-23-5'."
                ),
            },
            "package": {
                "type": "string",
                "description": (
                    "Optional package filter (substring match against the "
                    "catalogue's package field), e.g. 'SOT-23-5', 'SOIC-8', "
                    "'QFN-32'. Leave unset to search all packages."
                ),
            },
            "require_stock": {
                "type": "boolean",
                "description": (
                    "When true (default), only return parts JLCPCB has in "
                    "stock — useful for orderability. Set false to surface "
                    "out-of-stock parts as well."
                ),
                "default": True,
            },
            "limit": {
                "type": "integer",
                "description": "Max candidates to return (default 10, cap 50).",
                "default": 10,
            },
        },
        "required": ["keyword"],
    },
}


def run(
    state,
    keyword: str = "",
    package: Optional[str] = None,
    require_stock: bool = True,
    limit: int = 10,
    **_inputs,
) -> dict:
    kw = (keyword or "").strip()
    if not kw:
        return {"status": "error", "error": "keyword is required"}

    if parts_facade.signin_required():
        return {
            "status": "signin_required",
            "hint": (
                "Parts search needs a free Pinflow sign-in — the user is in cloud "
                "mode but not signed in. Tell them they can sign in (free, no card) "
                "to turn on automatic parts search and datasheet lookup — OR give "
                "you an exact MPN / attach the datasheet PDF and you'll continue "
                "from that. Don't block: offer both and proceed with whatever they "
                "provide."
            ),
        }

    if not parts_facade.is_cloud_available():
        return {
            "status": "unavailable",
            "hint": (
                "Parts search is temporarily unavailable. Ask the user for an "
                "exact MPN or LCSC code (parse_datasheet can resolve those "
                "directly), or have them attach the datasheet PDF, and continue "
                "from there."
            ),
        }

    capped = max(1, min(int(limit or 10), 50))
    pkg = (package or "").strip() or None
    results = parts_facade.search_keyword(
        kw,
        limit=capped,
        require_stock=bool(require_stock),
        package=pkg,
    )

    return {
        "status": "ok",
        "keyword": kw,
        "package_filter": pkg,
        "require_stock": bool(require_stock),
        "result_count": len(results),
        "results": results,
        "hint": (
            "Pick one lcsc_code and call parse_datasheet(mpn=<the mpn>, "
            "lcsc_code=<that code>, attachment_id=<datasheet PDF if you "
            "have one>) to wire the part into a subcircuit. If multiple "
            "candidates look plausible, ask_user to choose."
        ),
    }
