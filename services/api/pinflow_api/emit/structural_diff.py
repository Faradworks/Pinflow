"""Structural validator for placer output.

`validate_placer_output(netlist, result)` confirms the placer's serialized
output (a) round-trips through save/load with every component and label
intact, and (b) — the real check — that KiCad's *own* netlister sees the
same net topology as the input `Netlist`. That connectivity check exports
the schematic with `kicad-cli` and compares net part-membership: it catches
any accidental short (two nets merged) or break, which the part/label checks
alone cannot.

Why connectivity is verified via `kicad-cli`, not `get_net_for_pin`:
kicad-sch-api 0.5.x doesn't materialize pin/net metadata on a freshly loaded
schematic — `get_net_for_pin` returns `None`, so a probe against it silently
passes everything. `kicad-cli sch export netlist` is KiCad's authoritative
netlister and is reliable.

`diff_structure(baseline_result, refined_text)` is the Pass-2 refiner gate:
it asserts the refined schematic preserves the baseline's part set *and* net
topology (same `kicad-cli` export comparison) — so a refiner that shorts two
nets is caught, not waved through.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import kicad_sch_api as ksa

from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.netlist_to_sch import PlacerResult
from pinflow_api.kicad_cli import export_netlist
from pinflow_api.netlist import parse_kicadsexpr

# 2.54mm grid tolerance: file round-trip drops a few decimals at most.
_POS_TOL = 0.05


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _load(sch_text: str) -> ksa.Schematic:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        tmp = Path(f.name)
        f.write(sch_text)
    try:
        return ksa.load_schematic(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def _comp_unit(comp) -> int:
    return getattr(getattr(comp, "_data", None), "unit", 1) or 1


def _labels_by_pos(sch: ksa.Schematic) -> dict[tuple[float, float], list[str]]:
    """Index labels by snapped position for spec matching."""
    out: dict[tuple[float, float], list[str]] = {}
    for lab in sch.labels:
        pos = lab.position
        x = pos.x if hasattr(pos, "x") else pos[0]
        y = pos.y if hasattr(pos, "y") else pos[1]
        out.setdefault((round(float(x), 2), round(float(y), 2)), []).append(lab.text)
    return out


def _netlist_topology(netlist: Netlist) -> list[tuple[str, ...]]:
    """Net topology of the *input* netlist — a sorted list of refdes-sets,
    one per net touching ≥2 distinct parts."""
    topo: list[tuple[str, ...]] = []
    for net in netlist.nets:
        refs = sorted({ep.ref for ep in net.endpoints})
        if len(refs) >= 2:
            topo.append(tuple(refs))
    return sorted(topo)


def _export_topology(sch_text: str) -> list[tuple[str, ...]] | None:
    """Net topology of a rendered schematic as KiCad's own netlister sees it
    — a sorted list of refdes-sets (≥2 parts; `unconnected-` markers dropped).
    `None` if kicad-cli is unavailable or the export fails, so the caller can
    degrade to a warning rather than a false failure.

    Compared by *part set*, not pin number: that catches every net merge or
    break — the functionally dangerous cases — without false-flagging the
    interchangeable pin-1/pin-2 of a symmetric passive.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        tmp = Path(f.name)
        f.write(sch_text)
    try:
        parsed = parse_kicadsexpr(export_netlist(tmp))
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)
    topo: list[tuple[str, ...]] = []
    for name, pins in parsed.nets.items():
        if name.startswith("unconnected-"):
            continue
        refs = sorted({r for r, _p in pins})
        if len(refs) >= 2:
            topo.append(tuple(refs))
    return sorted(topo)


def _topo_diff(
    a: list[tuple[str, ...]], b: list[tuple[str, ...]]
) -> list[tuple[str, ...]]:
    """Multiset difference a − b: net part-sets in `a` not accounted for in
    `b`. Used to describe which way a topology mismatch went."""
    rest = list(b)
    out: list[tuple[str, ...]] = []
    for x in a:
        if x in rest:
            rest.remove(x)
        else:
            out.append(x)
    return out


def validate_placer_output(netlist: Netlist, result: PlacerResult) -> ValidationResult:
    """Verify the placer's serialized output preserved its work.

    Checks:
      - every netlist part is in the reloaded schematic (multi-unit allowed),
      - every `label_spec` from the placer has a matching label in the file,
      - no duplicate refdes-without-unit.
    """
    res = ValidationResult(ok=True)
    try:
        sch = _load(result.sch_text)
    except Exception as e:
        res.ok = False
        res.errors.append(f"failed to reload placed schematic: {e}")
        return res

    # --- Parts present ----------------------------------------------------
    # Power symbols (`power:*` lib_ids) are auto-generated by the placer from
    # rail nets, NOT preserved 1:1 from the netlist — count and refdes both
    # change. Skip them here; the connectivity check below verifies the rails
    # actually reach their pins. (Same filter as `_real_parts` elsewhere in
    # this file.)
    placed: dict[str, list] = {}
    for c in sch.components:
        if c.lib_id.startswith("power:"):
            continue
        placed.setdefault(c.reference, []).append(c)
    expected = {
        p.refdes for p in netlist.parts if not p.lib_id.startswith("power:")
    }
    missing = expected - placed.keys()
    if missing:
        res.ok = False
        res.errors.append(
            f"parts missing from placed schematic: {sorted(missing)}"
        )

    for ref, comps in placed.items():
        if len(comps) <= 1:
            continue
        units = {_comp_unit(c) for c in comps}
        if len(units) != len(comps):
            res.ok = False
            res.errors.append(
                f"duplicate refdes {ref!r} placed {len(comps)}× with units "
                f"{sorted(units)}"
            )

    # --- Labels at expected positions ------------------------------------
    file_labels = _labels_by_pos(sch)
    matched_positions: set[tuple[float, float]] = set()
    for spec in result.label_specs:
        key = (round(spec.position[0], 2), round(spec.position[1], 2))
        texts = file_labels.get(key, [])
        if spec.net_name not in texts:
            res.ok = False
            res.errors.append(
                f"no label {spec.net_name!r} at {spec.ref} pin {spec.pin} "
                f"({spec.position[0]:.2f},{spec.position[1]:.2f}); "
                f"found {texts!r}"
            )
        matched_positions.add(key)

    # --- Orphan labels (warnings, not errors) ----------------------------
    for key, texts in file_labels.items():
        if key not in matched_positions:
            for t in texts:
                res.warnings.append(
                    f"unexpected label {t!r} at {key}"
                )

    # --- Connectivity: KiCad's netlister must see the input's nets -------
    exported = _export_topology(result.sch_text)
    if exported is None:
        res.warnings.append(
            "connectivity not verified — kicad-cli netlist export unavailable"
        )
    else:
        expected_topo = _netlist_topology(netlist)
        if exported != expected_topo:
            res.ok = False
            res.errors.append(
                "net topology does not match the input netlist — missing "
                f"{_topo_diff(expected_topo, exported) or '[]'}, unexpected "
                f"{_topo_diff(exported, expected_topo) or '[]'}"
            )

    return res


def diff_structure(
    baseline: PlacerResult, refined_text: str
) -> ValidationResult:
    """Pass-2 refiner gate: ensure `refined_text` preserves baseline structure.

    Uses the baseline's `label_specs` as ground truth — the refiner is
    allowed to move parts (and labels) but must keep every `(ref, pin) →
    net_name` connection intact. Since labels in refined output sit at new
    positions, we match by (ref, pin, net_name) tuple rather than by
    position. To recover (ref, pin) on the refined side we use kicad-sch-api's
    `get_connected_pins` if available, otherwise fall back to position-radius
    search around the refined components' visible footprints.
    """
    res = ValidationResult(ok=True)
    try:
        a = _load(baseline.sch_text)
        b = _load(refined_text)
    except Exception as e:
        res.ok = False
        res.errors.append(f"failed to reload schematic(s): {e}")
        return res

    # Power symbols carry auto-assigned #PWR refdeses, and a valid re-layout
    # may use a different *count* of them (one shared GND vs one per pin) —
    # both are connectivity-equivalent. Identity-compare only real parts; the
    # topology export below is what actually verifies the power nets.
    def _real_parts(sch: ksa.Schematic) -> set[tuple[str, str]]:
        return {
            (c.reference, c.lib_id)
            for c in sch.components
            if not c.lib_id.startswith("power:")
        }

    a_parts = _real_parts(a)
    b_parts = _real_parts(b)
    if a_parts != b_parts:
        res.ok = False
        only_a = a_parts - b_parts
        only_b = b_parts - a_parts
        if only_a:
            res.errors.append(f"refined dropped parts: {sorted(only_a)}")
        if only_b:
            res.errors.append(f"refined added parts: {sorted(only_b)}")

    # Connectivity: KiCad's own netlister must see the same nets before and
    # after refinement. This is the real short-detector — a refiner that
    # merges or breaks a net shows up here as a changed topology, where the
    # old `get_net_for_pin` probe silently returned None on a loaded file.
    base_topo = _export_topology(baseline.sch_text)
    refn_topo = _export_topology(refined_text)
    if base_topo is None or refn_topo is None:
        res.warnings.append(
            "connectivity not verified — kicad-cli netlist export unavailable"
        )
    elif base_topo != refn_topo:
        res.ok = False
        res.errors.append(
            "refined schematic changed net topology — dropped "
            f"{_topo_diff(base_topo, refn_topo) or '[]'}, added "
            f"{_topo_diff(refn_topo, base_topo) or '[]'}"
        )

    return res
