"""Tool: resolve_parts.

Make every component in the staged schematic orderable. A component is
"resolved" once it carries an `LCSC` code (so a JLCPCB BOM can be cut). This
tool fills the gap left by the generate path (which places bare symbols) and
by any hand-drawn / replicated part, in two modes:

- **MPN→LCSC** — the component already has an MPN (a symbol property, the
  netlist-carried MPN, or a Value that *is* a part number like `1N4148` /
  `BSS138` / `TPS62840DLCR`): look the MPN up in the catalogue and fill the
  matching LCSC code.
- **keyword** — no MPN signal (a generic passive: `10uF`, `562k`, `2.2uH`):
  keyword-search the catalogue with the design_spec-baked query (rail-aware,
  derated) — or a value+footprint+type fallback — and pick the best in-stock
  candidate.

A part that already has both MPN and LCSC is left alone. Writeback is a single
batched mutation to the *staged* schematic; the model then runs the existing
ask_user(Confirm/Discard) → commit_edit gate.
"""

from __future__ import annotations

import re
from typing import Optional

from pinflow_api import parts as parts_facade
from pinflow_api import staging
from pinflow_api.agent.schematic_sync import load_active_schematic
from pinflow_api.agent.tools._subcircuit_common import load_target_schematic
from pinflow_api.agent.tools.resolve_mpn import _candidates_from_value
from pinflow_api.sch_properties import set_properties

SCHEMA = {
    "name": "resolve_parts",
    "description": (
        "Resolve every under-specified component in the staged schematic to a "
        "real LCSC catalogue part — fills MPN / LCSC / Manufacturer / "
        "Description so the design is orderable and a BOM can be cut. Covers "
        "passives AND diodes/transistors/ICs. Components that already have an "
        "LCSC code are skipped. Parts with an MPN (or an MPN-like Value) are "
        "matched by MPN; generic passives are keyword-searched using the "
        "rail-aware query design_spec baked in (or a value+footprint "
        "fallback). Auto-picks the best in-stock candidate per part. Call "
        "this after add_subcircuit_from_netlist has staged the block; then "
        "show the result and run ask_user(question='Apply these part "
        "choices?', options=['Confirm','Discard']) → commit_edit / "
        "discard_edit. Pass `refdeses` to resolve only specific parts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "refdeses": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional. Resolve only these reference designators. "
                    "Omit to resolve every unresolved component."
                ),
            },
        },
        "required": [],
    },
}

# Property keys we read/write. `MPN` matches sch_properties.get_mpn's lookup;
# `LCSC` is the de-facto JLCPCB / KiCad-plugin convention.
_MPN_KEYS = ("MPN", "Manufacturer Part Number", "manufacturer_part_number")
_LCSC_KEYS = ("LCSC", "LCSC Part #", "JLCPCB")

# A Value that is an electrical magnitude, not a part number — '10uF', '562k',
# '2.2µH', '0R', '4.7 nF', '100', '1%'. Such a Value must NOT seed an MPN
# lookup (it would route a plain cap into MPN mode and miss).
_PASSIVE_VALUE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:p|n|u|µ|μ|m|k|meg|M|G)?\s*(?:F|H|R|Ω|ohm|ohms|W|%|V|A)?\s*$",
    re.IGNORECASE,
)

_TYPE_WORD = {
    "C": "capacitor",
    "R": "resistor",
    "L": "inductor",
    "D": "diode",
    "Q": "transistor",
    "Y": "crystal",
    "FB": "ferrite bead",
}

# Package tokens worth using as a catalogue package filter, pulled out of a
# KiCad footprint string like 'Capacitor_SMD:C_0603_1608Metric'. `\b` is the
# wrong boundary here — in 'C_0603_1608Metric' the imperial code is flanked by
# underscores (word chars), so use explicit non-alnum lookarounds instead.
_PKG_RX = re.compile(
    r"(?<![A-Za-z0-9])("
    r"0201|0402|0603|0805|1206|1210|1812|2010|2512"
    r"|SOT-?\d+(?:-\d+)?|SOIC-?\d+|QFN-?\d+|TSSOP-?\d+|MSOP-?\d+"
    r"|S?SON-?\d+|DFN-?\d+|TO-?\d+|SOD-?\d+|DPAK|D2PAK"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_VOLT_RX = re.compile(r"(\d+(?:\.\d+)?)\s*V\b", re.IGNORECASE)

# SI prefixes → multiplier. 'u'/'µ'/'μ' micro; 'R'/'' base for resistors.
_PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "meg": 1e6, "M": 1e6, "g": 1e9,
}
# Unit class a Device:<letter> wants, and the regex that pulls a magnitude of
# that class out of a value string or a catalogue description.
_CLASS_UNIT = {"C": "F", "R": "OHM", "L": "H"}
_MAG_RX = {
    "F": re.compile(r"(\d+(?:\.\d+)?)\s*(p|n|u|µ|μ|m)?\s*F\b", re.IGNORECASE),
    "H": re.compile(r"(\d+(?:\.\d+)?)\s*(p|n|u|µ|μ|m)?\s*H\b", re.IGNORECASE),
    "OHM": re.compile(
        r"(\d+(?:\.\d+)?)\s*(k|K|M|m|meg)?\s*(?:Ω|ohm|ohms|R)\b",
        re.IGNORECASE,
    ),
}


def _to_base(num: float, prefix: Optional[str]) -> float:
    return num * _PREFIX.get((prefix or ""), _PREFIX.get((prefix or "").lower(), 1.0))


def _requested_magnitude(value: str, klass: str) -> Optional[float]:
    """Parse the symbol's Value ('10uF','562k','2.2µH','0R') to a base-unit
    float for the expected class. Bare numbers are read as that class's unit
    (a resistor '100' → 100Ω, 'k'/'M' suffixes handled)."""
    unit = _CLASS_UNIT.get(klass)
    if not unit:
        return None
    m = _MAG_RX[unit].search(value)
    if m:
        return _to_base(float(m.group(1)), m.group(2) if m.lastindex and m.lastindex >= 2 else "")
    # No explicit unit (common for R: '100k', '4.7', '0R' handled above).
    m2 = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(p|n|u|µ|μ|m|k|K|M|meg|g)?\s*", value or "")
    if m2 and klass == "R":
        return _to_base(float(m2.group(1)), m2.group(2) or "")
    return None


def _value_ok(value: str, lib_id: str, cand: dict) -> bool:
    """True if the candidate is plausibly the SAME component class + value as
    the symbol. Guards against the keyword ranker handing back a 22Ω resistor
    for a 22µF cap — for passives a wrong-type/wrong-value pick is worse than
    reporting no match."""
    klass = _letter(lib_id)
    if klass not in _CLASS_UNIT:
        return True  # not an R/C/L — no magnitude guard (diodes, ICs, …)
    want = _requested_magnitude(value, klass)
    if want is None:
        return True  # can't parse the request — don't over-reject
    desc = cand.get("description", "") or ""
    unit = _CLASS_UNIT[klass]
    best = None
    for m in _MAG_RX[unit].finditer(desc):
        got = _to_base(
            float(m.group(1)),
            m.group(2) if (m.lastindex and m.lastindex >= 2) else "",
        )
        if got > 0 and (best is None or abs(got - want) < abs(best - want)):
            best = got
    if best is None:
        return False  # no magnitude of the right unit class → wrong type
    return 0.95 <= (best / want) <= 1.05 if want else best == 0


def _get_prop(comp, name: str) -> str:
    """Read a user property off a kicad_sch_api component (handles the
    {name,value,…} wrapper, same as sch_properties.parse_properties)."""
    props = getattr(comp, "properties", None) or {}
    v = props.get(name)
    if v is None:
        return ""
    val = v["value"] if isinstance(v, dict) and "value" in v else v
    return str(val) if val else ""


def _first_prop(comp, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = _get_prop(comp, k)
        if v:
            return v
    return ""


def _letter(lib_id: str) -> str:
    if not lib_id or ":" not in lib_id:
        return ""
    sym = lib_id.split(":", 1)[1]
    if sym.upper().startswith("FB"):
        return "FB"
    return sym[:1].upper()


def _package_hint(footprint: str) -> Optional[str]:
    m = _PKG_RX.search(footprint or "")
    return m.group(1) if m else None


def _rated_voltage(description: str) -> Optional[float]:
    """Largest 'NNV' token in a catalogue description — best-effort rating."""
    vals = [float(x) for x in _VOLT_RX.findall(description or "")]
    return max(vals) if vals else None


def _derate(cands: list[dict], min_voltage: Optional[float]) -> list[dict]:
    """Drop candidates rated below `min_voltage`. Never empties the list: if
    the filter would remove everything (or no rating is parseable), the
    original ranking is returned unchanged."""
    if not min_voltage or not cands:
        return cands
    kept = [
        c for c in cands
        if (_rated_voltage(c.get("description", "")) or 0.0) >= min_voltage
    ]
    return kept or cands


def _seeds(existing_mpn: str, np_mpn: str, value: str, lib_id: str) -> list[str]:
    """Ordered, de-duped MPN seeds for the MPN→LCSC path."""
    out: list[str] = []
    letter = _letter(lib_id)
    for s in (existing_mpn, np_mpn):
        if s and s not in out:
            out.append(s)
    # Value-as-MPN only when the Value isn't an electrical magnitude and the
    # symbol isn't a generic R/C/L (those are keyword-resolved).
    if value and not _PASSIVE_VALUE.match(value) and letter not in ("R", "C", "L"):
        for c in _candidates_from_value(value):
            if c not in out:
                out.append(c)
    return out


def _fallback_query(value: str, footprint: str, lib_id: str) -> str:
    word = _TYPE_WORD.get(_letter(lib_id), "")
    pkg = _package_hint(footprint) or ""
    return " ".join(b for b in (value, pkg, word) if b).strip()


def run(state, refdeses: Optional[list] = None, **_inputs) -> dict:
    if state.active_sch_path is None:
        return {
            "status": "no_active_schematic",
            "hint": "Call read_active_schematic before resolve_parts.",
        }
    if not parts_facade.is_available():
        return {
            "status": "unavailable",
            "hint": (
                "Parts catalogue is unavailable, so components can't be "
                "auto-resolved to LCSC codes. Tell the user the design is "
                "still valid — it just won't carry orderable LCSC codes — and "
                "they can add MPNs/LCSC codes manually. Don't invent codes."
            ),
        }

    cloud = parts_facade.is_cloud_available()

    try:
        sch, source_text = load_target_schematic(state.active_sch_path)
    except Exception as e:
        return {"status": "load_failed", "error": f"{type(e).__name__}: {e}"}

    only = {r for r in refdeses} if refdeses else None

    sn = state.staged_netlists.get(str(state.active_sch_path)) or {}
    np_by_ref = {
        p.get("refdes"): p for p in sn.get("parts", []) if p.get("refdes")
    }

    resolved: list[dict] = []
    unmatched: list[str] = []
    updates: dict[str, dict[str, str]] = {}

    for comp in sch.components:
        ref = getattr(comp, "reference", None)
        lib_id = getattr(comp, "lib_id", "") or ""
        if not ref or lib_id.startswith("power:"):
            continue
        if only is not None and ref not in only:
            continue

        existing_mpn = _first_prop(comp, _MPN_KEYS)
        existing_lcsc = _first_prop(comp, _LCSC_KEYS)
        if existing_lcsc:
            continue  # already orderable

        value = (getattr(comp, "value", "") or "").strip()
        footprint = getattr(comp, "footprint", "") or ""
        np = np_by_ref.get(ref) or {}
        np_mpn = (np.get("mpn") or "").strip()

        seeds = _seeds(existing_mpn, np_mpn, value, lib_id)
        pick: Optional[dict] = None

        if seeds:
            mode = "mpn"
            detail = seeds[0]
            for s in seeds:
                cands = parts_facade.search_by_mpn(s, limit=8)
                if cands:
                    pick = cands[0]
                    detail = s
                    break
        else:
            mode = "keyword"
            query = (np.get("search_query") or "").strip() or _fallback_query(
                value, footprint, lib_id
            )
            detail = query
            if not cloud:
                # Keyword search needs the parts catalogue; record an honest miss.
                unmatched.append(ref)
                resolved.append({
                    "refdes": ref, "value": value, "mode": mode,
                    "query": query, "status": "no_match",
                    "note": "keyword search needs the parts catalogue (unavailable)",
                })
                continue
            cands = parts_facade.search_keyword(
                query,
                limit=25,
                require_stock=True,
                package=_package_hint(footprint),
            )
            cands = _derate(cands, np.get("min_voltage"))
            # Reject candidates that aren't the same component class + value
            # (the keyword ranker is loose for passives). Honest no_match
            # beats a 22Ω resistor stamped onto a 22µF cap.
            typed = [c for c in cands if _value_ok(value, lib_id, c)]
            pick = typed[0] if typed else None

        detail_key = "query" if mode == "keyword" else "mpn_seed"

        if pick is None:
            unmatched.append(ref)
            resolved.append({
                "refdes": ref, "value": value, "mode": mode,
                detail_key: detail, "status": "no_match",
            })
            continue

        updates[ref] = {
            "MPN": pick["mpn"],
            "LCSC": pick["lcsc_code"],
            "Manufacturer": pick.get("manufacturer", ""),
            "Description": pick.get("description", ""),
        }
        resolved.append({
            "refdes": ref,
            "value": value,
            "mode": mode,
            detail_key: detail,
            "lcsc_code": pick["lcsc_code"],
            "mpn": pick["mpn"],
            "manufacturer": pick.get("manufacturer", ""),
            "description": pick.get("description", ""),
            "status": "picked",
        })

    if not resolved:
        return {
            "status": "nothing_to_resolve",
            "hint": (
                "Every component already has an LCSC code, or none matched "
                "the refdeses filter. Nothing was staged."
            ),
        }

    if updates:
        try:
            new_text = set_properties(source_text, updates)
        except Exception as e:
            return {
                "status": "writeback_failed",
                "error": f"{type(e).__name__}: {e}",
            }
        staging.update(state.active_sch_path, new_text)
        load_active_schematic(state)

    return {
        "status": "ok",
        "resolved": resolved,
        "picked_count": len(updates),
        "unmatched": unmatched,
        "diff_available": bool(updates),
        "hint": (
            "Show the user this resolution table, then call "
            "ask_user(question='Apply these part choices?', "
            "options=['Confirm','Discard']). On Confirm call commit_edit; on "
            "Discard call discard_edit. "
            + (
                f"{len(unmatched)} part(s) had no catalogue match "
                f"({', '.join(unmatched)}) — tell the user they need a manual "
                "MPN or a different search; don't invent codes."
                if unmatched else
                "All parts resolved."
            )
        ),
    }
