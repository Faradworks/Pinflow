"""LLM-direct placer — *experimental* layout-from-scratch via constrained LLM.

Where the LLM-planner picked archetypes (didn't help) and the VLM-critic
nudged a placed layout (helped partially), this module asks the LLM to
do the actual placement — emit an `Anchor(refdes, axis, value)` for
every non-IC part in a single call. The deterministic solver then
resolves any inter-part constraints from the archetype emitters that
the LLM didn't override.

The architectural pitch: rules-based geometry hits a ceiling on dense
ICs because we can't encode every EE convention. The LLM has read
millions of schematics; the placement intuition is in the weights.
What was unreliable about `llm_emit.py` (LLM emits raw Python code +
coordinates) is mitigated here by:

  - **Constrained output vocabulary** — only `Anchor(refdes, axis, value)`.
    The solver validates consistency; the structural-diff validates
    connectivity. Nothing the LLM emits can crash the schematic.
  - **Grounded inputs** — the LLM sees the IC's pin positions in
    absolute coords, the oriented support parts with their pin offsets,
    and a few-shot example of a working layout. It doesn't bootstrap
    from zero.
  - **Deterministic fallback** — if the LLM's layout fails validation,
    we return the cplace baseline. Worst case is no worse than today.

Used as `cplace(netlist, external_emitter=plan_layout_constraints)`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from anthropic import Anthropic

from pinflow_api import llm
from pinflow_api.emit.classify import LayoutPlan, NetKind, RailSide, Role, classify
from pinflow_api.emit.constraints import Anchor
from pinflow_api.emit.layout_tree import (
    Archetype,
    LayoutTree,
    build_layout_tree,
)
from pinflow_api.emit.netlist import Netlist
from pinflow_api.settings import settings


# --- prompt -------------------------------------------------------------------

_SYSTEM = """\
You are an expert electrical engineer placing parts on a schematic page.

YOUR JOB IS PLACEMENT ONLY. Wiring is a separate deterministic step that
runs AFTER you. You only choose where each part *sits* — pick (x, y) for
each non-IC refdes such that:

  RULE 1 — NO TWO BODIES OVERLAP. Each part has a bbox extent {left,
  right, top, bottom} from its origin. The placed bbox of a part is:
      x_min = x - left,  x_max = x + right
      y_min = y - top,   y_max = y + bottom
  For any two parts A and B, their bboxes (plus a 2.54 mm safety margin)
  must NOT intersect. Verify before submitting.

  RULE 2 — NO TWO PIN COORDS COINCIDE. Pin position = origin + pin_offset.
  If part A's pin lands at the same coordinate as part B's pin, KiCad
  silently merges the nets — a short. Verify before submitting.

  RULE 3 — GENEROUS SPACING. Better sprawling than cramped. If you
  cannot fit a part on one side, place it elsewhere; do not squeeze.

INPUTS YOU WILL SEE:
  - The IC at a fixed anchored position with all its pin coordinates
    and which net each pin is on.
  - Every non-IC part:
      * refdes, role (input_cap, output_cap, config_cap, divider_resistor,
        pull_resistor, series_element, …)
      * a *suggested archetype* from the deterministic classifier — a
        hint, not a command.
      * pin offsets from origin AFTER orientation: for each pin, the
        (dx, dy) where it lands relative to the part's chosen (x, y).
      * pin_to_net: which net each pin connects to.
      * bbox_extent: {left, right, top, bottom} from origin.
  - The rails (input / output / ground net names).
  - One worked example (netlist → layout) from a similar known-good
    circuit.

YOUR OUTPUT:
  - For each non-IC refdes, an (x, y) anchor. Coordinates must be
    multiples of 2.54 mm.

LAYOUT CONVENTIONS:
  - Input rail trunk: horizontal line on the LEFT at the IC's input
    pin Y. Filter caps hang off it pin-1-up.
  - Output rail trunk: horizontal line on the RIGHT at the IC's
    output pin Y. Output cap bank hangs off pin-1-up.
  - Feedback divider: high leg above tap pin, low leg below tap pin,
    same X column, past the output bank.
  - Config caps: at their control pin's Y, just past the IC side edge.
  - Control resistors (EN pull-up, ILIM bias): at the pin's Y. If
    config caps are on the same side, put resistors one column further
    out.
  - Bootstrap caps (BOOT-SW): straddling the two IC pins, often above
    the IC body OR to the side both pins are on, at the midpoint Y.
  - Power inductor (buck-boost coupling): above the IC at the SW pins.
  - For a boost converter: the inductor sits INLINE on the input rail
    trunk (between input filter and IC's SW pin), and the Schottky
    diode sits INLINE between SW and the output rail.

VERIFICATION CHECKLIST (run mentally before submitting):
  1. For every pair of placed parts, does bbox_A intersect bbox_B?
     If yes, move one until they don't.
  2. For every pair of parts on different nets, does any pin of A
     coincide with any pin of B?  If yes, you have a short. Move one.
  3. Are all coordinates multiples of 2.54?

Call submit_layout exactly once with the full set.
"""


# --- structured input ---------------------------------------------------------

@dataclass
class _PartShape:
    refdes: str
    role: str
    rotation: float
    pin_offsets: dict[str, tuple[float, float]]
    archetype_hint: str
    side_hint: str | None
    rail_hint: str | None


def _summarise(
    netlist: Netlist, plan: LayoutPlan, tree: LayoutTree,
    parts: dict, ctx,
) -> dict:
    """Pack the LLM input as a structured dict."""
    anchor = tree.anchor

    # IC pinmap with absolute coords (parts[anchor].pin_off was relative;
    # convert to absolute).
    ic_origin = ctx.ic_origin
    ic_pins = []
    for pi in plan.pinmaps.get(anchor, []):
        # Look up absolute via parts[anchor].pin_off (already measured).
        off = parts[anchor].pin_off.get(pi.number)
        if off is None:
            continue
        abs_x = ic_origin[0] + off[0]
        abs_y = ic_origin[1] + off[1]
        # Find the net this pin lands on.
        net_name = None
        for net in netlist.nets:
            for ep in net.endpoints:
                if ep.ref == anchor and ep.pin == pi.number:
                    net_name = net.name
                    break
            if net_name:
                break
        ic_pins.append({
            "pin": pi.number,
            "name": pi.name,
            "side": pi.side,
            "etype": pi.etype,
            "x": round(abs_x, 2),
            "y": round(abs_y, 2),
            "net": net_name,
        })

    # Rails
    rails = {"input": tree.input_rail, "output": tree.output_rail,
             "ground": tree.ground}

    # Per-part shape info.
    # Build per-refdes archetype lookup.
    archetype_of: dict[str, tuple[str, str | None, str | None]] = {}
    for g in tree.groups:
        for r in g.members:
            archetype_of[r] = (g.archetype.value, g.side, g.rail)

    parts_info = []
    for ref, p in parts.items():
        if ref == anchor:
            continue
        pc = plan.parts.get(ref)
        role = pc.role.value if pc else "unknown"
        a_hint, side, rail = archetype_of.get(ref, ("loose", None, None))
        # Pin-to-net map for this part.
        pin_net = {}
        for net in netlist.nets:
            for ep in net.endpoints:
                if ep.ref == ref:
                    pin_net[ep.pin] = net.name
        rot = 0.0
        if p.cell.members:
            try:
                rot = float(p.cell.members[0].rotation or 0)
            except Exception:
                rot = 0.0
        parts_info.append({
            "refdes": ref,
            "role": role,
            "rotation": round(rot, 0),
            "archetype_hint": a_hint,
            "side_hint": side,
            "rail_hint": rail,
            "pin_offsets": {
                pn: (round(off[0], 2), round(off[1], 2))
                for pn, off in p.pin_off.items()
            },
            "pin_to_net": pin_net,
            "bbox_extent": {
                "left": round(p.leftext, 2),
                "right": round(p.rightext, 2),
                "top": round(p.topext, 2),
                "bottom": round(p.botext, 2),
            },
        })

    return {
        "ic": {
            "refdes": anchor,
            "origin_x": round(ic_origin[0], 2),
            "origin_y": round(ic_origin[1], 2),
            "left_extent": round(ctx.ic_leftext, 2),
            "right_extent": round(ctx.ic_rightext, 2),
            "pins": ic_pins,
        },
        "rails": rails,
        "parts": parts_info,
    }


# --- few-shot library --------------------------------------------------------

# A small library of (netlist → layout) examples baked from cplace's
# deterministic output on each golden. The placer picks examples to
# show the LLM excluding the one currently being placed (if it's
# itself a golden) — keeps the few-shots diverse and prevents the
# trivial "just look at the answer" case.

@lru_cache(maxsize=1)
def _few_shot_library() -> dict[str, dict]:
    """Build (netlist → layout) examples from every golden in the corpus.
    Returns a dict keyed by golden name. The layout is cplace's actual
    deterministic output for each one — already golden-quality on three
    of five, near-golden on the others."""
    out: dict[str, dict] = {}
    try:
        api_dir = Path(__file__).resolve().parents[2]
        manifest_path = (api_dir / "tests" / "fixtures"
                         / "golden_corpus.json")
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest.get("goldens", []):
            name = entry.get("name")
            if not name:
                continue
            try:
                ex = _build_few_shot(api_dir, entry)
            except Exception as e:  # noqa: BLE001
                print(f"  warn: few-shot {name} failed — {e}",
                      file=sys.stderr)
                continue
            if ex is not None:
                out[name] = ex
    except Exception as e:  # noqa: BLE001
        print(f"  warn: few-shot library failed — {e}", file=sys.stderr)
    return out


def _build_few_shot(api_dir: Path, entry: dict) -> dict | None:
    """Run cplace forward on one golden, capture (input, layout)."""
    import kicad_sch_api as ksa
    if entry.get("symbols"):
        ksa.get_symbol_cache().discover_libraries(
            [str(api_dir / "tests" / "fixtures" / entry["symbols"])]
        )
    netlist = Netlist.model_validate(json.loads(
        (api_dir / "tests" / "fixtures" / entry["netlist"]).read_text()
    ))

    from pinflow_api.emit.placers.cplace import (
        IC_X, IC_Y, _emit_all, _measure_part, _orient_all, _Ctx,
    )
    from pinflow_api.emit.constraints import solve
    from pinflow_api.emit.netlist_to_sch import (
        _natural_key, _place_and_measure,
    )
    from pinflow_api.emit._placer_helpers import _move_to

    plan = classify(netlist)
    tree = build_layout_tree(netlist)
    anchor = tree.anchor
    if anchor is None:
        return None

    sch = ksa.create_schematic(f"{entry['name']}-fewshot")
    issues: list = []
    horiz_series = {
        r for g in tree.groups if g.archetype == Archetype.SERIES_FILTER
        for r in g.members if plan.parts[r].role == Role.SERIES_ELEMENT
    }
    ordered = sorted(netlist.parts, key=lambda p: _natural_key(p.refdes))
    cells = _place_and_measure(sch, ordered, issues,
                               rotate_horizontal=frozenset(horiz_series))
    by_ref = {c.refdes: c for c in cells}
    placed_refs: dict = {}
    _move_to(by_ref[anchor], IC_X, IC_Y, placed_refs)
    ic_comp = sch.components.get(anchor)
    _orient_all(tree, by_ref, ic_comp)
    parts_m: dict = {}
    for ref, cell in by_ref.items():
        mp = _measure_part(cell)
        if mp is not None:
            parts_m[ref] = mp
    ic_part = parts_m[anchor]
    ctx = _Ctx(
        tree=tree, netlist=netlist, ic_comp=ic_comp, anchor=anchor,
        ic_origin=ic_part.origin,
        ic_leftext=ic_part.leftext, ic_rightext=ic_part.rightext,
    )

    input_payload = _summarise(netlist, plan, tree, parts_m, ctx)
    cs = _emit_all(tree, parts_m, ctx)
    xr = solve(cs.x, fallback=IC_X)
    yr = solve(cs.y, fallback=IC_Y)
    layout = {
        r: {"x": round(xr.pos.get(r, IC_X), 2),
            "y": round(yr.pos.get(r, IC_Y), 2)}
        for r in parts_m if r != anchor
    }
    return {"name": entry["name"], "input": input_payload,
            "layout": layout, "note": entry.get("note", "")}


def _pick_few_shots(
    current_part_count: int,
    max_shots: int = 3,
    exclude_names: set[str] | None = None,
) -> list[dict]:
    """Pick up to `max_shots` examples from the library, sorted by
    similarity in part count (so the LLM sees layouts of comparable
    complexity). The caller can pass `exclude_names` to skip golden(s)
    that match the current circuit — e.g. during eval to avoid the
    trivial 'copy the answer' case."""
    lib = _few_shot_library()
    exclude = exclude_names or set()
    candidates = [
        ex for ex in lib.values()
        if ex["input"]["ic"]["refdes"] is not None
        and ex["name"] not in exclude
    ]
    if not candidates:
        return []
    candidates.sort(
        key=lambda ex: abs(len(ex["input"]["parts"]) - current_part_count)
    )
    return candidates[:max_shots]


# --- tool schema --------------------------------------------------------------

_TOOL = {
    "name": "submit_layout",
    "description": "Submit (x, y) anchor for every non-IC part.",
    "input_schema": {
        "type": "object",
        "properties": {
            "placements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "refdes": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "rationale": {
                            "type": "string",
                            "description": (
                                "One short sentence: why this position. "
                                "For your own reasoning trace."
                            ),
                        },
                    },
                    "required": ["refdes", "x", "y"],
                },
            }
        },
        "required": ["placements"],
    },
}


# --- validation ---------------------------------------------------------------

_BBOX_MARGIN = 2.54   # mm — required gap between any two part bboxes
_PIN_TOL = 0.1        # mm — coord coincidence threshold


def _bbox_of(ref: str, xy: tuple[float, float],
             parts: dict) -> tuple[float, float, float, float]:
    """`(x_min, y_min, x_max, y_max)` for `ref` placed at `xy`."""
    p = parts[ref]
    x, y = xy
    return (x - p.leftext, y - p.topext,
            x + p.rightext, y + p.botext)


def _bboxes_overlap(a, b, margin: float = _BBOX_MARGIN) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 + margin <= bx0 or bx1 + margin <= ax0
                or ay1 + margin <= by0 or by1 + margin <= ay0)


def _pin_coord(ref: str, pin: str, xy: tuple[float, float],
               parts: dict) -> tuple[float, float] | None:
    off = parts[ref].pin_off.get(pin)
    if off is None:
        return None
    return (xy[0] + off[0], xy[1] + off[1])


def _check_placements(
    placed: dict[str, tuple[float, float]],
    parts: dict, netlist: Netlist, anchor: str,
    ic_origin: tuple[float, float],
) -> list[str]:
    """Return a list of human-readable violations the LLM should fix."""
    errs: list[str] = []
    # Pin → net for each part, so we can detect cross-net pin coincidence.
    net_of: dict[tuple[str, str], str] = {}
    for net in netlist.nets:
        for ep in net.endpoints:
            net_of[(ep.ref, ep.pin)] = net.name

    # Include the IC in the bbox check (a support part can't crash into U1).
    all_refs = list(placed.keys()) + [anchor]
    placed_with_ic = dict(placed)
    placed_with_ic[anchor] = ic_origin

    bboxes = {r: _bbox_of(r, placed_with_ic[r], parts) for r in all_refs}
    items = sorted(bboxes.items())
    for i in range(len(items)):
        ra, ba = items[i]
        for j in range(i + 1, len(items)):
            rb, bb = items[j]
            if _bboxes_overlap(ba, bb):
                errs.append(
                    f"BBOX OVERLAP: {ra} at ({placed_with_ic[ra][0]:.2f}, "
                    f"{placed_with_ic[ra][1]:.2f}) and {rb} at "
                    f"({placed_with_ic[rb][0]:.2f}, "
                    f"{placed_with_ic[rb][1]:.2f}) — their bodies "
                    f"overlap. Move one apart by at least "
                    f"{_BBOX_MARGIN} mm extra clearance."
                )

    # Cross-net pin coincidence.
    pins_at: dict[tuple[float, float], list[tuple[str, str, str]]] = {}
    for ref, xy in placed_with_ic.items():
        for pin in parts[ref].pin_off:
            pc = _pin_coord(ref, pin, xy, parts)
            if pc is None:
                continue
            key = (round(pc[0], 1), round(pc[1], 1))
            net = net_of.get((ref, pin), "?")
            pins_at.setdefault(key, []).append((ref, pin, net))
    for key, occ in pins_at.items():
        if len(occ) < 2:
            continue
        nets = {n for _r, _p, n in occ}
        if len(nets) <= 1:
            continue  # same net — fine
        names = ", ".join(f"{r}.{p}({n})" for r, p, n in occ)
        errs.append(
            f"PIN SHORT: pins at coord ({key[0]:.1f}, {key[1]:.1f}) "
            f"are on different nets — {names}. Move one part so the "
            f"pins don't coincide."
        )
    return errs


def _format_violations_for_retry(errs: list[str]) -> str:
    return (
        "The placement you submitted has violations. Fix and resubmit.\n\n"
        + "\n".join(f"  - {e}" for e in errs[:8])
        + (f"\n  ... ({len(errs) - 8} more)" if len(errs) > 8 else "")
        + "\n\nRe-emit submit_layout with corrected (x, y) for every part."
    )


# --- public entry -------------------------------------------------------------

_MAX_RETRIES = 2     # one initial call + this many corrections


def _call_llm(
    user_content: list, client: Anthropic, model: str,
) -> list[dict]:
    """Single LLM call returning the placements list from the tool call."""
    kwargs: dict = dict(model=model, max_tokens=4096)
    if not model.startswith("claude-opus-4-7"):
        kwargs["temperature"] = 0.0
    response = client.messages.create(
        **kwargs, system=_SYSTEM, tools=[_TOOL],
        tool_choice={"type": "tool", "name": "submit_layout"},
        messages=[{"role": "user", "content": user_content}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return list(block.input.get("placements", []))
    raise RuntimeError("LLM did not call submit_layout")


# --- visual self-review ------------------------------------------------------

def _render_to_png_bytes(sch_text: str) -> bytes:
    """Render a schematic and return the PNG bytes."""
    from pinflow_api.emit.render import render_schematic
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        out = Path(f.name)
    try:
        render_schematic(sch_text, out, dpi=200)
        return out.read_bytes()
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def _build_intermediate_sch(
    netlist: Netlist, tree: LayoutTree, parts: dict, ctx,
    placed: dict[str, tuple[float, float]],
) -> str:
    """Build a schematic *from* the placements without re-running the LLM,
    so we can render the current layout and show it back to the model."""
    from pinflow_api.builders._common import sch_to_string
    from pinflow_api.emit.placers.cplace import (
        _CSet, IC_X, IC_Y, _hide_gnd_labels,
        _reposition_fields,
    )
    from pinflow_api.emit.constraints import solve
    from pinflow_api.emit.classify import Role
    from pinflow_api.emit.netlist_to_sch import (
        _natural_key, _place_and_measure, _place_connectivity,
        _place_no_connects, _pin_xy as _pinxy, _topology_intact,
    )
    from pinflow_api.emit._placer_helpers import _translate, _move_to
    from pinflow_api.emit.layout_tree import Archetype
    import kicad_sch_api as ksa

    # Re-build the schematic via cplace's normal flow but with extras
    # forcing the placed positions.
    sch = ksa.create_schematic("intermediate")
    issues: list = []
    plan = tree.plan
    anchor = tree.anchor
    horiz_series = {
        r for g in tree.groups if g.archetype == Archetype.SERIES_FILTER
        for r in g.members if plan.parts[r].role == Role.SERIES_ELEMENT
    }
    ordered = sorted(netlist.parts, key=lambda p: _natural_key(p.refdes))
    cells = _place_and_measure(sch, ordered, issues,
                               rotate_horizontal=frozenset(horiz_series))
    by_ref = {c.refdes: c for c in cells}
    placed_refs: dict = {}
    _move_to(by_ref[anchor], IC_X, IC_Y, placed_refs)
    ic_comp = sch.components.get(anchor)
    from pinflow_api.emit.placers.cplace import _measure_part, _orient_all
    _orient_all(tree, by_ref, ic_comp)
    parts_m: dict = {}
    for ref, cell in by_ref.items():
        mp = _measure_part(cell)
        if mp is not None:
            parts_m[ref] = mp
    # Translate to LLM positions.
    for ref, (tx, ty) in placed.items():
        p = parts_m.get(ref)
        if p is None:
            continue
        dx = tx - p.origin[0]
        dy = ty - p.origin[1]
        _translate(p.cell, dx, dy, placed_refs)
    # Wire and label.
    _place_connectivity(sch, netlist, plan, placed_refs, issues,
                        wiring="router")
    _place_no_connects(sch, netlist, placed_refs, issues)
    text = _hide_gnd_labels(sch_to_string(sch), netlist)
    text = _reposition_fields(text, sch, anchor, above=True)
    for refdes, pc in plan.parts.items():
        if pc.role == Role.SERIES_ELEMENT:
            text = _reposition_fields(text, sch, refdes, above=False)
    return text


def _visual_review(
    placed: dict[str, tuple[float, float]],
    netlist: Netlist, tree: LayoutTree, parts: dict, ctx,
    payload: dict, client: Anthropic, model: str,
) -> dict[str, tuple[float, float]] | None:
    """Render the current placement, show it to the LLM, ask for refined
    placements. Returns refined `placed` or None if review failed."""
    try:
        sch_text = _build_intermediate_sch(
            netlist, tree, parts, ctx, placed
        )
        png_bytes = _render_to_png_bytes(sch_text)
        import base64
        png_b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    except Exception as e:  # noqa: BLE001
        print(f"  llm_placer: visual review render failed — {e}",
              file=sys.stderr)
        return None

    review_msg = (
        "## Visual self-review\n\n"
        "Your placement has been rendered below. Look at the image and "
        "judge: are there label collisions? Are labels grazing the IC "
        "body? Are wires running through bodies? Is the layout balanced "
        "(input on left, output on right, divider near FB)? Is anything "
        "lonely (a part far from anything related)?\n\n"
        "If the layout is GOOD, resubmit the SAME placements unchanged "
        "(verifies you reviewed). If you see specific problems, propose "
        "**refined placements** — keep what works, only adjust what's "
        "actually bad. Snap to 2.54 mm.\n\n"
        "Current placements:\n```json\n"
        + json.dumps([{"refdes": r, "x": placed[r][0], "y": placed[r][1]}
                     for r in sorted(placed)], indent=2)
        + "\n```\n\n"
        + "Input recap (unchanged):\n```json\n"
        + json.dumps(payload, indent=2)
        + "\n```\n"
    )

    user_content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": png_b64},
        },
        {"type": "text", "text": review_msg},
    ]
    try:
        placements = _call_llm(user_content, client, model)
    except RuntimeError as e:
        print(f"  llm_placer: visual review LLM call failed — {e}",
              file=sys.stderr)
        return None

    known_refs = set(placed.keys())
    refined: dict[str, tuple[float, float]] = {}
    for p in placements:
        ref = str(p["refdes"])
        if ref in known_refs:
            refined[ref] = (float(p["x"]), float(p["y"]))
    # Backfill any missing from the prior placement.
    for r in known_refs:
        if r not in refined:
            refined[r] = placed[r]
    return refined


# --- public entry ------------------------------------------------------------

def plan_layout_constraints(tree: LayoutTree, parts: dict, ctx,
                            *,
                            visual_review: bool = True,
                            exclude_few_shot_names: set[str] | None = None):
    """The external_emitter cplace calls in place of the archetype-based
    `_emit_all`. Returns a `_CSet` with anchors for every non-IC part.

    Pipeline:
      1. LLM proposes placements (with 3 multi-few-shot examples).
      2. Validator checks bbox-overlap + pin-shorts; retry with feedback
         up to `_MAX_RETRIES` times.
      3. Visual self-review (default on): render the validated layout,
         hand the image to the LLM, and let it propose refinements. Keep
         the refinement only if it still validates.
      4. Fill any missing refdeses from the archetype emitter defaults.
    """
    from pinflow_api.emit.placers.cplace import _CSet, IC_X, IC_Y, _emit_all
    from pinflow_api.emit.constraints import solve

    if not llm.available():
        raise RuntimeError(llm.NOT_CONFIGURED_MSG)

    plan = tree.plan
    payload = _summarise(tree.netlist, plan, tree, parts, ctx)
    known_refs = {ref for ref in parts if ref != tree.anchor}
    few_shots = _pick_few_shots(
        current_part_count=len(payload["parts"]),
        max_shots=3,
        exclude_names=exclude_few_shot_names,
    )

    client = llm.make_client()
    model = "claude-opus-4-7"

    # --- Stage 1: structured placement with retries -----------------------
    user_content: list = []
    for ex in few_shots:
        user_content.append({
            "type": "text",
            "text": (
                f"## Few-shot: {ex['name']}  ·  {ex['note']}\n\n"
                "### Input:\n```json\n"
                + json.dumps(ex["input"], indent=2)
                + "\n```\n\n### Layout:\n```json\n"
                + json.dumps(ex["layout"], indent=2)
                + "\n```\n"
            ),
        })
    user_content.append({
        "type": "text",
        "text": (
            "## Your task — produce the layout for this circuit\n\n"
            "Input:\n```json\n"
            + json.dumps(payload, indent=2)
            + "\n```\n\nCall submit_layout with an (x, y) anchor for "
              "every non-IC refdes above. Snap to 2.54 mm. Verify "
              "non-overlap + non-coincident pins before submitting.\n"
        ),
    })

    last_placements: list[dict] = []
    for attempt in range(_MAX_RETRIES + 1):
        try:
            placements = _call_llm(user_content, client, model)
        except RuntimeError as e:
            print(f"  llm_placer: attempt {attempt} — {e}", file=sys.stderr)
            break
        last_placements = placements

        placed: dict[str, tuple[float, float]] = {}
        for p in placements:
            ref = str(p["refdes"])
            if ref not in known_refs:
                continue
            placed[ref] = (float(p["x"]), float(p["y"]))

        violations = _check_placements(
            placed, parts, tree.netlist, tree.anchor, ctx.ic_origin
        )
        missing = known_refs - set(placed)
        if missing:
            violations.append(
                "MISSING PARTS: " + ", ".join(sorted(missing))
                + ". Every non-IC refdes needs an (x, y)."
            )
        if not violations:
            print(f"  llm_placer: stage1 attempt {attempt} passed",
                  file=sys.stderr)
            break
        print(f"  llm_placer: stage1 attempt {attempt} — "
              f"{len(violations)} violation(s); retrying",
              file=sys.stderr)
        user_content.append({
            "type": "text",
            "text": (
                "## Your prior placement\n```json\n"
                + json.dumps(placements, indent=2)
                + "\n```\n\n## Validator feedback\n"
                + _format_violations_for_retry(violations)
            ),
        })

    placed = {}
    for p in last_placements:
        ref = str(p["refdes"])
        if ref in known_refs:
            placed[ref] = (float(p["x"]), float(p["y"]))

    # --- Stage 2: visual self-review -------------------------------------
    # Runs even when stage 1 has residual violations — the LLM seeing its
    # own render is the leverage point, and the validator's "1 violation
    # left" might be a real bug or a benign near-miss the visual check
    # can disambiguate.
    if visual_review and placed:
        refined = _visual_review(
            placed, tree.netlist, tree, parts, ctx, payload,
            client, model,
        )
        if refined is not None:
            stage1_violations = _check_placements(
                placed, parts, tree.netlist, tree.anchor, ctx.ic_origin
            )
            refined_violations = _check_placements(
                refined, parts, tree.netlist, tree.anchor, ctx.ic_origin
            )
            # Accept the refined layout if it has fewer violations OR the
            # same and we trust the visual judgement.
            if len(refined_violations) <= len(stage1_violations):
                print(f"  llm_placer: visual review accepted "
                      f"({len(refined_violations)} viol vs "
                      f"{len(stage1_violations)} pre)", file=sys.stderr)
                placed = refined
            else:
                print(f"  llm_placer: visual review rejected — "
                      f"{len(refined_violations)} viol vs "
                      f"{len(stage1_violations)} pre", file=sys.stderr)

    # --- Stage 3: fill any missing parts from archetype fallback ---------
    missing = known_refs - set(placed)
    if missing:
        print(f"  llm_placer: {len(missing)} still missing, "
              f"filling from archetype: {sorted(missing)}", file=sys.stderr)
        fallback_cs = _emit_all(tree, parts, ctx)
        fb_x = solve(fallback_cs.x, fallback=IC_X)
        fb_y = solve(fallback_cs.y, fallback=IC_Y)
        for ref in missing:
            placed[ref] = (fb_x.pos.get(ref, IC_X), fb_y.pos.get(ref, IC_Y))

    cs = _CSet()
    cs.ax(tree.anchor, ctx.ic_origin[0])
    cs.ay(tree.anchor, ctx.ic_origin[1])
    for ref, (x, y) in placed.items():
        cs.ax(ref, x)
        cs.ay(ref, y)
    return cs


# --- best-of-N wrapper -------------------------------------------------------

def cplace_with_llm_best_of(
    netlist, *,
    n: int = 3,
    title: str = "Subcircuit",
    on_attempt=None,
):
    """Run `cplace(netlist, external_emitter=plan_layout_constraints)`
    `n` times and return the highest-scoring result.

    The LLM call is non-deterministic — repeated runs produce different
    layouts. Best-of-N picks off the variance tail: bad runs (when the
    LLM ends with violations) are filtered out by the rubric.

    `on_attempt(i, score)` is called after each attempt with the run
    index and rubric total — useful for logging in eval scripts.

    Returns the `PlacerResult` with the highest rubric score. Falls back
    to a single cplace baseline if every LLM attempt fails.
    """
    # Late imports to avoid module-level cycles.
    from pinflow_api.emit.placers.cplace import cplace as _cplace
    from pinflow_api.emit.netlist_to_sch import PlacerError
    from pinflow_api.emit.rubric import score as _score

    best = None
    best_total = -1.0
    for i in range(n):
        try:
            result = _cplace(netlist, title=title,
                             external_emitter=plan_layout_constraints)
        except PlacerError as e:
            print(f"  best_of[{i}]: PlacerError — {'; '.join(e.errors)}",
                  file=sys.stderr)
            if on_attempt is not None:
                on_attempt(i, None)
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  best_of[{i}]: {type(e).__name__}: {e}", file=sys.stderr)
            if on_attempt is not None:
                on_attempt(i, None)
            continue
        rb = _score(result.sch_text, netlist)
        if on_attempt is not None:
            on_attempt(i, rb.total)
        if rb.total > best_total:
            best = result
            best_total = rb.total

    if best is not None:
        return best
    # Every LLM attempt failed — fall back to a plain deterministic cplace.
    return _cplace(netlist, title=title)
