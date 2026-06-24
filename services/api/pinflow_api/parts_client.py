"""HTTP client for the Pinflow cloud parts API.

Parts queries flow through Pinflow Cloud (`PINFLOW_CLOUD_URL`), which serves
the LCSC/JLCPCB catalogue and gates access on a signed-in Pinflow user. The
Pinflow API service holds no catalogue credentials itself.

Public surface (the `parts` facade calls these):

  - `is_available()` — 60s-cached `/health` probe; False when unconfigured
    (empty `PINFLOW_CLOUD_URL`) or unreachable. The facade then returns no
    candidates so the agent can ask the user for an MPN / datasheet instead.
  - `search_by_mpn(mpn, limit)` — `GET /v1/parts/by-mpn/{mpn}`.
  - `search_keyword(query, ...)` — `GET /v1/parts/search`.
  - `fetch_datasheet_pdf(mpn, ...)` — `GET /v1/parts/datasheet/{mpn}/pdf`.

All response rows pass through `_part_to_candidate` which renames
`lcsc` → `lcsc_code` and drops `category`/`subcategory` so callers continue
to see the same 8-key dict shape `parse_datasheet`/`resolve_parts`/
`search_parts` already consume.

**Auth.** Parts are gated on a signed-in Pinflow user: the session JWT is
sent as `x-api-key` on every request. When no token is held the request goes
out unauthenticated and the service answers with a typed `signin_required`
401, which the agent surfaces as a "sign in (free) for parts" affordance.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import httpx

from pinflow_api import cloud_session
from pinflow_api.settings import settings

_AVAIL_CACHE_SECONDS = 60
# A cloud cold start can take 30-40s for the first request after idle; warm
# requests are sub-second. Use a generous timeout so a cold start doesn't read
# as "catalogue unavailable."
_REQUEST_TIMEOUT = 45.0

_clients: dict[str, httpx.Client] = {}
_client_lock = threading.Lock()
_client_init_error: Optional[str] = None

_avail_cache: tuple[float, bool] = (0.0, False)
_avail_lock = threading.Lock()


def _base_url() -> Optional[str]:
    return (settings.pinflow_cloud_url or "").strip().rstrip("/") or None


def _get_client() -> Optional[httpx.Client]:
    """An httpx.Client for the Pinflow cloud parts API, cached per base URL.
    Auth is attached per-request by `_request_headers()`, so token changes are
    picked up without rebuilding the client."""
    base = _base_url()
    if not base:
        return None
    with _client_lock:
        c = _clients.get(base)
        if c is None:
            c = httpx.Client(base_url=base, timeout=_REQUEST_TIMEOUT)
            _clients[base] = c
        return c


def _request_headers() -> dict:
    """Per-request auth: the Pinflow session JWT as `x-api-key`. Parts are gated
    on a signed-in user — empty when not signed in, which the service answers
    with a typed `signin_required` 401."""
    token = cloud_session.get_token()
    return {"x-api-key": token} if token else {}


def is_available() -> bool:
    """Cheap connectivity check, cached for 60s.

    False when `PINFLOW_CLOUD_URL` is empty (parts disabled) or the `/health`
    probe fails. The `parts` facade treats False as 'serve no catalogue
    results; the agent asks the user for an MPN / datasheet instead.'
    """
    global _avail_cache

    now = time.time()
    with _avail_lock:
        cached_at, cached = _avail_cache
        if now - cached_at < _AVAIL_CACHE_SECONDS:
            return cached

    ok = False
    try:
        client = _get_client()
        if client is not None:
            r = client.get("/health", headers=_request_headers(), timeout=_REQUEST_TIMEOUT)
            ok = r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        ok = False

    with _avail_lock:
        _avail_cache = (now, ok)
    return ok


def signin_required() -> bool:
    """True when parts are gated behind a Pinflow sign-in the user hasn't done:
    Pinflow Cloud is configured (so parts require a signed-in user) but no
    session token is held. The agent surfaces this as a "sign in (free) for
    parts" affordance — distinct from a catalogue outage."""
    return bool(_base_url()) and not cloud_session.get_token()


def init_error() -> Optional[str]:
    """Diagnostic — last reason client init failed."""
    return _client_init_error


def _part_to_candidate(part: dict) -> dict:
    """Map a catalogue `Part` (10-key) to the 8-key candidate dict that
    downstream code (parse_datasheet, resolve_parts, search_parts) expects.

    Renames `lcsc` → `lcsc_code`; drops `category`/`subcategory`. Defensive
    against missing fields — several are Optional.
    """
    return {
        "lcsc_code": part.get("lcsc") or "",
        "mpn": part.get("mpn") or "",
        "manufacturer": part.get("manufacturer") or "",
        "package": part.get("package") or "",
        "description": part.get("description") or "",
        "stock": part.get("stock") or 0,
        "basic": bool(part.get("basic")),
        "preferred": bool(part.get("preferred")),
    }


def search_by_mpn(mpn: str, limit: int = 8) -> list[dict]:
    """MPN → LCSC candidates via `GET /v1/parts/by-mpn/{mpn}`.

    Returns [] on any HTTP failure so the facade can fall back cleanly.
    Same 8-key candidate dict shape the `parts` facade returns.
    """
    needle = (mpn or "").strip()
    if not needle:
        return []

    client = _get_client()
    if client is None:
        return []
    try:
        r = client.get(
            f"/v1/parts/by-mpn/{needle}",
            params={"limit": int(limit)},
            headers=_request_headers(),
        )
        if r.status_code != 200:
            return []
        body = r.json()
        results = body.get("results") or []
    except Exception:
        return []

    return [_part_to_candidate(p) for p in results]


def search_by_mpn_batch(mpns: list[str]) -> dict[str, list[dict]]:
    """Batch EXACT MPN → LCSC candidates via `POST /v1/parts/by-mpn/batch`.

    Keyed by each raw input MPN; an empty list for a miss. Returns {} on any
    HTTP failure so the caller can fall back to per-MPN `search_by_mpn` (which
    does prefix matching, not just exact). Same 8-key candidate dict shape.

    This folds a resolver's N reverse lookups into one round trip (and one
    gateway rate-limit hit) instead of N sequential GETs through the
    gateway → purple-parts chain. Exact match only — the single endpoint's
    prefix arm is intentionally left to the per-seed fallback.
    """
    needles = [m for m in (mpns or []) if (m or "").strip()]
    if not needles:
        return {}

    client = _get_client()
    if client is None:
        return {}
    try:
        r = client.post(
            "/v1/parts/by-mpn/batch",
            json={"mpns": needles},
            headers=_request_headers(),
        )
        if r.status_code != 200:
            return {}
        results = r.json().get("results") or {}
    except Exception:
        return {}

    return {
        raw: [_part_to_candidate(p) for p in (cands or [])]
        for raw, cands in results.items()
    }


def fetch_datasheet_pdf(
    mpn: str,
    *,
    variant_hint: Optional[str] = None,
    manufacturer: Optional[str] = None,
) -> Optional[bytes]:
    """Auto-download the datasheet PDF for `mpn` via
    `GET /v1/parts/datasheet/{mpn}/pdf`.

    The service proxies DigiKey's Product Information API (exact MPN match
    only) and validates `%PDF-` magic + a min-size floor before returning,
    so a non-None result is a real PDF.

    Tries the variant-suffixed MPN first when `variant_hint` is provided
    (DigiKey indexes orderable parts, so e.g. `TPS62840DLCR` matches where
    `TPS62840` may not), then falls back to the bare canonical MPN.

    When `manufacturer` is supplied (free-text, as the user would write it,
    e.g. "Texas Instruments" or "Raspberry Pi"), it is passed through as
    `?manufacturer=…`; the service uses it as a fallback search filter when
    the exact-MPN lookup 404s (handles parts indexed under a distributor
    product code rather than the marketing MPN).

    Returns None on any HTTP failure — 404 (no DigiKey match), 502
    (upstream transport), 503 (creds unconfigured server-side), or a
    network error. Callers should fall back to asking the user.
    """
    needle = (mpn or "").strip()
    if not needle:
        return None

    client = _get_client()
    if client is None:
        return None

    candidates: list[str] = []
    vh = (variant_hint or "").strip()
    if vh:
        if vh.upper().startswith(needle.upper()):
            candidates.append(vh)
        else:
            candidates.append(needle + vh)
    candidates.append(needle)

    mfr = (manufacturer or "").strip()
    params = {"manufacturer": mfr} if mfr else None

    for cand in candidates:
        try:
            r = client.get(
                f"/v1/parts/datasheet/{cand}/pdf",
                headers=_request_headers(),
                params=params,
            )
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            continue
    return None


def search_keyword(
    query: str,
    *,
    limit: int = 25,
    require_stock: bool = True,
    package: Optional[str] = None,
) -> list[dict]:
    """Keyword search via `GET /v1/parts/search`. Returns [] on HTTP failure."""
    q = (query or "").strip()
    if not q:
        return []

    client = _get_client()
    if client is None:
        return []

    params: dict = {
        "keyword": q,
        "limit": int(limit),
        "require_stock": "true" if require_stock else "false",
    }
    if package:
        params["package"] = package

    try:
        r = client.get(
            "/v1/parts/search",
            params=params,
            headers=_request_headers(),
        )
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
    except Exception:
        return []

    return [_part_to_candidate(p) for p in results]
