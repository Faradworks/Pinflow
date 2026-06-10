"""Layout quality rubric — the eval instrument for the netlist→schematic placer.

`score(sch_text, netlist) -> RubricScore` turns a `.kicad_sch` into a structured
quality report. It is the research instrument behind the placer rebuild: the
same rubric is the eval metric (`scripts/eval_layout.py`), and is intended later
as the solver's objective and a refiner's gate.

The rubric scores *craft* — the geometric readability a human schematic has and
the current placer lacks (cramped banks, colliding label text, mis-aligned
parts). It does **not** score topology: connectivity is verified separately and
treated as a hard gate. Two kinds of result:

  - **Gates** (`connectivity`, `symbol_overlap`) — pass/fail. A fail is a
    schematic that is wrong or unusable, never just ugly. `None` = not
    evaluable (no netlist supplied, or kicad-cli unavailable).
  - **Metrics** — each a raw measurement plus a 0..1 sub-score (1 = ideal) and
    a weight. The weighted mean over the evaluable metrics is `total`.

The metrics:

  - `label_collision`  — Reference/Value labels overprinting each other — a
                         part's own two, or two parts'. The clearest defect.
  - `wire_crossings`   — wire segments crossing at an interior point.
  - `wire_orthogonality` — diagonal (non-orthogonal) segments. Should be 0.
  - `alignment`        — do parts that should share an axis (a cap bank's Y, a
                         divider's X) actually share it?
  - `spacing`          — is the pitch within a cap bank even?
  - `flow`             — does X increase input-rail → IC → output-rail?
  - `off_grid`         — fraction of parts / wire endpoints off the 1.27 mm
                         KiCad schematic grid.
  - `compactness`      — part-area / content-area density (a soft guard-rail
                         against pathologically cramped or sparse output).

Two calibration points learned from the golden corpus, worth knowing if you
extend this:

  - **Text boxes are measured here, not via `bbox._field_box`.** That helper
    reads only a field's *stored* angle; KiCad sums the symbol rotation into
    the rendered angle, so a horizontal value on a rotated ferrite reads to it
    as a tall vertical strip. `_text_box` below uses the *effective* angle.
    (The placer shares that `bbox` bug, so it under-spaces rotated parts — a
    finding for the placer rebuild, not fixed here.)
  - **The grid is 1.27 mm**, KiCad's schematic default — not the 2.54 mm the
    placer happens to snap to. A hand-drawn golden sits on 1.27.

The group-aware metrics (`alignment`, `spacing`, `flow`) need the netlist —
they run it through `emit.classify` to recover roles and the feedback divider,
exactly as the placer does. For full fidelity the caller must have the
netlist's symbol libraries discovered (`ksa.get_symbol_cache()
.discover_libraries(...)`) before calling — `classify` and the bbox
measurement both read symbol geometry.
"""

from __future__ import annotations

import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import kicad_sch_api as ksa
from kicad_sch_api.core.component_bounds import get_component_bounding_box

from pinflow_api.emit.classify import LayoutPlan, NetKind, Role, classify
from pinflow_api.emit.netlist import Netlist

# (min_x, min_y, max_x, max_y) in mm.
BBox = tuple[float, float, float, float]
Pt = tuple[float, float]
Seg = tuple[Pt, Pt]

_EPS = 0.01
_SCH_GRID = 1.27        # KiCad's default schematic grid (50 mil)
_GLYPH_ADV = 0.72       # stock-font glyph advance ÷ text height (mirrors bbox.py)
_COLLIDE_MARGIN = 0.0   # was 0.6 — the shrink was hiding real visible
                        # collisions a human reader flags. The rubric's job
                        # is to penalise what looks bad in the render; a
                        # bbox graze is already bad to a reader. Use a tiny
                        # value if you want to permit numerical epsilon.
_PROX_THRESH = 0.5      # mm — label pairs whose bboxes don't overlap but
                        # come within this distance still penalty-count
                        # (the "crowded" pattern visible on dense ICs).
_ROW_TOL = 2.54         # parts within one grid step of a shared axis read as
                        # "meant to align"; wider apart is a deliberate offset.
_ALIGN_SLACK = 1.27     # drift within one fine grid step is hand-drawn
                        # tolerance — not scored as misalignment.
_OVERLAP_MARGIN = 2.54  # body boxes inset a full pin length before the overlap
                        # gate — `get_component_bounding_box` includes pin
                        # stubs, so parts wired pin-to-pin graze without
                        # colliding; only true body-on-body overlap survives.

# Per-metric weight in the aggregate. Sums to 1.0; the aggregate renormalises
# over whichever metrics were evaluable, so a missing metric doesn't deflate
# the score. The headline defect (label collisions) and the structural
# readability metrics (alignment, spacing, flow) carry the weight; off-grid
# and compactness are guard-rails.
_WEIGHTS: dict[str, float] = {
    "label_collision": 0.18,
    "wire_crossings": 0.13,
    "wire_through_part": 0.13,
    "chain_coherence": 0.10,
    "rail_proximity": 0.08,
    "alignment": 0.10,
    "wire_orthogonality": 0.08,
    "spacing": 0.08,
    "flow": 0.07,
    "off_grid": 0.03,
    "compactness": 0.02,
}

# A failed gate (connectivity or symbol_overlap) multiplies `total` by this —
# a structurally broken schematic should not score in the 0.9s no matter how
# pretty the surviving geometry is. 0.3 is harsh enough to make the score
# unambiguously bad without collapsing all signal (so the metric deltas still
# tell you which kind of break it is).
_GATE_PENALTY = 0.3


# --- result types ------------------------------------------------------------

@dataclass
class MetricResult:
    """One scored metric: a raw measurement plus a normalised 0..1 sub-score.

    `score` is `None` when the metric could not be evaluated (e.g. a
    group-aware metric with no netlist, or no group of that kind in the
    circuit) — it is then excluded from the aggregate.
    """

    name: str
    raw: float
    score: float | None
    weight: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "raw": self.raw,
            "score": self.score,
            "weight": self.weight,
            "detail": self.detail,
        }


@dataclass
class RubricScore:
    """A full quality report for one schematic."""

    gates: dict[str, bool | None]
    metrics: list[MetricResult]
    total: float
    passed: bool
    notes: list[str] = field(default_factory=list)

    def metric(self, name: str) -> MetricResult | None:
        return next((m for m in self.metrics if m.name == name), None)

    def to_dict(self) -> dict:
        return {
            "gates": self.gates,
            "passed": self.passed,
            "total": self.total,
            "metrics": [m.to_dict() for m in self.metrics],
            "notes": self.notes,
        }


# --- geometry helpers --------------------------------------------------------

def _decay(n: float, half: float) -> float:
    """Map a defect count to a 0..1 score: 0 defects → 1.0, `half` → 0.5,
    decaying smoothly toward 0. `half` is the count judged 'half as good as
    perfect' — no hard cap, no negative scores."""
    return half / (half + max(0.0, n))


def _rects_overlap(a: BBox, b: BBox) -> bool:
    """True if two axis-aligned boxes share positive area."""
    return not (
        a[2] <= b[0] + _EPS or b[2] <= a[0] + _EPS
        or a[3] <= b[1] + _EPS or b[3] <= a[1] + _EPS
    )


def _shrink(b: BBox, m: float) -> BBox:
    """Inset a box by `m` on every side — degrades gracefully if the box is
    smaller than `2m` (collapses toward its centre, never inverts)."""
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return (
        min(b[0] + m, cx), min(b[1] + m, cy),
        max(b[2] - m, cx), max(b[3] - m, cy),
    )


def _seg_orient(s: Seg) -> str:
    (x0, y0), (x1, y1) = s
    if abs(y0 - y1) < _EPS:
        return "h"
    if abs(x0 - x1) < _EPS:
        return "v"
    return "d"


def _segments_cross(a: Seg, b: Seg) -> bool:
    """True if orthogonal segments `a` and `b` intersect at a point interior to
    both — a genuine crossing. A shared endpoint or a T-junction is not a
    crossing. Mirrors `emit.route._crosses` (kept local so the scorer doesn't
    depend on the router's internals — they are peers)."""
    oa, ob = _seg_orient(a), _seg_orient(b)
    if {oa, ob} != {"h", "v"}:
        return False
    h, v = (a, b) if oa == "h" else (b, a)
    hy = h[0][1]
    hx_lo, hx_hi = sorted((h[0][0], h[1][0]))
    vx = v[0][0]
    vy_lo, vy_hi = sorted((v[0][1], v[1][1]))
    return (hx_lo + _EPS < vx < hx_hi - _EPS
            and vy_lo + _EPS < hy < vy_hi - _EPS)


def _on_grid(v: float) -> bool:
    q = v / _SCH_GRID
    return abs(q - round(q)) < 0.02


def _load(sch_text: str) -> ksa.Schematic:
    """Load schematic text via a temp file — kicad-sch-api has no from-string
    constructor; mirrors `structural_diff._load`."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        tmp = Path(f.name)
        f.write(sch_text)
    try:
        return ksa.load_schematic(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def _is_real_part(ref: str) -> bool:
    """A netlist component, not a virtual power/flag symbol (`#PWR*`, `#FLG*`)."""
    return bool(ref) and not ref.startswith("#")


def _is_ic(ref: str) -> bool:
    """An IC (`U*`). Its bounding box spans many pins, so a support part placed
    correctly against those pins overlaps the IC's box without being a defect —
    IC-involved pairs are therefore excluded from the overlap gate."""
    return _is_real_part(ref) and ref[:1].upper() == "U"


# --- schematic geometry extraction -------------------------------------------

def _components_by_ref(sch: ksa.Schematic) -> dict[str, list]:
    """Group component objects by refdes — >1 entry for a multi-unit symbol."""
    out: dict[str, list] = {}
    for c in sch.components:
        out.setdefault(str(c.reference), []).append(c)
    return out


def _wire_segments(sch: ksa.Schematic) -> list[Seg]:
    """Every straight wire segment — one per consecutive vertex pair, dropping
    degenerate zero-length pairs."""
    segs: list[Seg] = []
    for wire in sch.wires:
        pts = [(float(p.x), float(p.y)) for p in wire.points]
        for a, b in zip(pts, pts[1:]):
            if abs(a[0] - b[0]) > _EPS or abs(a[1] - b[1]) > _EPS:
                segs.append((a, b))
    return segs


def _body_boxes(by_ref: dict[str, list]) -> dict[str, BBox]:
    """Per-refdes symbol-body box (symbol + pins, *no* Reference/Value text) —
    real parts only. Field text is measured separately by `_text_box`."""
    out: dict[str, BBox] = {}
    for ref, comps in by_ref.items():
        if not _is_real_part(ref):
            continue
        boxes: list[BBox] = []
        for c in comps:
            try:
                bb = get_component_bounding_box(c, include_properties=False)
                boxes.append((bb.min_x, bb.min_y, bb.max_x, bb.max_y))
            except Exception:
                continue
        if boxes:
            out[ref] = (
                min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes),
            )
    return out


def _text_box(comp, name: str) -> BBox | None:
    """On-page box of a component's visible Reference/Value field, or None.

    Like `bbox._field_box` but orientation uses the *effective* page angle —
    the component's rotation summed with the field's own stored angle, as
    KiCad renders it. `bbox._field_box` reads only the stored angle and so
    mis-orients fields on rotated symbols. Justify is applied only when the
    text lands horizontal; a rotated field is treated as centre-anchored
    (which is how the placer emits repositioned fields anyway)."""
    try:
        eff = comp.get_property_effects(name)
        raw = comp.get_property(name)
    except Exception:
        return None
    if not eff or not eff.get("visible", True):
        return None
    text = str(raw.get("value", "") if isinstance(raw, dict) else (raw or ""))
    if not text:
        return None
    px, py = eff["position"]
    fx, fy = eff.get("font_size") or (1.27, 1.27)
    w = len(text) * fx * _GLYPH_ADV
    h = fy
    comp_rot = float(getattr(comp, "rotation", 0) or 0)
    field_rot = float(eff.get("rotation", 0) or 0)
    rotated = int(round(comp_rot + field_rot)) % 180 == 90
    if rotated:
        w, h = h, w
    jh = None if rotated else eff.get("justify_h")
    jv = None if rotated else eff.get("justify_v")
    x0, x1 = (
        (px, px + w) if jh == "left"
        else (px - w, px) if jh == "right"
        else (px - w / 2, px + w / 2)
    )
    y0, y1 = (
        (py, py + h) if jv == "top"
        else (py - h, py) if jv == "bottom"
        else (py - h / 2, py + h / 2)
    )
    return (x0, y0, x1, y1)


def _label_box(label) -> BBox:
    """On-page box of a net label (`sch.labels` / `sch.global_labels` /
    `sch.hierarchical_labels`). KiCad anchors a label at the connection point
    and extends the text in the rotation direction (0°=right, 90°=up,
    180°=left, 270°=down). Heuristic but matches how KiCad renders."""
    pos = label.position
    px, py = float(pos[0]), float(pos[1])
    size = float(getattr(label, "size", 1.27) or 1.27)
    text = str(getattr(label, "text", "") or "")
    w = len(text) * size * _GLYPH_ADV
    h = size
    rot = int(round(float(getattr(label, "rotation", 0) or 0))) % 360
    if rot == 0:
        return (px, py - h / 2, px + w, py + h / 2)
    if rot == 90:
        return (px - h / 2, py, px + h / 2, py + w)
    if rot == 180:
        return (px - w, py - h / 2, px, py + h / 2)
    if rot == 270:
        return (px - h / 2, py - w, px + h / 2, py)
    # diagonal — measure as a square bounding the rotated rect
    half = max(w, h) / 2
    return (px - half, py - half, px + half, py + half)


def _text_boxes(sch: ksa.Schematic,
                by_ref: dict[str, list]) -> list[tuple[str, BBox]]:
    """(owner_tag, box) for every visible label on the page: component
    Reference/Value fields and the net labels (sch.labels / global / hier).
    Net labels are *visible text* — when two labels stack at the same pin
    coordinate (the dense-IC failure mode) they overprint as plainly as any
    component field. Free `(text)` annotation is still excluded — it's
    margin commentary, not placement."""
    out: list[tuple[str, BBox]] = []
    for ref, comps in by_ref.items():
        for c in comps:
            for fname in ("Reference", "Value"):
                box = _text_box(c, fname)
                if box is not None:
                    out.append((ref, box))
    for kind, attr in (("label", "labels"),
                       ("global", "global_labels"),
                       ("hier", "hierarchical_labels")):
        for L in getattr(sch, attr, []):
            try:
                box = _label_box(L)
            except Exception:
                continue
            out.append((f"{kind}:{getattr(L, 'text', '?')}", box))
    return out


def _content_bbox(body_boxes: dict[str, BBox], segs: list[Seg]) -> BBox | None:
    """Tight box around all drawn content — part bodies ∪ wire endpoints."""
    xs: list[float] = []
    ys: list[float] = []
    for b in body_boxes.values():
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    for a, b in segs:
        xs += [a[0], b[0]]
        ys += [a[1], b[1]]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


# --- gates -------------------------------------------------------------------

def _gate_connectivity(sch_text: str, netlist: Netlist | None) -> bool | None:
    """Does KiCad's own netlister see the same net topology as `netlist`?

    `None` when it cannot be judged (no netlist, or kicad-cli unavailable) —
    the caller treats `None` as 'not a failure'. Catches both shorts (two nets
    merged) and breaks. Reuses `structural_diff`'s authoritative export."""
    if netlist is None:
        return None
    # Function-local import: structural_diff imports netlist_to_sch, and
    # keeping this off the module top level avoids dragging that graph into
    # every rubric import.
    from pinflow_api.emit.structural_diff import (
        _export_topology,
        _netlist_topology,
    )
    exported = _export_topology(sch_text)
    if exported is None:
        return None
    return exported == _netlist_topology(netlist)


def _gate_symbol_overlap(body_boxes: dict[str, BBox]) -> tuple[bool, int]:
    """No two non-IC part bodies may substantially overlap. Boxes are inset by
    `_OVERLAP_MARGIN` first: `get_component_bounding_box` includes pin stubs,
    so two parts wired pin-to-pin have grazing boxes that are not a collision —
    only a real body-on-body overlap survives the inset. IC-involved pairs are
    excluded — an IC's box spans its whole pin field. Returns (passed, count)."""
    refs = sorted(r for r in body_boxes if not _is_ic(r))
    boxes = {r: _shrink(body_boxes[r], _OVERLAP_MARGIN) for r in refs}
    collisions = sum(
        1
        for i in range(len(refs))
        for j in range(i + 1, len(refs))
        if _rects_overlap(boxes[refs[i]], boxes[refs[j]])
    )
    return collisions == 0, collisions


# --- pure-geometry metrics ---------------------------------------------------

def _metric_label_collision(
    text_boxes: list[tuple[str, BBox]]
) -> MetricResult:
    """Count Reference/Value labels that overprint another label — a part's
    own two fields colliding, or two different parts' — always unreadable, the
    clearest layout defect. Boxes are inset by `_COLLIDE_MARGIN` first so a
    hairline graze does not count.

    Scoped to text-on-text: a label *near a wire* is routinely fine (a power
    flag sits on its own stem; a value sits beside a 2-pin part's pin), so
    text-on-wire false-positives even on the golden and is not scored.
    Crowding that stops short of true overlap is left to `spacing`."""
    # Exclude pairs that aren't real visible defects:
    #   - SAME OWNER: a component's own Reference + Value text sharing an
    #     anchor (KiCad's default — Reference is hidden, Value is the visible
    #     label). Not a visible overlap.
    #   - SAME TEXT: two power symbols both reading "GND" or "+5V". Visually
    #     a single label, semantically correct; not a defect.
    # Extract the visible text for both filters.
    pairs = []
    for ref, _box in text_boxes:
        pairs.append((ref.split(":", 1)[0].split(".", 1)[0], _box))

    def _same_owner(i, j):
        return pairs[i][0] == pairs[j][0]
    # Same-text needs the text — pull it from the owner tag where possible
    # (only label:* entries carry it embedded). For component fields the
    # text isn't here, so use a coarser proxy: same-owner already covers
    # the Reference/Value pair. Cross-component same-text is the
    # power-symbol case we want to exclude: detect via the leading "#PWR"
    # prefix (KiCad's power symbols).
    def _both_pwr(i, j):
        return (pairs[i][0].startswith("#PWR")
                and pairs[j][0].startswith("#PWR"))

    boxes = [p[1] for p in pairs]
    n = len(boxes)
    # Strict overlap (any bbox touch).
    hits = 0
    for i in range(n):
        for j in range(i + 1, n):
            if _same_owner(i, j) or _both_pwr(i, j):
                continue
            if _rects_overlap(boxes[i], boxes[j]):
                hits += 1
    # Crowding: bboxes don't overlap but sit closer than _PROX_THRESH mm.
    crowd = 0
    for i in range(n):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, n):
            if _same_owner(i, j) or _both_pwr(i, j):
                continue
            bx0, by0, bx1, by1 = boxes[j]
            if _rects_overlap(boxes[i], boxes[j]):
                continue
            dx = max(0.0, max(bx0 - ax1, ax0 - bx1))
            dy = max(0.0, max(by0 - ay1, ay0 - by1))
            if (dx * dx + dy * dy) ** 0.5 < _PROX_THRESH:
                crowd += 1
    score_hits = hits + 0.5 * crowd
    return MetricResult(
        "label_collision", float(score_hits), _decay(score_hits, 2.0),
        _WEIGHTS["label_collision"],
        f"{hits} overlap(s), {crowd} near-miss(es)",
    )


def _metric_wire_crossings(segs: list[Seg]) -> MetricResult:
    """Wire-segment crossings. Raw count over-counts the rare same-net
    crossing, but a crossing is a readability cost regardless of net. Half-
    quality at 3 — a dense schematic carries a couple of crossings even when
    hand-drawn (the golden tps61088 has 4), but a placer that piles up 10+ is
    plainly wrong; this calibration distinguishes those cases."""
    hits = sum(
        1
        for i in range(len(segs))
        for j in range(i + 1, len(segs))
        if _segments_cross(segs[i], segs[j])
    )
    return MetricResult(
        "wire_crossings", float(hits), _decay(hits, 3.0),
        _WEIGHTS["wire_crossings"], f"{hits} crossing(s)",
    )


def _seg_through_box(seg: Seg, box: BBox) -> bool:
    """True if any *interior* point of the segment falls inside `box`. A
    wire terminating at the box edge (e.g. at a pin) does not count — only
    a strict interior crossing. Works for both orthogonal and diagonal
    segments via a parametric inside-test."""
    (x0, y0), (x1, y1) = seg
    bx0, by0, bx1, by1 = box
    if bx1 - bx0 < _EPS or by1 - by0 < _EPS:
        return False
    # Sample three interior points (¼, ½, ¾ along the segment); if any is
    # inside the box, the segment cuts through.
    for t in (0.25, 0.5, 0.75):
        x = x0 + (x1 - x0) * t
        y = y0 + (y1 - y0) * t
        if bx0 + _EPS < x < bx1 - _EPS and by0 + _EPS < y < by1 - _EPS:
            return True
    return False


def _ep_inside(ep: Pt, box: BBox, slop: float = _EPS) -> bool:
    """True if endpoint `ep` is inside box `box` (incl. on the boundary
    within `slop`) — i.e. the wire is plausibly connecting to a pin that
    lives inside the body's bbox margin, not cutting through it. The default
    slop is `_EPS`: an endpoint exactly on the boundary still counts as
    'connected' because pin tips often sit on a bbox edge."""
    bx0, by0, bx1, by1 = box
    return (bx0 - slop < ep[0] < bx1 + slop
            and by0 - slop < ep[1] < by1 + slop)


def _metric_wire_through_part(
    segs: list[Seg], body_boxes: dict[str, BBox]
) -> MetricResult:
    """Wire segments cutting *through* a part body — both endpoints outside
    the body bbox, with interior crossing the body interior. The visual
    smell of a placer that didn't know there was a component in the way.
    A wire endpoint inside the bbox is a legitimate pin connection (pins
    sit inside the bbox because the box covers pin stubs); we exempt those.
    The body interior for the crossing test is shrunk slightly so a wire
    grazing the body's outer edge doesn't false-positive."""
    if not body_boxes:
        return MetricResult("wire_through_part", 0.0, 1.0,
                            _WEIGHTS["wire_through_part"], "no bodies")
    interiors = {ref: _shrink(b, 0.4 if not _is_ic(ref) else 1.0)
                 for ref, b in body_boxes.items()}
    violations = 0
    for seg in segs:
        for ref, body in body_boxes.items():
            # Either endpoint inside body's bbox: legitimate pin connection
            # against this body. Skip — no violation against *this* body.
            if _ep_inside(seg[0], body) or _ep_inside(seg[1], body):
                continue
            if _seg_through_box(seg, interiors[ref]):
                violations += 1
                break  # one violation per segment
    return MetricResult(
        "wire_through_part", float(violations), _decay(violations, 2.0),
        _WEIGHTS["wire_through_part"],
        f"{violations} wire(s) cutting through a body",
    )


def _metric_wire_orthogonality(segs: list[Seg]) -> MetricResult:
    """Diagonal (non-orthogonal) segments — the strongest wiring smell; a clean
    schematic has none."""
    diag = sum(1 for s in segs if _seg_orient(s) == "d")
    return MetricResult(
        "wire_orthogonality", float(diag),
        1.0 if diag == 0 else _decay(diag, 2.0),
        _WEIGHTS["wire_orthogonality"],
        f"{diag} diagonal of {len(segs)} segment(s)",
    )


def _metric_off_grid(by_ref: dict[str, list], segs: list[Seg]) -> MetricResult:
    """Fraction of part origins and wire endpoints off the 1.27 mm grid."""
    coords: list[bool] = []
    for ref, comps in by_ref.items():
        if not _is_real_part(ref):
            continue
        for c in comps:
            coords.append(_on_grid(float(c.position.x))
                          and _on_grid(float(c.position.y)))
    for a, b in segs:
        coords.append(_on_grid(a[0]) and _on_grid(a[1]))
        coords.append(_on_grid(b[0]) and _on_grid(b[1]))
    if not coords:
        return MetricResult("off_grid", 0.0, None, _WEIGHTS["off_grid"],
                            "nothing to check")
    off = sum(1 for ok in coords if not ok)
    frac = off / len(coords)
    return MetricResult(
        "off_grid", frac, 1.0 - frac, _WEIGHTS["off_grid"],
        f"{off}/{len(coords)} endpoints off-grid",
    )


def _metric_compactness(
    body_boxes: dict[str, BBox], segs: list[Seg]
) -> MetricResult:
    """Part-area / content-area density — a soft guard-rail. Scored on a wide
    band so it nudges only against pathologically cramped or sparse output;
    the golden corpus sits comfortably inside the band."""
    content = _content_bbox(body_boxes, segs)
    if content is None:
        return MetricResult("compactness", 0.0, None,
                            _WEIGHTS["compactness"], "empty schematic")
    cw, ch = content[2] - content[0], content[3] - content[1]
    area = cw * ch
    if area < 1.0:
        return MetricResult("compactness", 0.0, None,
                            _WEIGHTS["compactness"], "degenerate extent")
    part_area = sum((b[2] - b[0]) * (b[3] - b[1]) for b in body_boxes.values())
    density = part_area / area
    lo, hi = 0.05, 0.65
    if lo <= density <= hi:
        sub = 1.0
    elif density < lo:
        sub = max(0.0, density / lo)
    else:
        sub = max(0.0, 1.0 - (density - hi) / hi)
    return MetricResult(
        "compactness", density, sub, _WEIGHTS["compactness"],
        f"density {density:.3f} over {cw:.0f}x{ch:.0f}mm",
    )


# --- layout-structure metrics ------------------------------------------------

def _pos_by_ref(by_ref: dict[str, list]) -> dict[str, Pt]:
    """One (x, y) per refdes — the symbol origin (first unit for multi-unit)."""
    out: dict[str, Pt] = {}
    for ref, comps in by_ref.items():
        if comps:
            p = comps[0].position
            out[ref] = (float(p.x), float(p.y))
    return out


def _prefix(ref: str) -> str:
    """Alphabetic refdes prefix — 'C12' → 'C', 'FB1' → 'FB'."""
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref


def _aligned_runs(
    parts: list[tuple[str, float, float]]
) -> list[tuple[int, list[tuple[str, float, float]]]]:
    """Runs of parts sharing an axis within `_ROW_TOL` — a horizontal row
    (shared Y) or a vertical column (shared X). Returns (axis, run) for every
    run of >=2 parts; axis 0 = X (a column), 1 = Y (a row). A run is the
    layout's own evidence that the parts were *meant* to line up — parts more
    than one grid step apart are a deliberate offset and never compared."""
    runs: list[tuple[int, list]] = []
    for axis in (0, 1):
        ordered = sorted(parts, key=lambda p: p[1 + axis])
        cur: list = ordered[:1]
        for p in ordered[1:]:
            if p[1 + axis] - cur[-1][1 + axis] <= _ROW_TOL:
                cur.append(p)
            else:
                if len(cur) >= 2:
                    runs.append((axis, cur))
                cur = [p]
        if len(cur) >= 2:
            runs.append((axis, cur))
    return runs


def _metric_alignment(pos: dict[str, Pt]) -> MetricResult:
    """Among parts the layout places in a row or column — same kind, sharing
    an axis within one grid step — how exactly do they line up? Geometry-
    derived: it makes no assumption about which parts *should* group, so it is
    topology-agnostic — a horizontal cap bank, a vertical divider stack, a
    series feedback resistor all just work. Mean cross-spread of the detected
    runs; 0 mm = crisp, half-quality at one grid step of drift."""
    by_kind: dict[str, list[tuple[str, float, float]]] = {}
    for ref, (x, y) in pos.items():
        if _is_real_part(ref) and not _is_ic(ref):
            by_kind.setdefault(_prefix(ref), []).append((ref, x, y))
    spreads: list[float] = []
    for parts in by_kind.values():
        for axis, run in _aligned_runs(parts):
            coords = [p[1 + axis] for p in run]
            spreads.append(max(coords) - min(coords))
    if not spreads:
        return MetricResult("alignment", 0.0, None, _WEIGHTS["alignment"],
                            "no row or column to check")
    mean_spread = statistics.mean(spreads)
    # Drift within one fine grid step is within hand-drawn tolerance — a
    # golden carries it; only the excess past that is scored as misalignment.
    excess = statistics.mean(max(0.0, s - _ALIGN_SLACK) for s in spreads)
    return MetricResult(
        "alignment", mean_spread, _decay(excess, 2.54),
        _WEIGHTS["alignment"],
        f"{len(spreads)} run(s), mean cross-spread {mean_spread:.2f}mm",
    )


def _metric_spacing(plan: LayoutPlan, pos: dict[str, Pt]) -> MetricResult:
    """Pitch evenness within each cap bank — the coefficient of variation of
    the gaps between adjacent caps. 0 = perfectly even."""
    cvs: list[float] = []
    detail_bits: list[str] = []
    for role, label in ((Role.INPUT_CAP, "input"), (Role.OUTPUT_CAP, "output")):
        refs = [r for r in plan.with_role(role) if r in pos]
        if len(refs) < 3:
            continue
        pts = [pos[r] for r in refs]
        spread_x = max(p[0] for p in pts) - min(p[0] for p in pts)
        spread_y = max(p[1] for p in pts) - min(p[1] for p in pts)
        axis = 0 if spread_x >= spread_y else 1
        coords = sorted(p[axis] for p in pts)
        pitches = [b - a for a, b in zip(coords, coords[1:])]
        mean = statistics.mean(pitches)
        if mean < _EPS:
            continue
        cv = statistics.pstdev(pitches) / mean
        cvs.append(cv)
        detail_bits.append(f"{label} cv={cv:.2f}")
    if not cvs:
        return MetricResult("spacing", 0.0, None, _WEIGHTS["spacing"],
                            "no bank of 3+ caps")
    mean_cv = statistics.mean(cvs)
    return MetricResult(
        "spacing", mean_cv, max(0.0, 1.0 - mean_cv), _WEIGHTS["spacing"],
        "; ".join(detail_bits),
    )


def _detect_chains(netlist: Netlist, plan: LayoutPlan) -> list[list[str]]:
    """Find part chains that logically form a single layout unit. A chain
    is a sequence of 2-pin parts connected through internal-signal nets
    (no other parts on those nets), with rail / ground / IC endpoints.
    Mirrors `layout_tree`'s SHUNT_BRANCH + DIVIDER_STACK detection but
    works from netlist + plan alone (no tree needed)."""
    # net_name → list[refdes]
    net_members: dict[str, list[str]] = {}
    for net in netlist.nets:
        for ep in net.endpoints:
            net_members.setdefault(net.name, []).append(ep.ref)

    chains: list[list[str]] = []
    # Dividers (high + low resistors)
    for d in plan.dividers:
        chains.append([d.high_refdes, d.low_refdes])

    # Shunt chains: rail → internal → … → GND, no IC contact
    seen: set[str] = {r for chain in chains for r in chain}
    anchors = set(plan.ics)
    for seed in sorted(plan.parts):
        if seed in seen or seed in anchors:
            continue
        pc = plan.parts[seed]
        if len(pc.nets) != 2:
            continue
        # Walk: build connected component over internal-signal nets.
        visited: set[str] = set()
        frontier = [seed]
        chain: list[str] = []
        rail_ends = 0
        ground_ends = 0
        valid = True
        while frontier:
            cur = frontier.pop()
            if cur in visited or cur in anchors:
                continue
            visited.add(cur)
            pc_cur = plan.parts.get(cur)
            if pc_cur is None or len(pc_cur.nets) != 2:
                valid = False
                break
            chain.append(cur)
            for net_name in pc_cur.nets:
                nc = plan.nets.get(net_name)
                if nc is None:
                    valid = False
                    break
                if nc.kind == NetKind.GROUND:
                    ground_ends += 1
                    continue
                if nc.kind == NetKind.RAIL:
                    rail_ends += 1
                    continue
                # Internal — must not touch IC, must walk to next link.
                if any(c.ic_refdes in anchors for c in nc.ic_contacts):
                    valid = False
                    break
                for other in net_members.get(net_name, []):
                    if other != cur and other not in visited:
                        frontier.append(other)
            if not valid:
                break
        if valid and len(chain) >= 2 and rail_ends == 1 and ground_ends == 1:
            chains.append(sorted(chain))
            seen.update(chain)
    return chains


def _metric_chain_coherence(plan: LayoutPlan, netlist: Netlist,
                             pos: dict[str, Pt]) -> MetricResult:
    """For each detected chain (divider, shunt branch), members should
    share an axis — a vertical column (low X-spread) or a horizontal
    run (low Y-spread). A chain whose members scatter across both axes
    is a topology-rendering defect: the schematic reader can't tell that
    those parts form a single logical unit."""
    chains = _detect_chains(netlist, plan)
    if not chains:
        return MetricResult("chain_coherence", 0.0, None,
                            _WEIGHTS["chain_coherence"],
                            "no chains in this circuit")
    incoherent = 0
    worst: list[str] = []
    for chain in chains:
        xs = [pos[r][0] for r in chain if r in pos]
        ys = [pos[r][1] for r in chain if r in pos]
        if len(xs) < 2:
            continue
        x_spread = max(xs) - min(xs)
        y_spread = max(ys) - min(ys)
        # Coherent if either axis spread is within one grid step (parts
        # are essentially aligned on a row or column).
        if min(x_spread, y_spread) > _ALIGN_SLACK:
            incoherent += 1
            worst.append("/".join(chain))
    return MetricResult(
        "chain_coherence", float(incoherent),
        _decay(incoherent, 1.0),
        _WEIGHTS["chain_coherence"],
        (f"{incoherent} scattered chain(s): {', '.join(worst[:3])}"
         if incoherent else f"{len(chains)} chain(s) all coherent"),
    )


def _metric_rail_proximity(netlist: Netlist, plan: LayoutPlan,
                            pos: dict[str, Pt],
                            text_boxes: list[tuple[str, BBox]]) -> MetricResult:
    """For every non-IC part that touches a power rail, the part should
    sit near the rail's wire trunk. A part that touches `+5V` but lands
    20 mm away from the rail's other +5V endpoints forces a long detour
    wire — a sign the part was misplaced. Measured as the median
    distance from each rail-touching part to the centroid of the rail's
    OTHER endpoints; large = misplacement smell."""
    # Per-rail collect positions of all endpoint parts (excluding the IC).
    by_rail: dict[str, list[Pt]] = {}
    ic_refs = set(plan.ics)
    for net in netlist.nets:
        nc = plan.nets.get(net.name)
        if nc is None or nc.kind != NetKind.RAIL:
            continue
        coords = []
        for ep in net.endpoints:
            if ep.ref in ic_refs:
                continue
            if ep.ref in pos:
                coords.append(pos[ep.ref])
        if len(coords) >= 2:
            by_rail[net.name] = coords

    if not by_rail:
        return MetricResult("rail_proximity", 0.0, None,
                            _WEIGHTS["rail_proximity"],
                            "no rail-touching non-IC parts")

    far_count = 0
    detail: list[str] = []
    for rail, coords in by_rail.items():
        # Centroid of non-IC endpoints on this rail.
        cx = statistics.mean(p[0] for p in coords)
        cy = statistics.mean(p[1] for p in coords)
        for (x, y) in coords:
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            # A spread of > 50 mm from the centroid is a clear outlier.
            if d > 50.0:
                far_count += 1
                detail.append(f"{rail}: {d:.0f}mm")
    return MetricResult(
        "rail_proximity", float(far_count),
        _decay(far_count, 2.0),
        _WEIGHTS["rail_proximity"],
        (f"{far_count} rail outlier(s): {', '.join(detail[:3])}"
         if far_count else f"{len(by_rail)} rail(s) clustered"),
    )


def _metric_flow(plan: LayoutPlan, pos: dict[str, Pt]) -> MetricResult:
    """Does the layout read left→right — input parts, then the IC, then output
    parts — by mean X? Scores the fraction of the orderings that hold."""
    def _mean_x(refs: list[str]) -> float | None:
        xs = [pos[r][0] for r in refs if r in pos]
        return statistics.mean(xs) if xs else None

    inp = _mean_x(plan.with_role(Role.INPUT_CAP))
    out = _mean_x(plan.with_role(Role.OUTPUT_CAP)
                  + plan.with_role(Role.DIVIDER_RESISTOR))
    ic = _mean_x(plan.ics)
    checks: list[bool] = []
    if inp is not None and ic is not None:
        checks.append(inp < ic)
    if ic is not None and out is not None:
        checks.append(ic < out)
    if inp is not None and out is not None:
        checks.append(inp < out)
    if not checks:
        return MetricResult("flow", 0.0, None, _WEIGHTS["flow"],
                            "input/output sides not identifiable")
    frac = sum(checks) / len(checks)
    return MetricResult(
        "flow", frac, frac, _WEIGHTS["flow"],
        f"{sum(checks)}/{len(checks)} left→right ordering(s) hold",
    )


# --- orchestration -----------------------------------------------------------

def score(
    sch_text: str,
    netlist: Netlist | None = None,
    *,
    check_connectivity: bool = True,
) -> RubricScore:
    """Score one schematic against the layout quality rubric.

    `sch_text` is a full `(kicad_sch ...)` document. `netlist` is the netlist
    whose schematic this is — required for the connectivity gate and the
    group-aware metrics (`alignment`, `spacing`, `flow`); without it those are
    reported as not-evaluable and excluded from the aggregate.

    `check_connectivity=False` skips the kicad-cli netlist export (the slow
    part) — for callers that verify connectivity separately, e.g. a solver
    using the rubric as an inner-loop objective.
    """
    notes: list[str] = []
    try:
        sch = _load(sch_text)
    except Exception as e:  # noqa: BLE001
        return RubricScore(
            gates={"connectivity": None, "symbol_overlap": None},
            metrics=[], total=0.0, passed=False,
            notes=[f"could not load schematic: {type(e).__name__}: {e}"],
        )

    by_ref = _components_by_ref(sch)
    segs = _wire_segments(sch)
    body_boxes = _body_boxes(by_ref)
    text_boxes = _text_boxes(sch, by_ref)
    pos = _pos_by_ref(by_ref)

    # --- gates ---------------------------------------------------------------
    overlap_ok, overlap_n = _gate_symbol_overlap(body_boxes)
    conn: bool | None = None
    if check_connectivity:
        try:
            conn = _gate_connectivity(sch_text, netlist)
        except Exception as e:  # noqa: BLE001
            notes.append(f"connectivity gate errored: {type(e).__name__}: {e}")
            conn = None
        if conn is None and netlist is not None:
            notes.append("connectivity not verified — kicad-cli unavailable")
    gates: dict[str, bool | None] = {
        "connectivity": conn,
        "symbol_overlap": overlap_ok,
    }
    if not overlap_ok:
        notes.append(f"{overlap_n} non-IC symbol-body overlap(s)")

    # --- metrics -------------------------------------------------------------
    metrics: list[MetricResult] = [
        _metric_label_collision(text_boxes),
        _metric_wire_crossings(segs),
        _metric_wire_through_part(segs, body_boxes),
        _metric_wire_orthogonality(segs),
        _metric_alignment(pos),
        _metric_off_grid(by_ref, segs),
        _metric_compactness(body_boxes, segs),
    ]

    if netlist is not None:
        try:
            plan = classify(netlist)
            metrics += [
                _metric_spacing(plan, pos),
                _metric_flow(plan, pos),
                _metric_chain_coherence(plan, netlist, pos),
                _metric_rail_proximity(netlist, plan, pos, text_boxes),
            ]
        except Exception as e:  # noqa: BLE001
            notes.append(f"classify failed — group metrics skipped: "
                         f"{type(e).__name__}: {e}")
    else:
        notes.append("no netlist — alignment/spacing/flow metrics skipped")

    # --- aggregate -----------------------------------------------------------
    scored = [m for m in metrics if m.score is not None]
    wsum = sum(m.weight for m in scored)
    total = (sum(m.score * m.weight for m in scored) / wsum) if wsum else 0.0

    # Each failed gate multiplies total by `_GATE_PENALTY` — a broken
    # connectivity / overlapping symbols can't score in the 0.9s on the back
    # of clean ancillary geometry. The `total` field is what eval scripts /
    # solver objectives read, so the penalty has to live here, not in `passed`.
    fails = sum(1 for g in gates.values() if g is False)
    if fails:
        total *= _GATE_PENALTY ** fails
        notes.append(f"gate penalty: {fails} fail(s) × {_GATE_PENALTY}^n")

    passed = all(g is not False for g in gates.values())
    # Order metrics by descending weight so the report leads with what matters.
    metrics.sort(key=lambda m: (-m.weight, m.name))
    return RubricScore(
        gates=gates, metrics=metrics, total=round(total, 4),
        passed=passed, notes=notes,
    )
