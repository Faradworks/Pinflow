"""Tool: search_symbols — find KiCad symbols in the installed libraries by keyword.

Distinct from `search_parts` (which queries the LCSC parts catalogue). This
searches the KiCad symbol libraries installed on THIS machine so the model can
discover the exact `lib_id` to put in a netlist — e.g. "USB-C" →
`Connector:USB_C_Receptacle`. Without it the model guesses lib_ids from
training data (KiCad symbol names drift across versions/installs) and burns
turns on `no_symbol` errors.
"""

from __future__ import annotations

import re

from pinflow_api import symbol_resolver

SCHEMA = {
    "name": "search_symbols",
    "description": (
        "Search the KiCad symbol libraries installed on this machine for "
        "symbols matching a keyword, returning exact lib_ids you can drop "
        "into a netlist's `lib_id` field. Call this BEFORE "
        "add_subcircuit_from_netlist whenever you're not 100% sure a symbol "
        "exists — do NOT guess lib_ids from memory; KiCad symbol names vary "
        "by version/install and wrong guesses fail with no_symbol. This "
        "searches LOCAL KiCad symbols, NOT the LCSC parts catalogue (use "
        "search_parts for that). Example: query 'USB-C receptacle' returns "
        "'Connector:USB_C_Receptacle'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Keyword(s), e.g. 'USB-C', 'crystal', 'ESD diode', "
                    "'tactile switch'. Drop package/variant suffixes for "
                    "broader matches."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 15).",
            },
        },
        "required": ["query"],
    },
}


_TOKEN_RE = re.compile(r"[A-Z0-9]+")


def _tokens(s: str) -> list[str]:
    return _TOKEN_RE.findall(s.upper())


def run(state, query: str = "", limit: int = 15, **_inputs) -> dict:
    query = (query or "").strip()
    if not query:
        return {"status": "error", "error": "query is required"}

    q_tokens = _tokens(query)
    if not q_tokens:
        return {"status": "error", "error": "query has no searchable tokens"}

    # Tokens of length >=3 are the discriminators; short ones (e.g. the "C" in
    # "USB-C") match too many symbols to gate on, so they only contribute to
    # ranking, not filtering.
    significant = [t for t in q_tokens if len(t) >= 3]

    index = symbol_resolver.list_index()  # {UPPER_SYMBOL_NAME: "lib:Name"}
    scored: list[tuple[int, int, str]] = []
    for upper_name, lib_id in index.items():
        name_tokens = set(_tokens(upper_name))

        def _hit(tok: str) -> bool:
            return tok in name_tokens or any(tok in nt for nt in name_tokens)

        if significant and not any(_hit(t) for t in significant):
            continue
        matched = sum(1 for t in q_tokens if _hit(t))
        if matched == 0:
            continue
        scored.append((matched, len(upper_name), lib_id))

    if not scored:
        return {
            "status": "no_matches",
            "query": query,
            "hint": (
                f"No installed KiCad symbol matches {query!r}. Try a broader "
                "or differently-worded keyword (drop package/variant "
                "suffixes). If the part genuinely has no bundled symbol, fall "
                "back to a generic symbol (e.g. Device:R, "
                "Connector_Generic:Conn_01xNN) or ask the user."
            ),
        }

    # More matched tokens first, then shorter (more generic) symbol names.
    scored.sort(key=lambda t: (-t[0], t[1]))
    top = scored[: max(1, int(limit or 15))]

    results = []
    for matched, _name_len, lib_id in top:
        library, _, symbol = lib_id.partition(":")
        results.append(
            {
                "lib_id": lib_id,
                "library": library,
                "symbol": symbol,
                "matched_tokens": matched,
            }
        )

    return {
        "status": "ok",
        "query": query,
        "count": len(results),
        "results": results,
        "hint": (
            "Use one of these lib_ids verbatim as a part's `lib_id`. The first "
            "is the closest match. These are real symbols on this machine — no "
            "need to install anything."
        ),
    }
