"""Tool: get_component_profile — cache-hit-or-extract for an MPN profile.

Returns the cached `ComponentProfile` if one exists in the global cache
(`services/api/_components_cache/<safe_mpn>.json`). On cache miss, we
cannot extract without a datasheet PDF — that path requires fetching
the PDF from a URL or having the user upload it, which is a separate
concern deferred to a future datasheet-fetch tool. For now we
surface a structured `needs_datasheet` status so the model can ask.
"""

from __future__ import annotations

from pinflow_api.profile import load_cached, save_cached

SCHEMA = {
    "name": "get_component_profile",
    "description": (
        "Fetch the cached JSON profile for an MPN (description, package, "
        "pintable, recommended passives). Cache hit returns the full profile. "
        "Cache miss returns `needs_datasheet` — ask the user to attach the "
        "datasheet PDF (chat has a paperclip + drag-drop), then call "
        "parse_datasheet(attachment_id, mpn) to populate the cache."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mpn": {"type": "string", "description": "Manufacturer part number."},
        },
        "required": ["mpn"],
    },
}


def run(state, mpn: str = "", **_inputs) -> dict:
    mpn = (mpn or "").strip()
    if not mpn:
        return {"status": "error", "error": "mpn is required"}

    cached = load_cached(mpn)
    if cached is None:
        return {
            "status": "needs_datasheet",
            "mpn": mpn,
            "hint": (
                "No cached profile for this MPN. Ask the user to attach the "
                "datasheet PDF (paperclip + drag-drop in the chat box). When "
                "the next user message contains an [Attachments from user] "
                "marker, call parse_datasheet(attachment_id, mpn) to populate "
                "the cache, then proceed."
            ),
        }

    # Make the profile available to the digest immediately.
    state.profiles_by_mpn[cached.mpn] = cached
    save_cached(cached)  # idempotent — refresh extracted_at format if migrated

    return {
        "status": "ok",
        "mpn": cached.mpn,
        "manufacturer": cached.manufacturer,
        "description": cached.description,
        "package": cached.package,
        "pin_count": len(cached.chosen_pintable),
        "recommended_passives": [rp.model_dump() for rp in cached.recommended_passives],
        "notes": cached.notes,
        "datasheet_url": cached.datasheet_url,
    }
