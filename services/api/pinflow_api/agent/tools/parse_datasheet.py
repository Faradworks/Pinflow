"""Tool: parse_datasheet — datasheet PDF (or cached profile) → resolved
KiCad symbol, ready for `design_spec`.

This is the only datasheet entry point. It handles both cold and warm cache:

  - Cold (no cache or different variant requested): requires `attachment_id`.
    LLM-A extracts a variant-aware `ChipExtract` from the PDF; profile is
    written to the global cache.
  - Warm (cache hit and variant_hint matches the cached variant, or no hint):
    skips LLM-A and uses the cached profile.

In both cases, the tool then:
  - Resolves a KiCad symbol for the chosen variant.
  - Validates the symbol's pin numbers cover that variant's pintable. If the
    chosen variant fails, tries the other available_variants before giving up.
  - Stashes the resolved symbol + chosen variant on the conversation
    (`state.resolved_symbols[mpn]`) and returns `status:"profile_ready"`
    with a hint to call `design_spec` next.

It no longer synthesizes a netlist — `design_spec` runs the deterministic
equation pass and drives `netlist_synth`. Chain:
`parse_datasheet` → `design_spec` → ask_user(Confirm/Discard) →
`add_subcircuit_from_netlist`. `add_subcircuit_from_datasheet` is deleted —
this tool subsumes both the cold and warm paths.
"""

from __future__ import annotations

from typing import Optional

from pinflow_api import parts as parts_facade, profile as profile_mod, symbol_resolver
from pinflow_api.datasheet_parse import VariantCandidate, parse_datasheet as _parse_pdf
from pinflow_api.agent import attachments as _attachments
from pinflow_api.agent.tools._subcircuit_common import (
    SymbolMismatch,
    resolve_and_validate_for_variant,
)

SCHEMA = {
    "name": "parse_datasheet",
    "description": (
        "Step 1 of the subcircuit chain: datasheet → cached profile + "
        "resolved KiCad symbol for one IC. Handles both cold (needs PDF) "
        "and warm (cached profile) paths. On success returns "
        "status:'profile_ready' — then call "
        "design_spec(mpn=…, topology=…, vin=…, vout=…) to compute "
        "component values; do NOT call add_subcircuit_from_netlist "
        "directly (this tool no longer returns a netlist — design_spec "
        "produces it). If status:'needs_datasheet' is returned, ask the "
        "user to attach the datasheet PDF in plain text and end the turn "
        "— do NOT call ask_user for file attachments."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mpn": {
                "type": "string",
                "description": (
                    "Canonical MPN without variant/reel suffixes, e.g. "
                    "'TPS62840' (not 'TPS62840DLCR'). Use `variant_hint` "
                    "for the variant selection."
                ),
            },
            "attachment_id": {
                "type": "string",
                "description": (
                    "Attachment id from a recent `[Attachments from user]` "
                    "marker. Required when no cached profile exists for "
                    "`mpn`, or when `variant_hint` doesn't match the cached "
                    "variant. Reuse the same id across turns within a "
                    "conversation if the PDF is already on file."
                ),
            },
            "variant_hint": {
                "type": "string",
                "description": (
                    "Package code (e.g. 'DLC', 'YBG') or full orderable "
                    "part (e.g. 'TPS62840DLCR') to bias variant selection. "
                    "Leave unset to let the extractor pick the most "
                    "commonly stocked / non-BGA default."
                ),
            },
            "manufacturer": {
                "type": "string",
                "description": "Optional manufacturer name (e.g. 'Texas Instruments').",
            },
            "description": {
                "type": "string",
                "description": "Optional one-line description (e.g. '1.8V buck regulator').",
            },
            "extraction_hint": {
                "type": "string",
                "description": (
                    "Optional prompt to bias the extraction — e.g. which "
                    "subcircuit / which recommended values to prefer when "
                    "the datasheet covers multiple configurations."
                ),
            },
            "lcsc_code": {
                "type": "string",
                "description": (
                    "LCSC part code (e.g. 'C123456') to force-use as the "
                    "symbol source. Set this only after a prior call "
                    "returned status:'needs_lcsc_choice' AND the user "
                    "picked one of the candidate codes via ask_user. "
                    "Bypasses the bundled-library lookup entirely."
                ),
            },
            "role": {
                "type": "string",
                "description": "Block role, e.g. 'buck regulator', 'mcu', 'gate driver'.",
            },
            "vin": {
                "type": "string",
                "description": "Input rail name as it will appear in the schematic, e.g. '+5V'.",
            },
            "vout": {
                "type": "string",
                "description": "Output rail name, e.g. '+3V3'.",
            },
            "port_bindings": {
                "type": "object",
                "description": (
                    "Map of default port net name → user-facing rail name "
                    "(e.g. {'VOUT':'+4V5','GND':'GND'}). The netlist "
                    "synthesizer applies these to the boundary nets."
                ),
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["mpn"],
    },
}


def _matches_variant(prof: "profile_mod.ComponentProfile", hint: str) -> bool:
    """Does the cached profile already represent the variant the user wants?

    The hint may name a variant_code (`MR-G`), an orderable_part
    (`XC6206P332MR-G`), or a *package* (`SOT-23`) — the model commonly passes
    the package. Match any of them, against both the chosen variant and every
    available variant, so a cached profile isn't needlessly re-extracted.
    """
    hint_u = hint.strip().upper()
    if not hint_u:
        return True
    if prof.variant_code and prof.variant_code.upper() == hint_u:
        return True
    if prof.orderable_part and prof.orderable_part.upper() == hint_u:
        return True
    if prof.package and hint_u in prof.package.upper():
        return True
    for v in prof.available_variants:
        if hint_u in (
            v.package_code.upper(),
            v.orderable_part.upper(),
        ) or hint_u == (v.package or "").upper():
            return True
    return False


def _pick_variant(
    prof: "profile_mod.ComponentProfile", hint: Optional[str]
) -> Optional[VariantCandidate]:
    """Pick a VariantCandidate from the cached profile.

    hint > cached chosen variant > first available > None.
    """
    hint_u = (hint or "").strip().upper()
    if hint_u:
        for v in prof.available_variants:
            if (
                v.package_code.upper() == hint_u
                or v.orderable_part.upper() == hint_u
                or (v.package or "").upper() == hint_u
            ):
                return v
    # Cached chosen variant first
    if prof.variant_code:
        for v in prof.available_variants:
            if v.package_code == prof.variant_code:
                return v
        if prof.orderable_part:
            return VariantCandidate(
                orderable_part=prof.orderable_part,
                package=prof.package,
                package_code=prof.variant_code,
                pin_count=len(prof.pintable_for(None)),
            )
    # First available_variant as a last resort
    if prof.available_variants:
        return prof.available_variants[0]
    return None


def run(
    state,
    mpn: str = "",
    attachment_id: str = "",
    variant_hint: Optional[str] = None,
    manufacturer: Optional[str] = None,
    description: Optional[str] = None,
    extraction_hint: Optional[str] = None,
    lcsc_code: Optional[str] = None,
    role: Optional[str] = None,
    vin: Optional[str] = None,
    vout: Optional[str] = None,
    port_bindings: Optional[dict] = None,
    **_inputs,
) -> dict:
    mpn = (mpn or "").strip()
    aid = (attachment_id or "").strip()
    if not mpn:
        return {"status": "error", "error": "mpn is required"}

    prof = profile_mod.load_cached(mpn)

    need_reextract = (
        prof is None
        or (variant_hint and not _matches_variant(prof, variant_hint))
        or bool(aid)  # caller explicitly handed a PDF → re-extract (idempotent overwrite)
    )

    if need_reextract:
        if not aid:
            # Auto-fetch from the parts catalogue (DigiKey proxy) before asking the
            # user. Saves the user a manual upload step when DigiKey has the
            # datasheet on file; transparent fall-through to needs_datasheet
            # on miss preserves the existing UX.
            pdf_bytes = parts_facade.fetch_datasheet_pdf(
                mpn, variant_hint=variant_hint, manufacturer=manufacturer
            )
            if pdf_bytes:
                auto_ref = _attachments.save(
                    state.conversation_id,
                    filename=f"{mpn}.pdf",
                    mime="application/pdf",
                    data=pdf_bytes,
                )
                state.attachments[auto_ref.attachment_id] = auto_ref
                aid = auto_ref.attachment_id
            else:
                cached = profile_mod.list_cached_mpns()
                needs_signin = parts_facade.signin_required()
                if needs_signin:
                    reason = (
                        " — automatic datasheet fetch needs a free Pinflow sign-in "
                        "(the user is in cloud mode but not signed in)"
                    )
                    fallback = (
                        "Tell the user they can sign in (free, no card) to turn on "
                        "automatic datasheet + parts lookup, OR attach the datasheet "
                        "PDF (paperclip in the chat box, or drag it in). Offer both, "
                        "then END THE TURN."
                    )
                else:
                    reason = " and no datasheet was found for it automatically"
                    fallback = (
                        "Otherwise ask the user to attach the datasheet PDF "
                        "(paperclip in the chat box, or drag it in) and END THE "
                        "TURN. Do NOT keep guessing other MPNs — every guess is a "
                        "wasted turn."
                    )
                return {
                    "status": "needs_datasheet",
                    "mpn": mpn,
                    "variant_hint": variant_hint,
                    "cached_mpns": cached,
                    "signin_required": needs_signin,
                    "hint": (
                        f"No cached profile for {mpn}"
                        + (f" with variant {variant_hint}" if variant_hint else "")
                        + reason
                        + ". Cached MPNs available: "
                        + (", ".join(cached) if cached else "(none)")
                        + ". If the user's MPN is a longer form of one of these "
                        "(e.g. orderable part with package/finish suffix), retry "
                        "parse_datasheet with the SHORTER canonical MPN — the "
                        "loader auto-strips suffixes, but only if you call it "
                        "with the right base. " + fallback
                    ),
                }
        ref = state.attachments.get(aid)
        if ref is None:
            return {
                "status": "no_such_attachment",
                "attachment_id": aid,
                "hint": (
                    "Pinflow has no record of that attachment_id on this "
                    "conversation. Ask the user to re-attach the PDF."
                ),
            }
        if "pdf" not in (ref.mime or "").lower() and not ref.filename.lower().endswith(".pdf"):
            return {
                "status": "wrong_mime",
                "attachment_id": aid,
                "filename": ref.filename,
                "mime": ref.mime,
                "hint": "parse_datasheet only accepts PDFs.",
            }
        try:
            pdf_bytes = ref.path.read_bytes()
        except Exception as e:
            return {"status": "read_failed", "error": f"{type(e).__name__}: {e}"}
        try:
            extract = _parse_pdf(
                pdf_bytes,
                user_prompt=extraction_hint or None,
                variant_hint=variant_hint or None,
            )
        except Exception as e:
            return {"status": "extract_failed", "error": f"{type(e).__name__}: {e}"}

        prof = profile_mod.from_chip_extract(
            extract,
            mpn=mpn,
            manufacturer=manufacturer,
            description=description,
            datasheet_bytes=pdf_bytes,
        )
        try:
            profile_mod.save_cached(prof)
        except Exception as e:
            return {"status": "cache_failed", "error": f"{type(e).__name__}: {e}"}
        ref.parsed_mpn = prof.mpn

    # From here, `prof` is populated (warm cache or freshly extracted).
    # Register the profile on the conversation under the model-supplied `mpn`
    # — the same key `resolved_symbols` and `design_spec` use. This must run
    # on the warm-cache path too, not just fresh extraction: otherwise
    # design_spec's `profiles_by_mpn.get(mpn)` misses and it dead-loops on
    # needs_parse_datasheet while parse_datasheet keeps returning profile_ready.
    state.profiles_by_mpn[mpn] = prof
    chosen_variant = _pick_variant(prof, variant_hint)
    project_dir = state.active_sch_path.parent if state.active_sch_path else None

    # If the user already picked an LCSC code (after a needs_lcsc_choice
    # round-trip), force-use it; skip the bundled-library chase.
    forced_lcsc = [lcsc_code.strip()] if lcsc_code and lcsc_code.strip() else None

    try:
        resolved, symbol_pins, variant_used = resolve_and_validate_for_variant(
            prof,
            chosen_variant=chosen_variant,
            project_dir=project_dir,
            design_graph=state.design_graph,
            lcsc_codes=forced_lcsc,
        )
    except SymbolMismatch as e:
        if forced_lcsc:
            # User picked an LCSC code; if even that didn't validate, the
            # picked part has the wrong pin shape for this datasheet.
            # Surface the error so they can pick a different candidate.
            return {"status": "symbol_mismatch", "lcsc_code": lcsc_code, **e.detail}

        # Bundled / design-graph paths exhausted. Try the parts catalogue
        # (the parts catalogue) for an orderable LCSC part — easyeda2kicad can then
        # fetch its symbol.
        if not parts_facade.is_available():
            return {
                "status": "symbol_mismatch",
                **e.detail,
                "catalogue_available": False,
                "hint": (
                    e.detail.get("hint", "")
                    + " No bundled KiCad symbol matched this part's pinout, "
                    "and the parts catalogue isn't reachable to find an "
                    "orderable LCSC part (which would bring its own symbol). "
                    "Ask the user for an LCSC code (e.g. C12345) — "
                    "parse_datasheet(lcsc_code=…) fetches the symbol directly "
                    "— or for a specific orderable part number."
                ).strip(),
            }

        seeds: list[str] = []
        if chosen_variant is not None:
            seeds.append(chosen_variant.orderable_part)
        if prof.orderable_part and prof.orderable_part not in seeds:
            seeds.append(prof.orderable_part)
        if prof.mpn not in seeds:
            seeds.append(prof.mpn)

        candidates: list[dict] = []
        seen: set[str] = set()
        for seed in seeds:
            for cand in parts_facade.search_by_mpn(seed, limit=8):
                code = cand["lcsc_code"]
                if code in seen:
                    continue
                seen.add(code)
                candidates.append(cand)
                if len(candidates) >= 8:
                    break
            if len(candidates) >= 8:
                break

        if not candidates:
            return {
                "status": "symbol_mismatch",
                **e.detail,
                "catalogue_searched": seeds,
                "catalogue_hits": 0,
            }

        if len(candidates) == 1:
            # Exactly one candidate — auto-pick.
            auto = candidates[0]
            try:
                resolved, symbol_pins, variant_used = resolve_and_validate_for_variant(
                    prof,
                    chosen_variant=chosen_variant,
                    project_dir=project_dir,
                    design_graph=state.design_graph,
                    lcsc_codes=[auto["lcsc_code"]],
                )
            except SymbolMismatch as e2:
                return {
                    "status": "symbol_mismatch",
                    "catalogue_auto_pick": auto,
                    **e2.detail,
                }
        else:
            # Multiple — defer to the user via ask_user.
            return {
                "status": "needs_lcsc_choice",
                "mpn": mpn,
                "candidates": candidates,
                "hint": (
                    "Multiple LCSC parts match this MPN. Call ask_user "
                    "with options=[<lcsc_code> — <mpn> (<manufacturer>, "
                    "<package>)] for each candidate, then re-call "
                    "parse_datasheet with lcsc_code=<the chosen code>. "
                    "Don't invent codes — only use ones from this list."
                ),
            }

    # Hand off to design_spec — stash the resolved symbol + chosen variant on
    # the conversation so design_spec can run the equation pass and synthesize
    # a spec-driven netlist. parse_datasheet no longer returns a netlist.
    chosen = variant_used or chosen_variant
    state.resolved_symbols[mpn] = {
        "lib_id": resolved.lib_id,
        "symbol_source": resolved.source,
        "symbol_pins": symbol_pins,
        "variant_code": (chosen.package_code if chosen else prof.variant_code),
        "orderable_part": (chosen.orderable_part if chosen else prof.orderable_part),
    }

    return {
        "status": "profile_ready",
        "mpn": mpn,
        "variant": chosen.package_code if chosen else prof.variant_code,
        "orderable_part": (chosen.orderable_part if chosen else prof.orderable_part),
        "lib_id": resolved.lib_id,
        "symbol_source": resolved.source,
        "available_variants": [v.model_dump() for v in prof.available_variants],
        "pin_count": len(prof.pintable_for(chosen)),
        "symbol_match": {
            "pintable_pins": len(prof.pintable_for(chosen)),
            "symbol_pins": len(symbol_pins),
        },
        "hint": (
            "Profile + KiCad symbol resolved. Next: call "
            "design_spec(mpn=…, topology=<buck|boost|buck_boost|ldo>, "
            "vin=…, vout=…, vref=<datasheet FB reference if known>, "
            "fsw_hz=…, iout_a=…, role=…) to compute component values and "
            "show the user a reviewable design spec."
        ),
    }
