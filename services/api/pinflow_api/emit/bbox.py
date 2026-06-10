"""Symbol/component extent helper for non-overlapping placement.

The placer and the merge layer both need to answer "how much room does this
part take?" so parts can be packed side by side without colliding.

The honest finding (audited against kicad-sch-api 0.5.6): the *symbol
definition's* precomputed `.bounding_box` is unreliable for exactly the parts
we pack most — `Device:R/C/L` report a zero-width box (pin endpoints only),
and symbols not on the cache's discovered library path report `None`. What
*is* reliable is measuring a component **after it has been placed** via
`core.component_bounds.get_component_bounding_box(..., include_properties=
False)`, which returns a real, tight box of symbol body + pins + pin names
(a resistor measures ~4.3 x 11.9 mm) and applies rotation.

What is *not* reliable is that helper's `include_properties=True` mode: it
tacks on a flat, symbol-blind `+20.08 mm` of height and a `10 mm` minimum
width to *every* component — independent of how short the Reference/Value
text actually is. That over-estimate is invisible on a big IC but dominates a
passive (a 4x12 mm resistor packs as a 10x32 mm cell), so a grid of passives
ends up mostly dead space. We therefore measure geometry only and union it
with the Reference/Value text at the field's *actual* placed position +
font + justify (`_field_box`, via `get_property_effects`) — the exact box
KiCad will draw — then grow the union by a flat `_CLEARANCE` (0.1 mm) on
every side. No role/stack/run heuristic: the box hugs the symbol and its
own labels and nothing more.

Earlier revisions guessed the field block side-agnostically (a centred
half-run + a two-line stack padded onto all four sides), which roughly
doubled every passive on both axes (a ~4x12 mm part packed ~12x20 mm) for
slop KiCad never actually draws. Reading the field's real `(at …)` removes
the guess entirely; the only assumption left is a stock-font glyph advance
(`_GLYPH_ADV`, ~0.72x text height) to size the text run.

So this module's primary path is `measured_bbox` / `union_bbox` (post-place
measurement + our text allowance). `estimate_extent` is a deliberately crude
role-by-refdes fallback used only when measurement is unavailable (a part
failed to add, or the library geometry couldn't be resolved). It is a packing
estimate, not geometry — keep it conservative (over-estimate) so the fallback
still avoids overlap.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from kicad_sch_api.core.component_bounds import get_component_bounding_box

# (min_x, min_y, max_x, max_y) in mm.
BBox = tuple[float, float, float, float]

# Crude role-keyed extents (w, h) in mm — fallback only. Over-estimates on
# purpose: a too-big fallback box wastes page space; a too-small one collides.
_PASSIVE = (10.0, 14.0)   # R/C/L/D, properties included
_CONNECTOR = (12.0, 24.0)
_POWER = (6.0, 8.0)       # power:* net-anchor symbols
_IC = (30.0, 30.0)        # generic IC guess; real ICs almost always measure


def _role_for_refdes(refdes: str) -> str:
    """'U1'->ic, 'J2'->connector, '#PWR03'->power, 'R5'->passive."""
    if refdes.startswith("#PWR") or refdes.startswith("#FLG"):
        return "power"
    head = refdes[:1].upper()
    if head == "U":
        return "ic"
    if head == "J":
        return "connector"
    return "passive"


def estimate_extent(refdes: str = "", lib_id: str = "") -> tuple[float, float]:
    """Conservative (w, h) fallback in mm when a part can't be measured.

    Keyed off the refdes prefix (the same signal `_bucket_parts` uses); the
    lib_id is a secondary hint for power symbols whose refdes we don't know.
    """
    if lib_id.startswith("power:"):
        return _POWER
    role = _role_for_refdes(refdes)
    return {
        "ic": _IC,
        "connector": _CONNECTOR,
        "power": _POWER,
        "passive": _PASSIVE,
    }[role]


# A stock-font glyph (incl. inter-char advance) is ~0.72x its height.
_GLYPH_ADV = 0.72
# The box hugs the symbol + its artifacts (pins, Reference/Value text) with
# exactly this much breathing room on every side — no role/stack heuristic.
_CLEARANCE = 0.1


def _field_box(comp, name: str) -> Optional[BBox]:
    """Real on-page bbox (mm) of one visible Reference/Value field, or None.

    Uses the field's *actual* placed `(at x y rot)` + font size + justify
    (`get_property_effects`) — i.e. exactly where KiCad put the text, not a
    side-agnostic guess. Hidden/empty fields contribute nothing.
    """
    try:
        eff = comp.get_property_effects(name)
        raw = comp.get_property(name)
    except Exception:
        return None
    # get_property returns a {name,value,at,effects,...} dict on loaded
    # components but a bare string on freshly-added ones — want the value.
    text = str(
        raw.get("value", "") if isinstance(raw, dict) else (raw or "")
    )
    if not text or not eff.get("visible", True):
        return None
    px, py = eff["position"]
    fx, fy = eff.get("font_size") or (1.27, 1.27)
    w = len(text) * fx * _GLYPH_ADV
    h = fy
    if int(round(eff.get("rotation", 0) or 0)) % 180 == 90:
        w, h = h, w  # field rotated onto the vertical axis
    jh, jv = eff.get("justify_h"), eff.get("justify_v")
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


def measured_bbox(comp) -> Optional[BBox]:
    """Real placed-component extent (symbol + pins + visible fields), or None.

    Symbol geometry is measured with `include_properties=False` (the reliable
    path — the helper's property mode adds a fixed 20 mm of slop, see module
    docs); the Reference/Value text is added back from its *actual* placed
    position via `_field_box`. The result is the tight union of all of that,
    grown by exactly `_CLEARANCE` (0.1 mm) on every side.
    """
    try:
        bb = get_component_bounding_box(comp, include_properties=False)
    except Exception:
        return None
    try:
        min_x, min_y, max_x, max_y = bb.min_x, bb.min_y, bb.max_x, bb.max_y
    except AttributeError:
        return None
    for name in ("Reference", "Value"):
        fb = _field_box(comp, name)
        if fb is not None:
            min_x, min_y = min(min_x, fb[0]), min(min_y, fb[1])
            max_x, max_y = max(max_x, fb[2]), max(max_y, fb[3])
    return (
        min_x - _CLEARANCE, min_y - _CLEARANCE,
        max_x + _CLEARANCE, max_y + _CLEARANCE,
    )


def text_extent(
    text: str,
    position: tuple[float, float],
    size: float = 1.27,
    rotation: float = 0.0,
    justify_h: Optional[str] = None,
    justify_v: Optional[str] = None,
) -> Optional[BBox]:
    """Tight on-page bbox (mm) for a free-standing label or text element.

    `size` is KiCad's font height (the single value labels/texts each expose
    as `.size`). Per-line width is `len * size * _GLYPH_ADV`; height is
    `line_count * size`. Multi-line strings are split on `\\n`. Rotation
    90/270 swaps width/height. `justify_*` is center-anchored when None,
    which matches `add_text` / `add_label` output when no `(effects ...)`
    block is emitted — over-estimates by ~half a glyph run on each side
    for a true left-justified label, which is the safe direction for
    packing.
    """
    if not text:
        return None
    lines = text.split("\n")
    w = max((len(line) for line in lines), default=0) * size * _GLYPH_ADV
    h = max(1, len(lines)) * size
    if int(round(rotation or 0)) % 180 == 90:
        w, h = h, w
    px, py = position
    if justify_h == "left":
        x0, x1 = px, px + w
    elif justify_h == "right":
        x0, x1 = px - w, px
    else:
        x0, x1 = px - w / 2, px + w / 2
    if justify_v == "top":
        y0, y1 = py, py + h
    elif justify_v == "bottom":
        y0, y1 = py - h, py
    else:
        y0, y1 = py - h / 2, py + h / 2
    return (x0, y0, x1, y1)


def union_bbox(comps: Iterable) -> Optional[BBox]:
    """Union of measured bboxes over a group (e.g. a multi-unit symbol's units).

    Returns None if no member could be measured.
    """
    boxes = [b for b in (measured_bbox(c) for c in comps) if b is not None]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _strip_lib_symbols(text: str) -> str:
    """Drop the `(lib_symbols ...)` block so its symbol-local geometry
    (coords near 0,0 — pin endpoints, glyph outlines) doesn't drag a
    page-coordinate scan toward the origin. Balanced-paren removal.
    """
    i = text.find("(lib_symbols")
    if i < 0:
        return text
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[:i] + text[j + 1:]
    return text[:i]


# Page-coordinate carriers in a placed schematic: symbol/label/text `(at x y
# [rot])`, wire/poly vertices `(xy x y)`, graphic `(start|end|mid x y)`.
_COORD = re.compile(
    r"\((?:at|xy|start|end|mid)\s+"
    r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"
)


def content_bbox(sch, sch_text: str) -> Optional[BBox]:
    """Tight bounding box (mm) of everything drawn on the page.

    Unions two sources: measured component extents (`union_bbox` — true
    symbol body + pins + text, the part raw coords undercount) and a raw
    scan of every page-coordinate token in `sch_text` with `lib_symbols`
    stripped (catches wires, labels, power flags, the drawn frame rectangle
    — things `get_component_bounding_box` doesn't see). Returns None only if
    the schematic is empty.
    """
    boxes: list[BBox] = []
    cb = union_bbox(getattr(sch, "components", []))
    if cb is not None:
        boxes.append(cb)
    for mx, my in _COORD.findall(_strip_lib_symbols(sch_text)):
        x, y = float(mx), float(my)
        boxes.append((x, y, x, y))
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
