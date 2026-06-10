"""Preview-only highlight overlay for staged schematic edits.

The agent stages edits into a working copy that the user reviews in the
KiCanvas viewer before committing. KiCanvas has no external API to highlight
elements, so the *only* way to make "what just changed" stand out is to bake
graphics into the schematic source it renders. `build_preview` returns a
*derived* copy of the staged text with colored rectangles drawn around new /
changed content. This copy is shown in the viewer **only** — it is never the
working copy and never committed (commit writes the clean working copy).

**Operation-driven, not diff-driven.** Highlights come from what each editing
tool *recorded that it touched*, not from diffing the staged bytes against the
on-disk file. A byte diff flags every component whose serialized form differs
from disk — which includes pre-existing parts that `resolve_parts` backfilled
with LCSC metadata, and multi-unit symbols perturbed by a ksa round-trip. None
of those are things the user meaningfully changed, yet they'd light up. Driving
off recorded touches highlights exactly the work the user is reviewing:

- **Block outline** — one rectangle per *whole new block*. Recorded at add time
  from the `placed_bbox` that `layout.merge_subcircuit` computes
  (`block_regions`).
- **Per-component outline** — one rectangle per component an edit explicitly
  touched (`changed_refs`, e.g. `edit_property` on a part) that is *not* inside
  any block region. Edits inside a pre-existing block, or to standalone parts.

Style is a thick dashed stroke in an accent color: the dash + width delineate
"new" even if a renderer ignores the per-element color, and we use no fill so
the surrounded components stay legible.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

import kicad_sch_api as ksa

from pinflow_api.builders._common import sch_to_string
from pinflow_api.emit import bbox

# (min_x, min_y, max_x, max_y) in mm — same shape as bbox.BBox.
Rect = tuple[float, float, float, float]

# Accent applied to every highlight box. Dashed + wide so the shape reads as
# "new" regardless of whether the viewer honors the per-element color.
_HL_COLOR = (46, 160, 67, 1.0)  # GitHub-add green
_HL_WIDTH = 0.6
_HL_DASH = "dash"

# Padding (mm) around the highlighted geometry so the box doesn't clip the
# content it surrounds. Block boxes get a touch more room than tight parts.
_BLOCK_PAD = 2.0
_COMP_PAD = 1.2
# Slack when testing whether a component sits inside a recorded block region:
# the region is the block's content bbox, components hug its interior, so a
# small tolerance keeps borderline parts attributed to their block.
_COVERAGE_SLACK = 4.0


def build_preview(
    working_copy: str,
    *,
    block_regions: list[Rect],
    changed_refs: Iterable[str],
) -> str:
    """Return `working_copy` with preview highlight rectangles injected.

    `block_regions` are content bboxes of whole new blocks recorded at add
    time. `changed_refs` are component references an edit explicitly touched.
    A touched component inside a block region is left to the block outline (no
    double box). Returns `working_copy` unchanged when there is nothing to
    highlight (or on any failure — the preview must never break the viewer's
    source).
    """
    refs = {str(r) for r in changed_refs}
    if not block_regions and not refs:
        return working_copy

    try:
        sch = _load(working_copy)
    except Exception:
        return working_copy

    boxes: list[tuple[Rect, float]] = [(r, _BLOCK_PAD) for r in block_regions]

    if refs:
        # Group components by reference so multi-unit symbols (same refdes,
        # several Component objects) outline as one box.
        by_ref: dict[str, list] = {}
        for comp in getattr(sch, "components", []):
            if comp.reference in refs:
                by_ref.setdefault(comp.reference, []).append(comp)
        for comps in by_ref.values():
            bb = bbox.union_bbox(comps)
            if bb is None:
                continue
            if _covered(bb, block_regions):
                continue  # the block outline already encloses this part
            boxes.append((bb, _COMP_PAD))

    if not boxes:
        return working_copy

    try:
        for rect, pad in boxes:
            _draw_highlight_box(sch, rect, pad)
        return sch_to_string(sch, preserve_lib_symbols_from=working_copy)
    except Exception:
        return working_copy


def _load(text: str) -> ksa.Schematic:
    """Parse `.kicad_sch` text into a Schematic (round-trips via tempfile —
    ksa 0.5.x loads from a path, not a string)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".kicad_sch", delete=False, encoding="utf-8"
    ) as f:
        tmp = Path(f.name)
        f.write(text)
    try:
        return ksa.load_schematic(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def _draw_highlight_box(sch: ksa.Schematic, rect: Rect, pad: float) -> None:
    x0, y0, x1, y1 = rect
    sch.add_rectangle(
        start=(x0 - pad, y0 - pad),
        end=(x1 + pad, y1 + pad),
        stroke_width=_HL_WIDTH,
        stroke_type=_HL_DASH,
        stroke_color=_HL_COLOR,
    )


def _covered(bb: Rect, regions: list[Rect]) -> bool:
    """True if `bb`'s center sits inside any region (with slack)."""
    cx = (bb[0] + bb[2]) / 2
    cy = (bb[1] + bb[3]) / 2
    for rx0, ry0, rx1, ry1 in regions:
        if (
            rx0 - _COVERAGE_SLACK <= cx <= rx1 + _COVERAGE_SLACK
            and ry0 - _COVERAGE_SLACK <= cy <= ry1 + _COVERAGE_SLACK
        ):
            return True
    return False
