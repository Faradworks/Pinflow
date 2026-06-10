"""MPN-keyed component profile + global cache.

A `ComponentProfile` is the cached datasheet-derived description of one MPN:
description, package, per-package pintables, recommended passives, source
URL + sha256.

Pinouts live only in `pintables` (one entry per distinct package pinout,
keyed by `package_code`). There is no longer a legacy singular `pintable`
mirror — read-side consumers that want "the chosen variant's pins" call
`chosen_pintable`; variant-aware callers pass a `VariantCandidate` to
`pintable_for(variant)`.

Profiles are extracted from `ChipExtract` (the LLM tool-use output of
`datasheet_parse.parse_datasheet`) via `from_chip_extract`. We keep
`ChipExtract` narrow for the LLM and build the richer profile in code.

Cache lives at `services/api/_components_cache/<safe_mpn>.json` — global
across all projects on the user's machine. Per-project mirror is design-doc
Tier 2 and ships later.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from pinflow_api.datasheet_parse import (
    ChipExtract,
    Pin,
    RecommendedPassive,
    VariantCandidate,
    VariantPintable,
)

_EXTRACTOR_VERSION = "1.3.0"  # dropped legacy singular `pintable`; pintables-only


class ComponentProfile(BaseModel):
    """Persisted, MPN-keyed datasheet profile."""

    mpn: str
    manufacturer: str | None = None
    description: str | None = None
    package: str
    # Variant chosen during extraction. New fields default-None so caches written
    # by extractor_version 1.0.0 still load.
    variant_code: str | None = None
    orderable_part: str | None = None
    available_variants: list[VariantCandidate] = Field(default_factory=list)
    datasheet_url: str | None = None
    datasheet_sha256: str | None = None
    # The ONLY pinout store: one entry per distinct package pinout, keyed by
    # `package_code`. `from_chip_extract` guarantees the chosen variant's pins
    # are present (synthesizing a single entry for single-pinout chips whose
    # extract left `pintables` empty), and orders the chosen entry first so the
    # first-entry fallback in `pintable_for` is the chosen variant. Resolve
    # variant-aware reads through `pintable_for(variant)`; for "the chosen
    # variant's pins" with no variant in hand use `chosen_pintable`.
    pintables: list[VariantPintable] = Field(default_factory=list)
    recommended_passives: list[RecommendedPassive] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    extracted_at: str  # ISO date
    extractor_version: str = _EXTRACTOR_VERSION

    def pintable_for(self, variant: VariantCandidate | None = None) -> list[Pin]:
        """Pin map for `variant`.

        Resolution order: exact `package_code` match in `pintables` → the
        chosen variant's (`variant_code`) `pintables` entry → the first
        `pintables` entry. `from_chip_extract` orders the chosen variant
        first, so the final fallback is the chosen pinout — and an unknown
        `package_code` degrades to it instead of raising. Returns `[]` only
        for a profile with no pintables at all (shouldn't happen for a
        profile built via `from_chip_extract`).
        """
        if not self.pintables:
            return []
        if variant is not None and variant.package_code:
            want = variant.package_code.upper()
            for vp in self.pintables:
                if vp.package_code.upper() == want:
                    return vp.pins
        if self.variant_code:
            want = self.variant_code.upper()
            for vp in self.pintables:
                if vp.package_code.upper() == want:
                    return vp.pins
        return self.pintables[0].pins

    @property
    def chosen_pintable(self) -> list[Pin]:
        """Pins of the extraction's chosen variant (`variant_code`), else the
        first pintable. The no-variant accessor for read-side consumers
        (graph/digest) that just want this MPN's canonical pin map."""
        return self.pintable_for(None)


def safe_mpn(mpn: str) -> str:
    """Filename-safe MPN slug. Collisions are tolerable; this is a cache."""
    return re.sub(r"[^A-Za-z0-9._\-]", "_", mpn)


def cache_dir() -> Path:
    """Global component-profile cache root. Created lazily."""
    # services/api/_components_cache/  (alongside _easyeda_cache/)
    api_root = Path(__file__).resolve().parent.parent
    d = api_root / "_components_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_cached(mpn: str) -> ComponentProfile | None:
    """Return the cached profile for `mpn`, or None if missing/unreadable.

    Falls back to longest-prefix match against existing cache stems when the
    exact MPN isn't cached. This lets `XC6206P332MR-G` (orderable part with
    finish suffix) resolve to a cached `XC6206P332MR` or `XC6206` profile,
    and `AMS1117-3.3` resolve to a cached `AMS1117`. Without this, the model
    burns turns retrying parse_datasheet with progressively shorter MPNs.

    Prefix match requires the cached stem to be followed in `mpn` by a
    non-alphanumeric separator (or end of string) so `AMS11` doesn't
    accidentally match a cached `AMS1117`.
    """
    if not mpn:
        return None
    path = cache_dir() / f"{safe_mpn(mpn)}.json"
    if path.is_file():
        try:
            return ComponentProfile.model_validate_json(path.read_text())
        except Exception:
            return None

    requested = safe_mpn(mpn)
    candidates = sorted(
        (f.stem for f in cache_dir().glob("*.json")),
        key=len,
        reverse=True,
    )
    for stem in candidates:
        if not requested.startswith(stem):
            continue
        # Must be a clean boundary, not mid-token (e.g. don't match "AMS11" → "AMS1117").
        if len(requested) > len(stem) and requested[len(stem)].isalnum():
            continue
        try:
            return ComponentProfile.model_validate_json(
                (cache_dir() / f"{stem}.json").read_text()
            )
        except Exception:
            continue
    return None


def list_cached_mpns() -> list[str]:
    """Return all cached MPN stems (sorted), for hint/error surfaces."""
    return sorted(f.stem for f in cache_dir().glob("*.json"))


def save_cached(profile: ComponentProfile) -> Path:
    """Persist `profile` to the global cache. Returns the file path."""
    path = cache_dir() / f"{safe_mpn(profile.mpn)}.json"
    path.write_text(profile.model_dump_json(indent=2) + "\n")
    return path


def from_chip_extract(
    extract: ChipExtract,
    *,
    mpn: str,
    manufacturer: str | None = None,
    description: str | None = None,
    datasheet_url: str | None = None,
    datasheet_bytes: bytes | None = None,
) -> ComponentProfile:
    """Build a `ComponentProfile` from a `ChipExtract` plus caller-supplied identity.

    `datasheet_sha256` is computed from `datasheet_bytes` when supplied.
    """
    sha = hashlib.sha256(datasheet_bytes).hexdigest() if datasheet_bytes else None

    # `pintables` is the only pinout store. The extractor is allowed to leave
    # it empty for single-pinout chips (filling only `pins`), and may omit the
    # chosen variant's entry when it did emit `pintables`. Normalize both: make
    # sure the chosen variant's pins (`extract.pins`) are present, ordered
    # first, so `pintable_for`'s first-entry fallback is the chosen pinout.
    pintables = list(extract.pintables)
    vc = (extract.variant_code or "").upper()
    has_chosen = bool(vc) and any(vp.package_code.upper() == vc for vp in pintables)
    if not has_chosen and extract.pins:
        pintables.insert(
            0,
            VariantPintable(
                package_code=extract.variant_code or "",
                package=extract.package,
                pins=list(extract.pins),
            ),
        )

    return ComponentProfile(
        mpn=mpn,
        manufacturer=manufacturer,
        description=description,
        package=extract.package,
        variant_code=extract.variant_code,
        orderable_part=extract.orderable_part,
        available_variants=list(extract.available_variants),
        datasheet_url=datasheet_url,
        datasheet_sha256=sha,
        pintables=pintables,
        recommended_passives=list(extract.recommended_passives),
        notes=list(extract.notes),
        extracted_at=date.today().isoformat(),
        extractor_version=_EXTRACTOR_VERSION,
    )
