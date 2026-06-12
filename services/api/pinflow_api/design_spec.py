"""DesignSpec — the reviewable design abstract between profile and netlist.

`build_design_spec` takes a cached `ComponentProfile` + the chosen variant +
the user's rails/topology, runs the deterministic `equations` pass, and
produces a `DesignSpec`: the human-reviewable "what we're going to build"
artifact. Computed parametrized parts (feedback divider, main inductor,
in/out caps) OVERRIDE the datasheet's LLM-extracted recommended values; all
other recommended passives (decoupling, pull-ups, soft-start, …) pass
through unchanged.

The spec then drives `netlist_synth` (via `to_recommended_passives`) so the
netlist encodes the spec'd values rather than the LLM's guesses. Surfaced as
a `DesignSpecCard` + confirm gate (mirrors `plan_block_diagram`).
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from pinflow_api import equations
from pinflow_api.datasheet_parse import RecommendedPassive
from pinflow_api.profile import ComponentProfile
from pinflow_api.datasheet_parse import VariantCandidate


_DESC_PAREN = re.compile(r"[\(\[].*?[\)\]]")
_BARE_NUM = re.compile(r"[0-9]*\.?[0-9]+")


def normalize_value_str(raw: str) -> str:
    """Reduce a value string to its bare electrical magnitude.

    The deterministic `equations` pass already emits clean values
    ('2.2uH', '562k'), but the LLM datasheet extractor stuffs dielectric /
    DCR / package / tolerance annotations into `RecommendedPassive.value`
    despite the schema asking for just the magnitude — e.g.
    '2.2µH (116mΩ DCR, 2016 size)', '4.7µF X5R 0402', '0.267kΩ (E96)'.
    Those land in the KiCad Value field, blow up the rendered text (and the
    packing bbox), and are non-standard EDA practice — Value carries the
    magnitude; dielectric/package belong in Footprint, tolerance in its own
    field (which `SpecComponent` already has). This strips it back to the
    magnitude:

        '2.2µH (116mΩ DCR, 2016 size)' -> '2.2µH'
        '4.7µF X5R 0402'               -> '4.7µF'
        '0.267kΩ (E96)'                -> '0.267kΩ'
        '1N4148 (SOD-123)'             -> '1N4148'
        '562k' / '12MHz' / '100nF'     -> unchanged

    Strip parenthesised/bracketed notes, then keep the leading token (the
    number+unit glob); a bare-numeric head is rejoined to a following unit
    token so a stray '4.7 µF ...' still collapses to '4.7µF'. Empty/odd
    input degrades to the trimmed original — never returns ''.
    """
    s = _DESC_PAREN.sub("", raw or "").strip(" ,;")
    toks = s.split()
    if not toks:
        return (raw or "").strip()
    head = toks[0]
    if len(toks) > 1 and _BARE_NUM.fullmatch(head):
        head += toks[1]
    return head.strip(" ,;") or (raw or "").strip()


class SpecComponent(BaseModel):
    purpose: str = Field(description="e.g. 'feedback divider high-side', 'output cap'")
    refdes_hint: str = Field(description="suggested ref, e.g. 'R1','C2','L1' (LLM-B may re-letter)")
    component: str = Field(description="ref letter: R/C/L/Y/D")
    value: str = Field(description="authoritative value string, e.g. '562k', '3.3uH'")
    chip_pin_number: Optional[str] = None
    equation: Optional[str] = Field(default=None, description="provenance; None for datasheet passthrough")
    tolerance: Optional[str] = None
    source: Literal["computed", "datasheet"]


class RailMapping(BaseModel):
    pin_number: str
    pin_name: str
    rail: str = Field(description="user-facing rail, e.g. '+5V','+3V3','GND', or '' if unmapped")


class DesignSpec(BaseModel):
    mpn: str
    orderable_part: Optional[str] = None
    variant_code: Optional[str] = None
    topology: str
    role: Optional[str] = None
    vin: str
    vout: str
    vin_v: Optional[float] = None
    vout_v: Optional[float] = None
    vref_v: Optional[float] = None
    fsw_hz: Optional[float] = None
    iout_a: Optional[float] = None
    duty_cycle: Optional[float] = None
    components: list[SpecComponent] = Field(default_factory=list)
    rail_map: list[RailMapping] = Field(default_factory=list)
    blurb: str = ""
    warnings: list[str] = Field(default_factory=list)

    def to_recommended_passives(self) -> list[RecommendedPassive]:
        """Adapter so `netlist_synth.synthesize_netlist` is fed spec'd values
        without an API change. Computed values flow in as the authoritative
        recommended values (LLM-B `_SYSTEM` is told to use them verbatim)."""
        return [
            RecommendedPassive(
                purpose=c.purpose,
                component=c.component,
                value=c.value,
                chip_pin_number=c.chip_pin_number,
            )
            for c in self.components
        ]


# Which datasheet recommended-passive entries the deterministic pass replaces.
# (purpose-substring, component-letter) → True means "drop, use computed".
# Broad on purpose: design_spec only runs for regulator topologies, so any
# input/output cap or feedback-divider R the datasheet lists IS the part the
# deterministic pass just sized — override it rather than double-place.
_FB_KEYS = ("feedback", "fb", "divider", "vset", "vsense", "vfb")
_L_KEYS = ("inductor",)
_CIN_KEYS = ("input", "vin", "cin", "bulk")
_COUT_KEYS = ("output", "vout", "cout")


def _is(purpose: str, keys: tuple[str, ...]) -> bool:
    p = purpose.lower()
    return any(k in p for k in keys)


def _find_pin(profile_pins, *names: str) -> Optional[str]:
    """First pin whose name contains any of `names` (case-insensitive)."""
    for p in profile_pins:
        nm = (p.name or "").upper()
        if any(n in nm for n in names):
            return str(p.number)
    return None


def _norm_pn(s: str) -> str:
    """Part-number comparison form: case/separator-insensitive."""
    return re.sub(r"[^A-Z0-9.]", "", s.upper())


def _orderable_for(mpn: str, candidate: Optional[str]) -> Optional[str]:
    """Pick the ordering code, guarding against a wrong-option variant.

    Variants are keyed by *package* pinout, so on a fixed-output regulator
    every voltage option shares one variant entry — and its `orderable_part`
    is whichever option the extractor happened to choose as representative.
    When the user's MPN already encodes the option (e.g. `AP2112K-3.3`), a
    candidate that isn't an extension of it (`AP2112K-1.2TRG1`) would put the
    WRONG part in the schematic's ordering fields. Prefer the user's MPN in
    that case; keep the candidate when it merely adds packaging suffixes
    (`TPS62840` → `TPS62840DLCR`).
    """
    if not candidate:
        return mpn
    if _norm_pn(candidate).startswith(_norm_pn(mpn)):
        return candidate
    return mpn


def build_design_spec(
    *,
    profile: ComponentProfile,
    variant: Optional[VariantCandidate],
    topology: str,
    vin: str,
    vout: str,
    vin_v: Optional[float],
    vout_v: Optional[float],
    vref: Optional[float],
    fsw_hz: Optional[float],
    iout_a: Optional[float],
    role: Optional[str] = None,
) -> DesignSpec:
    """Deterministic pass + datasheet-passthrough merge. Never raises on
    under-specified input — degrades to datasheet values + a warning."""
    pins = profile.pintable_for(variant)
    warnings: list[str] = []

    comp = None
    if vin_v is not None and vout_v is not None:
        comp = equations.compute_spec(
            topology=topology,
            vin=vin_v,
            vout=vout_v,
            vref=vref,
            fsw_hz=fsw_hz,
            iout_a=iout_a,
        )
        warnings.extend(comp.warnings)
    else:
        warnings.append(
            "Vin/Vout not numeric — deterministic equations skipped; "
            "all values from datasheet/LLM"
        )

    components: list[SpecComponent] = []

    have_fb = comp is not None and comp.fb_divider is not None
    have_l = comp is not None and comp.inductor is not None
    have_caps = comp is not None and comp.caps is not None

    # Computed (override) parts first.
    if have_fb:
        fb = comp.fb_divider
        fb_pin = _find_pin(pins, "FB", "VSET", "VSENSE", "VFB")
        components.append(SpecComponent(
            purpose="feedback divider high-side", refdes_hint="R1",
            component="R", value=fb.r_top_str, chip_pin_number=fb_pin,
            equation=fb.equation, tolerance=fb.tolerance, source="computed",
        ))
        components.append(SpecComponent(
            purpose="feedback divider low-side", refdes_hint="R2",
            component="R", value=fb.r_bottom_str, chip_pin_number=fb_pin,
            equation=fb.equation, tolerance=fb.tolerance, source="computed",
        ))
    if have_l:
        ind = comp.inductor
        components.append(SpecComponent(
            purpose="main power inductor", refdes_hint="L1",
            component="L", value=ind.value_str,
            chip_pin_number=_find_pin(pins, "SW", "LX", "SWITCH"),
            equation=ind.equation, tolerance=ind.tolerance, source="computed",
        ))
    if have_caps:
        cp = comp.caps
        components.append(SpecComponent(
            purpose="input capacitor", refdes_hint="C1",
            component="C", value=cp.cin_str,
            chip_pin_number=_find_pin(pins, "VIN", "PVIN", "VDD"),
            equation=cp.equation, tolerance=cp.tolerance, source="computed",
        ))
        components.append(SpecComponent(
            purpose="output capacitor", refdes_hint="C2",
            component="C", value=cp.cout_str,
            chip_pin_number=_find_pin(pins, "VOUT", "VO "),
            equation=cp.equation, tolerance=cp.tolerance, source="computed",
        ))

    # Datasheet passthrough — skip the categories we just computed.
    for rp in profile.recommended_passives:
        c = (rp.component or "").upper()
        if have_fb and c == "R" and _is(rp.purpose, _FB_KEYS):
            continue
        if have_l and c == "L" and _is(rp.purpose, _L_KEYS):
            continue
        if have_caps and c == "C" and _is(rp.purpose, _CIN_KEYS):
            continue
        if have_caps and c == "C" and _is(rp.purpose, _COUT_KEYS):
            continue
        # LLM-extracted; strip dielectric/DCR/package/tolerance noise the
        # extractor adds despite the schema (computed values bypass this —
        # equations.py already emits clean magnitudes).
        components.append(SpecComponent(
            purpose=rp.purpose,
            refdes_hint=f"{rp.component}?",
            component=rp.component,
            value=normalize_value_str(rp.value),
            chip_pin_number=rp.chip_pin_number,
            equation=None,
            tolerance=None,
            source="datasheet",
        ))

    rail_map = _build_rail_map(pins, vin, vout)

    n_c = sum(1 for x in components if x.source == "computed")
    n_d = len(components) - n_c
    blurb = (
        f"{profile.mpn}"
        + (f" ({variant.package_code})" if variant and variant.package_code else "")
        + f" — {topology.replace('_', '-')}"
        + (f", {role}" if role else "")
        + f". {vin} → {vout}. "
        + f"{n_c} component value(s) computed deterministically"
        + (f" (D={comp.duty_cycle})" if comp and comp.duty_cycle else "")
        + f", {n_d} from datasheet."
    )

    return DesignSpec(
        mpn=profile.mpn,
        orderable_part=_orderable_for(
            profile.mpn,
            variant.orderable_part if variant else profile.orderable_part,
        ),
        variant_code=(variant.package_code if variant else profile.variant_code),
        topology=topology,
        role=role,
        vin=vin,
        vout=vout,
        vin_v=vin_v,
        vout_v=vout_v,
        vref_v=vref,
        fsw_hz=fsw_hz,
        iout_a=iout_a,
        duty_cycle=(comp.duty_cycle if comp else None),
        components=components,
        rail_map=rail_map,
        blurb=blurb,
        warnings=warnings,
    )


def _build_rail_map(pins, vin: str, vout: str) -> list[RailMapping]:
    """Best-effort pin→rail mapping for the card's 'design blurb' section."""
    out: list[RailMapping] = []
    for p in pins:
        nm = (p.name or "").upper()
        rail = ""
        if any(k in nm for k in ("GND", "VSS", "AGND", "PGND")):
            rail = "GND"
        elif any(k in nm for k in ("VIN", "PVIN", "VDD", "AVIN")):
            rail = vin
        elif any(k in nm for k in ("VOUT", "VO")):
            rail = vout
        out.append(RailMapping(pin_number=str(p.number), pin_name=p.name, rail=rail))
    return out
