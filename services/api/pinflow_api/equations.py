"""Deterministic power-converter design equations.

Greenfield, pure functions, no LLM / no I/O. Given a topology + the user's
rails (Vin, Vout) + the datasheet's feedback reference and switching params,
compute concrete starting-point component values: the feedback divider, the
main inductor, and the input/output capacitors.

These are *first-order* design equations — the canonical textbook forms used
to size a converter's external network. They are not a substitute for a full
loop-stability / thermal analysis; their job is to replace the LLM's guessed
values with reproducible, equation-backed numbers for the parametrized parts.
Every result carries the equation it came from and a tolerance/error note so
the DesignSpec card can show provenance.

E96 (±1%) for the feedback divider; E12 for L / C (the values you can
actually buy and that datasheets quote). `snap_*` always returns a real
series member, never an arbitrary float.
"""

from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, Field

Topology = Literal["buck", "boost", "buck_boost", "ldo"]
TOPOLOGIES: tuple[str, ...] = ("buck", "boost", "buck_boost", "ldo")


# --- E-series ---------------------------------------------------------------

# E96: 96 values/decade, ±1% — the series resistor dividers are specified in.
_E96 = [
    100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130, 133, 137, 140,
    143, 147, 150, 154, 158, 162, 165, 169, 174, 178, 182, 187, 191, 196, 200,
    205, 210, 215, 221, 226, 232, 237, 243, 249, 255, 261, 267, 274, 280, 287,
    294, 301, 309, 316, 324, 332, 340, 348, 357, 365, 374, 383, 392, 402, 412,
    422, 432, 442, 453, 464, 475, 487, 499, 511, 523, 536, 549, 562, 576, 590,
    604, 619, 634, 649, 665, 681, 698, 715, 732, 750, 768, 787, 806, 825, 845,
    866, 887, 909, 931, 953, 976,
]
# E12: 12 values/decade — capacitor / inductor catalogue values.
_E12 = [100, 120, 150, 180, 220, 270, 330, 390, 470, 560, 680, 820]


def _snap(value: float, series: list[int]) -> float:
    """Nearest member of `series` (geometric nearest, across decades)."""
    if value <= 0:
        raise ValueError(f"cannot snap non-positive value {value!r}")
    decade = math.floor(math.log10(value))
    best: Optional[float] = None
    best_err = float("inf")
    for d in (decade - 1, decade, decade + 1):
        scale = 10.0 ** (d - 2)  # series mantissas are 100..976
        for m in series:
            cand = m * scale
            err = abs(math.log(cand / value))
            if err < best_err:
                best_err, best = err, cand
    assert best is not None
    return best


def snap_e96(value: float) -> float:
    """Nearest E96 (±1%) value — for feedback-divider resistors."""
    return _snap(value, _E96)


def snap_e12(value: float) -> float:
    """Nearest E12 value — for inductors / capacitors."""
    return _snap(value, _E12)


_SI = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M"}


def _eng(v: float, unit: str) -> str:
    """`v` in engineering notation: mantissa in [1, 1000) + SI suffix + unit.

    Exponent is derived analytically (not by trial float division, which
    rounds 1e-6/1e-9 to 999.999 and mis-buckets). A post-round bump handles
    a mantissa that rounds up to 1000.
    """
    if v <= 0:
        return f"0{unit}"
    exp3 = 3 * math.floor(math.floor(math.log10(v)) / 3)
    exp3 = max(-12, min(6, exp3))
    mant = v / (10.0 ** exp3)
    if round(mant, 2) >= 1000.0 and exp3 < 6:
        exp3 += 3
        mant /= 1000.0
    return f"{_sig(mant)}{_SI[exp3]}{unit}"


def ohms_str(r: float) -> str:
    """'56.2k', '1M', '470' — ≤3 sig-figs, engineering suffix."""
    return _eng(r, "")


def henries_str(h: float) -> str:
    """'2.2uH', '470nH', '10mH'."""
    return _eng(h, "H")


def farads_str(f: float) -> str:
    """'10uF', '100nF', '4.7pF'."""
    return _eng(f, "F")


def _sig(x: float) -> str:
    """≤3 sig-fig, no trailing-zero noise: 2.2, 56.2, 470, 1.0."""
    if x == 0:
        return "0"
    digits = 2 - math.floor(math.log10(abs(x)))
    r = round(x, max(digits, 0))
    if r == int(r):
        return str(int(r))
    return f"{r:.{max(digits, 0)}f}".rstrip("0").rstrip(".")


# --- Result models ----------------------------------------------------------


class FBDividerResult(BaseModel):
    r_top: float = Field(description="high-side resistor, ohms (snapped E96)")
    r_bottom: float = Field(description="low-side resistor, ohms (snapped E96)")
    r_top_str: str
    r_bottom_str: str
    vout_actual: float = Field(description="Vout the snapped pair actually yields, V")
    vout_error_pct: float
    equation: str
    tolerance: str


class InductorResult(BaseModel):
    henries: float
    value_str: str
    ripple_current_a: float
    equation: str
    tolerance: str


class CapResult(BaseModel):
    cin_farads: float
    cin_str: str
    cout_farads: float
    cout_str: str
    equation: str
    tolerance: str


class SpecComputation(BaseModel):
    """Everything `compute_spec` deterministically derived. Any sub-result is
    None when the topology / inputs don't define it (e.g. LDO has no inductor;
    a fixed-Vout regulator has no FB divider)."""

    topology: str
    duty_cycle: Optional[float] = None
    fb_divider: Optional[FBDividerResult] = None
    inductor: Optional[InductorResult] = None
    caps: Optional[CapResult] = None
    warnings: list[str] = Field(default_factory=list)


# --- Equations --------------------------------------------------------------


def compute_fb_divider(
    *, vref: float, vout: float, r_bottom_hint: float = 100_000.0
) -> FBDividerResult:
    """Adjustable-regulator feedback divider.

    Vout = Vref · (1 + Rtop/Rbot)  ⇒  Rtop = Rbot · (Vout/Vref − 1)

    Rbot is anchored (default 100 kΩ — the usual datasheet starting point;
    high enough to keep divider current low, low enough to swamp FB-pin
    leakage). Both legs snap to E96; Vout is recomputed from the snapped
    pair so the card shows the real ±tolerance error.
    """
    if vref <= 0 or vout <= vref:
        raise ValueError(f"need 0 < vref < vout (vref={vref}, vout={vout})")
    r_bottom = snap_e96(r_bottom_hint)
    r_top = snap_e96(r_bottom * (vout / vref - 1.0))
    vout_actual = vref * (1.0 + r_top / r_bottom)
    err = (vout_actual - vout) / vout * 100.0
    return FBDividerResult(
        r_top=r_top,
        r_bottom=r_bottom,
        r_top_str=ohms_str(r_top),
        r_bottom_str=ohms_str(r_bottom),
        vout_actual=round(vout_actual, 4),
        vout_error_pct=round(err, 3),
        equation=f"Rtop = Rbot·(Vout/Vref − 1); Vout = {vref:g}·(1 + Rtop/Rbot)",
        tolerance=f"E96 ±1% legs; Vout error {err:+.2f}% vs {vout:g}V target",
    )


def _inductor(
    *, vin: float, vout: float, fsw_hz: float, iout_a: float,
    ripple_frac: float, topology: str,
) -> tuple[InductorResult, float]:
    """Returns (InductorResult, duty_cycle). buck/boost/buck-boost forms."""
    di = ripple_frac * iout_a  # peak-to-peak inductor-current ripple
    if topology == "buck":
        duty = vout / vin
        L = (vin - vout) * vout / (vin * fsw_hz * di)
        eq = "L = (Vin−Vout)·Vout / (Vin·fsw·ΔIL)"
    elif topology == "boost":
        duty = 1.0 - vin / vout
        L = vin * duty / (fsw_hz * di)
        eq = "L = Vin·D / (fsw·ΔIL),  D = 1 − Vin/Vout"
    else:  # buck_boost (inverting / 4-switch first-order form)
        duty = vout / (vin + vout)
        L = vin * duty / (fsw_hz * di)
        eq = "L = Vin·D / (fsw·ΔIL),  D = Vout/(Vin+Vout)"
    L_snapped = snap_e12(L)
    return (
        InductorResult(
            henries=L_snapped,
            value_str=henries_str(L_snapped),
            ripple_current_a=round(di, 4),
            equation=eq,
            tolerance=f"ΔIL = {ripple_frac:.0%}·Iout = {di:.3g}A; E12 snap",
        ),
        duty,
    )


def _caps(
    *, vin: float, vout: float, fsw_hz: float, iout_a: float, duty: float,
    ripple_current_a: float, vout_ripple_frac: float, vin_ripple_frac: float,
    topology: str,
) -> CapResult:
    dvout = vout_ripple_frac * vout
    dvin = vin_ripple_frac * vin
    if topology == "buck":
        cout = ripple_current_a / (8.0 * fsw_hz * dvout)
        cin = iout_a * duty * (1.0 - duty) / (fsw_hz * dvin)
        eq = "Cout = ΔIL/(8·fsw·ΔVout);  Cin = Iout·D·(1−D)/(fsw·ΔVin)"
    else:  # boost / buck-boost: Cout supplies Iout during the on-time
        cout = iout_a * duty / (fsw_hz * dvout)
        cin = ripple_current_a / (8.0 * fsw_hz * dvin)
        eq = "Cout = Iout·D/(fsw·ΔVout);  Cin = ΔIL/(8·fsw·ΔVin)"
    cin_s = snap_e12(cin)
    cout_s = snap_e12(cout)
    return CapResult(
        cin_farads=cin_s,
        cin_str=farads_str(cin_s),
        cout_farads=cout_s,
        cout_str=farads_str(cout_s),
        equation=eq,
        tolerance=(
            f"ΔVout={vout_ripple_frac:.0%}·Vout, ΔVin={vin_ripple_frac:.0%}·Vin; "
            "E12 snap (use low-ESR ceramic; derate for DC bias)"
        ),
    )


def compute_spec(
    *,
    topology: str,
    vin: float,
    vout: float,
    vref: Optional[float] = None,
    fsw_hz: Optional[float] = None,
    iout_a: Optional[float] = None,
    ripple_frac: float = 0.30,
    vout_ripple_frac: float = 0.01,
    vin_ripple_frac: float = 0.01,
    r_bottom_hint: float = 100_000.0,
) -> SpecComputation:
    """Per-topology deterministic design pass.

    Degrades gracefully: a missing input zeroes only the sub-results that
    need it (recorded in `warnings`) rather than raising — the caller falls
    back to LLM-sourced values for those parts.
    """
    if topology not in TOPOLOGIES:
        raise ValueError(f"unknown topology {topology!r}; one of {TOPOLOGIES}")
    out = SpecComputation(topology=topology)
    # Append via out.warnings: Pydantic copies a list passed at construction,
    # so a separate local would detach and silently drop these.
    warnings = out.warnings

    # Feedback divider (adjustable regulators only).
    if vref is None:
        warnings.append(
            "no Vref supplied — feedback-divider values left to datasheet/LLM"
        )
    else:
        try:
            out.fb_divider = compute_fb_divider(
                vref=vref, vout=vout, r_bottom_hint=r_bottom_hint
            )
        except ValueError as e:
            warnings.append(f"FB divider skipped: {e}")

    if topology == "ldo":
        # No inductor; Cin/Cout are datasheet stability minima, not switching
        # equations. Leave caps to the datasheet/LLM but flag it.
        warnings.append(
            "LDO: inductor N/A; Cin/Cout are datasheet stability minima "
            "(not computed) — using datasheet-recommended values"
        )
        return out

    if fsw_hz is None or iout_a is None:
        warnings.append(
            "fsw_hz and/or iout_a missing — inductor + caps left to "
            "datasheet/LLM (switching equations need both)"
        )
        return out

    if vout >= vin and topology == "buck":
        warnings.append(
            f"Vout {vout}V ≥ Vin {vin}V is invalid for a buck — check topology"
        )
        return out

    ind, duty = _inductor(
        vin=vin, vout=vout, fsw_hz=fsw_hz, iout_a=iout_a,
        ripple_frac=ripple_frac, topology=topology,
    )
    out.inductor = ind
    out.duty_cycle = round(duty, 4)
    out.caps = _caps(
        vin=vin, vout=vout, fsw_hz=fsw_hz, iout_a=iout_a, duty=duty,
        ripple_current_a=ind.ripple_current_a,
        vout_ripple_frac=vout_ripple_frac, vin_ripple_frac=vin_ripple_frac,
        topology=topology,
    )
    return out
