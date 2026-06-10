"""Parts catalogue dispatcher.

Thin facade over the Pinflow cloud parts API (via `parts_client`). All
callers — `parse_datasheet.py` on resolver miss, the `search_parts` agent
tool, `resolve_parts` — should import this module instead of `parts_client`
directly so the catalogue source stays swappable.

The MPN-lookup contract (`search_by_mpn`) returns the 8-key dict shape
downstream code already consumes; the HTTP-to-dict translation lives in
`parts_client._part_to_candidate`. Both MPN and keyword search return []
when the parts catalogue is unconfigured/unreachable, so the agent degrades to
asking the user for an MPN or datasheet PDF rather than failing. Callers
can check `is_cloud_available()` before promising the model a result.
"""

from __future__ import annotations

from typing import Optional

from pinflow_api import parts_client


def is_available() -> bool:
    """True if the parts catalogue can answer MPN lookups."""
    return parts_client.is_available()


def is_cloud_available() -> bool:
    """True only if the cloud parts backend is reachable. Required for
    keyword search."""
    return parts_client.is_available()


def signin_required() -> bool:
    """True when the catalogue is gated specifically because the user isn't signed
    in (cloud mode, no Pinflow session) — so a parts tool can offer a free sign-in
    rather than just reporting 'unavailable'. See `parts_client.signin_required`."""
    return parts_client.signin_required()


def search_by_mpn(mpn: str, limit: int = 8) -> list[dict]:
    """Look up LCSC candidates by MPN via the parts catalogue. [] when unavailable."""
    if not parts_client.is_available():
        return []
    return parts_client.search_by_mpn(mpn, limit)


def fetch_datasheet_pdf(
    mpn: str,
    *,
    variant_hint: Optional[str] = None,
    manufacturer: Optional[str] = None,
) -> Optional[bytes]:
    """Auto-download a datasheet PDF for `mpn` via the parts catalogue (DigiKey
    proxy). Returns None when the backend is unavailable or DigiKey
    has no match — callers should fall back to asking the user.

    Canonical-MPN bridge: DigiKey only indexes orderable parts (e.g.
    `TPS62840DLCR`, not the canonical family `TPS62840`). The agent loop
    usually calls with the canonical MPN and no `variant_hint`, so when
    that direct lookup misses we chain through `search_by_mpn` to recover
    the orderable variants (the catalogue's `/v1/parts/by-mpn/{mpn}` returns
    these) and retry the fetch for each — first hit wins. Skipped when
    `variant_hint` is supplied (the caller has already committed to a
    specific orderable; don't second-guess them).

    `manufacturer` (free-text, e.g. "Texas Instruments", "Raspberry Pi") is
    forwarded to the catalogue as a fallback search filter for parts whose
    marketing MPN differs from DigiKey's product code (e.g. RP2040 listed
    as SC0914(7)). Applied on the first call and on each search_by_mpn
    bridge retry."""
    if not parts_client.is_available():
        return None

    pdf = parts_client.fetch_datasheet_pdf(
        mpn, variant_hint=variant_hint, manufacturer=manufacturer
    )
    if pdf is not None:
        return pdf

    # Only bridge when no variant hint was supplied — a hint is the
    # caller's best guess at the orderable, so respect it.
    if variant_hint:
        return None

    canonical = (mpn or "").strip()
    if not canonical:
        return None

    try:
        candidates = parts_client.search_by_mpn(canonical, limit=3)
    except Exception:
        return None

    for cand in candidates:
        orderable = (cand.get("mpn") or "").strip()
        if not orderable:
            continue
        if orderable.upper() == canonical.upper():
            continue
        pdf = parts_client.fetch_datasheet_pdf(orderable, manufacturer=manufacturer)
        if pdf is not None:
            return pdf
    return None


def search_keyword(
    query: str,
    *,
    limit: int = 25,
    require_stock: bool = True,
    package: Optional[str] = None,
) -> list[dict]:
    """Keyword search across the catalogue. Cloud-only — returns
    [] when unavailable so the caller can return a clean 'unavailable'
    status to the model rather than silently falling back to a path
    that can't serve the query."""
    if not parts_client.is_available():
        return []
    return parts_client.search_keyword(
        query, limit=limit, require_stock=require_stock, package=package
    )
