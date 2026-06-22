"""Wrap-around block placement + labeled rectangle frame for agent edits.

Used by `add_subcircuit_from_netlist` to merge a freshly placed subcircuit
into the user's existing schematic without overlapping prior content.

Layout policy:
- First block anchors at (MARGIN, MARGIN).
- Subsequent blocks anchor immediately right of the existing content's right
  edge, top-aligned with the existing top.
- If the new block would cross the right page margin, it wraps to a new row
  below the existing bottom edge.
- If no row fits within the current sheet's height, the sheet grows up the
  standard ladder (A4 → A3 → … → A0) so blocks stay on-page instead of
  spilling off the bottom edge. The sheet never shrinks.
- Each placed block gets a labeled rectangle drawn around it.

All measurements in mm. Snapped to KiCad's 2.54 mm grid for component
placement. Starts from the schematic's own paper size (A4 landscape if unset)
and grows it as needed.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from pathlib import Path

import kicad_sch_api as ksa
from kicad_sch_api.core.types import Point

from pinflow_api.emit import bbox

PAGE_W = 297.0     # A4 landscape, mm
PAGE_H = 210.0
MARGIN = 10.0      # KiCad's default drawing-sheet border inset from page edge
BUFFER = 5.0       # gap between that border and a block's frame, so they don't touch
PAD = 5.0          # gutter between blocks
FRAME_PAD = 3.0    # extra space between contents and frame rectangle
TITLE_BAND = 4.0   # extra height reserved inside the frame top for the label
COMP_HALF = 10.0   # fallback half-extent when a component can't be measured
GRID = 2.54        # KiCad standard grid

# How far `draw_frame`'s rectangle overhangs the block content: FRAME_PAD on
# the left/right/bottom, FRAME_PAD + TITLE_BAND on top (the title band). The
# frame is drawn after placement, so placement must reserve this room to keep
# the frame — not just the content — inside the MARGIN border.
_FRAME_LEFT = FRAME_PAD
_FRAME_TOP = FRAME_PAD + TITLE_BAND
_FRAME_RIGHT = FRAME_PAD
_FRAME_BOTTOM = FRAME_PAD

# KiCad standard sheet sizes, landscape orientation: (name, width, height) mm,
# smallest first. When a new block won't fit the current sheet, placement grows
# UP this ladder (never shrinks) instead of letting the block run off the edge.
_PAPER_LADDER = [
    ("A4", 297.0, 210.0),
    ("A3", 420.0, 297.0),
    ("A2", 594.0, 420.0),
    ("A1", 841.0, 594.0),
    ("A0", 1189.0, 841.0),
]
_PAPER_DIMS = {name: (w, h) for name, w, h in _PAPER_LADDER}


def _page_dims(paper: str) -> tuple[float, float]:
    """(width, height) mm for a paper name; A4 landscape for anything unknown
    (custom/User sizes fall back so the grow ladder still starts sane)."""
    return _PAPER_DIMS.get(paper, (PAGE_W, PAGE_H))


def _read_paper(sch: ksa.Schematic) -> str:
    paper = sch._data.get("paper")
    return paper if isinstance(paper, str) and paper else "A4"


def _set_paper(sch: ksa.Schematic, paper: str) -> None:
    sch._data["paper"] = paper


@dataclass
class BBox:
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @property
    def w(self) -> float:
        return self.xmax - self.xmin

    @property
    def h(self) -> float:
        return self.ymax - self.ymin


def _snap_up(v: float) -> float:
    """Grid-snap toward +∞. Used for placement translations so a block lands
    *at least* its target offset from the page border / existing content —
    nearest-rounding could pull it ~half a grid back into the buffer it's
    meant to clear (measured bbox edges aren't grid-aligned)."""
    return math.ceil(v / GRID) * GRID


def compute_bbox(
    sch: ksa.Schematic, *, include_rectangles: bool = True
) -> BBox | None:
    """Bounding box over all visible elements; None if the schematic is empty.

    Component extents come from `emit.bbox.measured_bbox` (symbol + pins +
    a text-length-aware Reference/Value allowance). Labels and free-text
    elements contribute their estimated drawn extent (font × glyph advance,
    multi-line aware) via `emit.bbox.text_extent` — anchoring on just the
    position point left wide annotations like a multi-line "5V Step-UP …"
    sticker invisible to packing, so the next block would land on top of
    them. Frames drawn by prior `draw_frame` calls live in `sch._data
    ["rectangles"]` and are included here so subsequent blocks pack past
    them, not through them. Only when a component can't be measured
    (geometry unresolved) do we fall back to the old ±COMP_HALF
    approximation around its centre.

    `include_rectangles=False` excludes those drawn frames. A frame sits
    `FRAME_PAD + TITLE_BAND` *above* its block's content, so the full bbox's
    top edge is a frame, not real content. Top-aligning a new block to that
    edge (then drawing its own frame another band higher) makes every block
    creep up ~7 mm from the last — the frame-exclusive top is the stable
    anchor that kills the staircase. Horizontal packing still uses the full
    bbox so blocks don't overlap each other's frames.
    """
    xs: list[float] = []
    ys: list[float] = []

    def _absorb(box: tuple[float, float, float, float] | None) -> None:
        if box is None:
            return
        x0, y0, x1, y1 = box
        xs.extend([x0, x1])
        ys.extend([y0, y1])

    for comp in sch.components:
        box = bbox.measured_bbox(comp)
        if box is not None:
            _absorb(box)
        else:
            cx, cy = comp.position.x, comp.position.y
            _absorb((cx - COMP_HALF, cy - COMP_HALF,
                     cx + COMP_HALF, cy + COMP_HALF))
    for wire in sch.wires:
        for p in wire.points:
            xs.append(p.x)
            ys.append(p.y)
    for lab in sch.labels:
        _absorb(bbox.text_extent(
            lab.text,
            (lab.position.x, lab.position.y),
            size=getattr(lab, "size", 1.27),
            rotation=getattr(lab, "rotation", 0.0) or 0.0,
        ))
    for txt in sch.texts:
        _absorb(bbox.text_extent(
            txt.text,
            (txt.position.x, txt.position.y),
            size=getattr(txt, "size", 1.27),
            rotation=getattr(txt, "rotation", 0.0) or 0.0,
        ))
    for j in sch.junctions:
        xs.append(j.position.x)
        ys.append(j.position.y)
    # `add_rectangle` stores into `_data["rectangles"]`; there's no public
    # accessor on `ksa.Schematic` (the `list_all_graphics` shortcut reads
    # the singular key and misses additions — known ksa quirk).
    for rect in (sch._data.get("rectangles", []) or []) if include_rectangles else []:
        start = rect.get("start") or {}
        end = rect.get("end") or {}
        try:
            x0 = float(start["x"]); y0 = float(start["y"])
            x1 = float(end["x"]);   y1 = float(end["y"])
        except (KeyError, TypeError, ValueError):
            continue
        xs.extend([x0, x1])
        ys.extend([y0, y1])

    if not xs:
        return None
    return BBox(min(xs), min(ys), max(xs), max(ys))


def compute_placement(
    existing: BBox | None,
    new: BBox,
    page_w: float = PAGE_W,
    *,
    existing_top: float | None = None,
) -> tuple[float, float, bool]:
    """Return (dx, dy, wrapped) — translation to apply to the new block.

    Packs the new block to the right of existing content, wrapping to a new
    row below it when that would cross the right margin of a `page_w`-wide
    sheet. The *height* bound is enforced by the caller (`_fit_page`), which
    grows the sheet when no row fits — so a block never silently runs off the
    bottom edge.

    `existing_top` is the existing content's top edge measured WITHOUT the
    drawn title frames (`compute_bbox(..., include_rectangles=False).ymin`).
    Right-packed blocks top-align to it instead of `existing.ymin`, because
    `existing.ymin` is a frame edge sitting `FRAME_PAD + TITLE_BAND` above real
    content — aligning to that and then drawing a fresh frame another band
    higher makes every block staircase up ~7 mm from the last (and eventually
    off the top edge). The frame-free top is stable, so blocks stay aligned.
    Falls back to `existing.ymin` (floored at the frame inset) when not given.

    Anchors reserve the block's own frame overhang inside the MARGIN border:
    content starts at `MARGIN + _FRAME_LEFT` / `MARGIN + _FRAME_TOP`, so the
    frame `draw_frame` later adds lands on the border rather than poking
    outside KiCad's drawing sheet."""
    left_anchor = MARGIN + BUFFER + _FRAME_LEFT
    top_anchor = MARGIN + BUFFER + _FRAME_TOP
    right_limit = page_w - MARGIN - BUFFER
    if existing is None:
        target_x = left_anchor
        target_y = top_anchor
        wrapped = False
    else:
        anchor_top = existing_top if existing_top is not None else existing.ymin
        # existing.xmax/ymax are the neighbors' FRAME edges (compute_bbox counts
        # drawn frames), but target_x/y position the new block's CONTENT. The
        # new block draws its own frame _FRAME_LEFT left / _FRAME_TOP above its
        # content, so add that leading overhang to keep the PAD gutter measured
        # frame-to-frame; without it the new frame pokes back into the neighbor
        # by (_FRAME − PAD) and the boxes collide.
        target_x = existing.xmax + PAD + _FRAME_LEFT
        target_y = max(top_anchor, anchor_top)
        # Wrap when the block + its frame would cross the right border buffer.
        if target_x + new.w + _FRAME_RIGHT > right_limit:
            target_x = left_anchor
            target_y = existing.ymax + PAD + _FRAME_TOP
            wrapped = True
        else:
            wrapped = False
    dx = _snap_up(target_x - new.xmin)
    dy = _snap_up(target_y - new.ymin)
    return dx, dy, wrapped


def _fit_page(
    existing: BBox | None,
    new: BBox,
    start_paper: str,
    *,
    existing_top: float | None = None,
) -> tuple[str, float, float, bool]:
    """Pick the smallest standard sheet (≥ the current one) that contains the
    new block once it's packed beside the existing content, plus the placement.

    Returns (paper_name, dx, dy, wrapped). Grows the sheet up `_PAPER_LADDER`
    rather than letting a block spill off the page; never shrinks below the
    current size. If nothing fits even at A0, places on A0 best-effort. The
    width/height fit is checked against the *union* of existing + placed, so a
    sheet that's already overflowing (from a prior off-page placement) gets
    grown enough to contain everything.

    Packing uses the **current** page width, not each candidate width, so a
    block that runs past the right margin WRAPS to a new row. Feeding the
    candidate width into `compute_placement` instead (the old behavior) widened
    the wrap threshold on every rung, so a block never wrapped — the sheet just
    ballooned sideways to keep one long row (e.g. four tall blocks → an A1 sheet
    with content crammed into a top strip). Wrap first, then grow only as far as
    the wrapped layout needs."""
    start_w, start_h = _page_dims(start_paper)
    ladder = [(n, w, h) for n, w, h in _PAPER_LADDER if w >= start_w and h >= start_h]
    if not ladder:  # current sheet already ≥ A0 (or unknown-huge): keep it
        ladder = [(start_paper, start_w, start_h)]

    dx, dy, wrapped = compute_placement(
        existing, new, start_w, existing_top=existing_top
    )
    # Reserve the new block's frame overhang on the right/bottom too, so the
    # frame stays inside the MARGIN border (existing.xmax/ymax already include
    # prior blocks' drawn frames).
    placed_xmax = new.xmax + dx + _FRAME_RIGHT
    placed_ymax = new.ymax + dy + _FRAME_BOTTOM
    union_xmax = max(existing.xmax, placed_xmax) if existing else placed_xmax
    union_ymax = max(existing.ymax, placed_ymax) if existing else placed_ymax
    for name, pw, ph in ladder:
        if union_xmax <= pw - MARGIN - BUFFER and union_ymax <= ph - MARGIN - BUFFER:
            return name, dx, dy, wrapped

    name, pw, ph = ladder[-1]
    return name, dx, dy, wrapped


def translate_schematic(sch: ksa.Schematic, dx: float, dy: float) -> None:
    """Apply (dx, dy) to every positioned element in `sch`."""
    for comp in sch.components:
        comp.translate(dx, dy)
    for wire in sch.wires:
        wire.points = [Point(p.x + dx, p.y + dy) for p in wire.points]
    for lab in sch.labels:
        lab.position = (lab.position.x + dx, lab.position.y + dy)
    for txt in sch.texts:
        txt.position = (txt.position.x + dx, txt.position.y + dy)
    for j in sch.junctions:
        j.position = Point(j.position.x + dx, j.position.y + dy)


def draw_frame(target_sch: ksa.Schematic, bbox: BBox, label: str) -> None:
    """Draw a labeled rectangle around `bbox` on `target_sch`.

    The frame's top edge is lifted by an extra TITLE_BAND so the label can
    sit *inside* the rectangle (in its own band above the contents) rather
    than floating above the frame where the auto-crop would clip it.
    """
    top = bbox.ymin - FRAME_PAD - TITLE_BAND
    target_sch.add_rectangle(
        start=(bbox.xmin - FRAME_PAD, top),
        end=(bbox.xmax + FRAME_PAD, bbox.ymax + FRAME_PAD),
        stroke_width=0.3,
    )
    # ksa's add_text emits no `justify`, so KiCad renders the text
    # center-anchored — anchor on the frame's horizontal midpoint so the
    # title sits centered inside the title band (any label length).
    mid_x = (bbox.xmin + bbox.xmax) / 2
    target_sch.add_text(
        label,
        position=(mid_x, top + TITLE_BAND / 2),
        size=1.5,
        bold=True,
    )


def _ref_prefix(ref: str) -> str:
    """Alphabetic prefix of a reference — 'U1' -> 'U', '#PWR012' -> '#PWR'."""
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref


def _next_free_ref(
    target_sch: ksa.Schematic, prefix: str, taken: set[str] = frozenset()
) -> str:
    """Lowest `<prefix><n>` not already in `target_sch` nor in `taken`.

    `taken` lets callers reserve references that aren't in the schematic yet
    — e.g. source refdes a merge will copy verbatim, or refs minted earlier
    in the same merge. Without it, a rename can mint a ref that a later
    unprocessed source part also claims, causing a collision on add."""
    nums: list[int] = []
    for comp in target_sch.components:
        ref = comp.reference
        if ref.startswith(prefix):
            tail = ref[len(prefix):]
            if tail.isdigit():
                nums.append(int(tail))
    nxt = (max(nums) + 1) if nums else 1
    while f"{prefix}{nxt}" in taken:
        nxt += 1
    return f"{prefix}{nxt}"


_INTRINSIC_PROPS = {"Reference", "Value", "Footprint"}


def _copy_components(src: ksa.Schematic, dst: ksa.Schematic) -> None:
    """Copy each component from src into dst, preserving multi-unit symbols.

    Multi-unit symbols (e.g. RP2040 has 2 units) appear in `src.components`
    as multiple Component objects with the same reference but different
    `_data.unit` values. We pass `unit=` through to `dst.components.add` so
    each unit registers as a sibling of the same logical symbol — not as
    separate refdes. Collision renames happen at most once per source ref
    (the first unit encounter); subsequent units reuse that mapping.
    """
    pre_existing_refs = {c.reference for c in dst.components}
    # Reserve every source refdes up front: a non-colliding src ref is kept
    # verbatim, so a rename of a *different* colliding ref must not mint it.
    # `taken` grows as we mint, so two renames can't land on the same ref.
    taken: set[str] = set(pre_existing_refs) | {c.reference for c in src.components}
    ref_map: dict[str, str] = {}  # src_ref -> dst_ref (after collision rename)

    for comp in src.components:
        src_ref = comp.reference
        if src_ref not in ref_map:
            if src_ref in pre_existing_refs:
                new = _next_free_ref(dst, _ref_prefix(src_ref), taken)
                taken.add(new)
                ref_map[src_ref] = new
            else:
                ref_map[src_ref] = src_ref
        new_ref = ref_map[src_ref]
        unit = getattr(comp._data, "unit", 1)

        new_comp = dst.components.add(
            lib_id=comp.lib_id,
            reference=new_ref,
            value=comp.value,
            position=(comp.position.x, comp.position.y),
            footprint=comp.footprint,
            rotation=comp.rotation,
            unit=unit,
        )
        for name, raw in (comp.properties or {}).items():
            if name in _INTRINSIC_PROPS or str(name).startswith("__sexp_"):
                continue
            value = raw["value"] if isinstance(raw, dict) and "value" in raw else raw
            if value:
                new_comp.set_property(str(name), str(value))


def _copy_wires(src: ksa.Schematic, dst: ksa.Schematic) -> None:
    for wire in src.wires:
        pts = list(wire.points)
        for i in range(len(pts) - 1):
            dst.add_wire(
                start=(pts[i].x, pts[i].y),
                end=(pts[i + 1].x, pts[i + 1].y),
            )


def _copy_labels(src: ksa.Schematic, dst: ksa.Schematic) -> None:
    for lab in src.labels:
        dst.add_label(
            text=lab.text,
            position=(lab.position.x, lab.position.y),
            rotation=lab.rotation,
        )


def _copy_texts(src: ksa.Schematic, dst: ksa.Schematic) -> None:
    for txt in src.texts:
        dst.add_text(
            text=txt.text,
            position=(txt.position.x, txt.position.y),
            rotation=txt.rotation,
        )


def _copy_junctions(src: ksa.Schematic, dst: ksa.Schematic) -> None:
    for j in src.junctions:
        dst.junctions.add(position=(j.position.x, j.position.y))


def merge_subcircuit(
    target_sch: ksa.Schematic,
    new_sch_text: str,
    label: str,
) -> dict:
    """Place + merge `new_sch_text` into `target_sch`.

    Returns `{"dx", "dy", "wrapped"}` describing the placement applied.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        tmp = Path(f.name)
        f.write(new_sch_text)
    try:
        new_sch = ksa.load_schematic(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)

    existing_bbox = compute_bbox(target_sch)
    # Frame-free top for vertical alignment: the prior blocks' title frames
    # sit a band above their content, so aligning to them staircases each new
    # block upward (see `compute_placement`/`compute_bbox`).
    existing_content = compute_bbox(target_sch, include_rectangles=False)
    existing_top = existing_content.ymin if existing_content else None
    new_bbox = compute_bbox(new_sch)
    if new_bbox is None:
        return {"dx": 0.0, "dy": 0.0, "wrapped": False, "skipped": True}

    start_paper = _read_paper(target_sch)
    paper, dx, dy, wrapped = _fit_page(
        existing_bbox, new_bbox, start_paper, existing_top=existing_top
    )
    if paper != start_paper:
        _set_paper(target_sch, paper)
    translate_schematic(new_sch, dx, dy)

    placed_bbox = BBox(
        new_bbox.xmin + dx, new_bbox.ymin + dy,
        new_bbox.xmax + dx, new_bbox.ymax + dy,
    )

    _copy_components(new_sch, target_sch)
    _copy_wires(new_sch, target_sch)
    _copy_labels(new_sch, target_sch)
    _copy_texts(new_sch, target_sch)
    _copy_junctions(new_sch, target_sch)

    draw_frame(target_sch, placed_bbox, label)

    return {
        "dx": dx,
        "dy": dy,
        "wrapped": wrapped,
        "paper": paper,
        "grew_page": paper != start_paper,
        # Content bbox of the placed block (page coords, as a plain tuple) so
        # the staging layer can draw a preview-only highlight around it. The
        # drawn frame extends a little past this; the highlight pads on its own.
        "block_bbox": (
            placed_bbox.xmin,
            placed_bbox.ymin,
            placed_bbox.xmax,
            placed_bbox.ymax,
        ),
    }
