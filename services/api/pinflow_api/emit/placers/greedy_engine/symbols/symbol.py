"""Symbol abstraction layer.

Wraps `kicad-sch-api`'s symbol-cache info with a clean `place(x, y, rot)` that
returns absolute pin positions and an absolute bounding box in schematic
coordinates.

Why a wrapper? kicad-sch-api 0.5.5 has a bug in its pin-position transform for
90°/270° rotations — it Y-inverts BEFORE rotating, which inverts the rotation
direction relative to what KiCad eeschema actually does. We re-implement the
transform here in the correct order.

Coordinate conventions:
  Symbol (lib) space: +X right, +Y up   (math convention)
  Schematic space:    +X right, +Y down (screen convention)
Transform: rotate in lib space (CCW, math convention), THEN negate Y.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import kicad_sch_api as ksa


@dataclass(frozen=True)
class PlacedPin:
    """A pin after its parent symbol has been placed in schematic coords."""
    number: str
    name: str
    pin_type: str           # 'passive', 'power_in', 'power_out', ...
    x: float                # absolute schematic position
    y: float
    angle: float            # absolute angle in schematic (degrees, 0=right, 90=down on screen)


@dataclass(frozen=True)
class BBox:
    """Axis-aligned bounding box in schematic coordinates."""
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> tuple[float, float]:
        return ((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    def overlaps(self, other: BBox, margin: float = 0.0) -> bool:
        return not (
            self.max_x + margin <= other.min_x
            or other.max_x + margin <= self.min_x
            or self.max_y + margin <= other.min_y
            or other.max_y + margin <= self.min_y
        )


@dataclass(frozen=True)
class PlacedSymbol:
    """A symbol after placement: pins and bbox in absolute schematic coords."""
    lib_id: str
    origin_x: float
    origin_y: float
    rotation: float
    pins: tuple[PlacedPin, ...]
    bbox: BBox              # absolute, includes pin tips

    def pin(self, number: str) -> PlacedPin:
        for p in self.pins:
            if p.number == number:
                return p
        raise KeyError(f"{self.lib_id}: no pin {number!r} (have {[p.number for p in self.pins]})")


def _rotate_then_yinv(x: float, y: float, rotation_deg: float) -> tuple[float, float]:
    """Transform a lib-space offset (math convention) into a schematic-space offset.

    KiCad's convention: rotate CCW in lib space first, THEN flip Y for screen.
    This matches what eeschema does (verified empirically against level1 wires).
    """
    if rotation_deg in (0, 0.0):
        rx, ry = x, y
    elif rotation_deg in (90, 90.0):
        rx, ry = -y, x       # CCW 90°
    elif rotation_deg in (180, 180.0):
        rx, ry = -x, -y      # CCW 180°
    elif rotation_deg in (270, 270.0):
        rx, ry = y, -x       # CCW 270°
    else:
        # General case (any angle). KiCad symbols are normally on 90° increments
        # but support this for completeness.
        rad = math.radians(rotation_deg)
        c, s = math.cos(rad), math.sin(rad)
        rx = x * c - y * s
        ry = x * s + y * c
    # Y inversion last
    return (rx, -ry)


class Symbol:
    """A library symbol: definition only, not yet placed."""

    def __init__(self, lib_id: str, ksa_info, ksa_bbox):
        self.lib_id = lib_id
        self._info = ksa_info       # ksa SchematicSymbol info
        self._bbox = ksa_bbox       # ksa BoundingBox (lib coords)

    @property
    def pin_count(self) -> int:
        return len(self._info.pins)

    @property
    def reference_prefix(self) -> str:
        return self._info.reference_prefix or ""

    def pins_lib(self) -> list[tuple[str, str, str, float, float, float]]:
        """Return pin info in lib coords. Tuples: (number, name, type, x, y, angle)."""
        result = []
        for p in self._info.pins:
            result.append((
                p.number,
                p.name,
                p.pin_type.value if hasattr(p.pin_type, "value") else str(p.pin_type),
                p.position.x,
                p.position.y,
                p.rotation,
            ))
        return result

    def place(self, x: float, y: float, rotation: float = 0.0) -> PlacedSymbol:
        """Place this symbol at (x, y) with given rotation. Returns absolute pin & bbox info."""
        placed_pins = []
        for p in self._info.pins:
            dx, dy = _rotate_then_yinv(p.position.x, p.position.y, rotation)
            placed_pins.append(PlacedPin(
                number=p.number,
                name=p.name,
                pin_type=p.pin_type.value if hasattr(p.pin_type, "value") else str(p.pin_type),
                x=x + dx,
                y=y + dy,
                angle=(p.rotation + rotation) % 360,
            ))

        # Bbox: take corners of lib-coord bbox, transform each, find the new AA bbox.
        lib_corners = [
            (self._bbox.min_x, self._bbox.min_y),
            (self._bbox.max_x, self._bbox.min_y),
            (self._bbox.min_x, self._bbox.max_y),
            (self._bbox.max_x, self._bbox.max_y),
        ]
        xs, ys = [], []
        for cx, cy in lib_corners:
            dx, dy = _rotate_then_yinv(cx, cy, rotation)
            xs.append(x + dx)
            ys.append(y + dy)
        # Also expand to include all pin tips, in case the lib bbox missed any.
        for p in placed_pins:
            xs.append(p.x)
            ys.append(p.y)

        return PlacedSymbol(
            lib_id=self.lib_id,
            origin_x=x,
            origin_y=y,
            rotation=rotation,
            pins=tuple(placed_pins),
            bbox=BBox(min(xs), min(ys), max(xs), max(ys)),
        )


class SymbolLibrary:
    """Loads symbols by lib_id from kicad-sch-api's symbol cache."""

    def __init__(self, extra_paths: list[str] | None = None):
        self._cache = ksa.get_symbol_cache()
        if extra_paths:
            self._cache.discover_libraries(extra_paths)
        self._symbol_cache: dict[str, Symbol] = {}

    def discover(self, path: str) -> None:
        self._cache.discover_libraries([path])

    def load_embedded_from(self, sch_path: str) -> list[str]:
        """Lift symbol defs from a .kicad_sch's lib_symbols block and register them."""
        from pinflow_api.emit.placers.greedy_engine.libs.embedded import register_embedded_symbols
        return register_embedded_symbols(sch_path, self._cache)

    def get(self, lib_id: str) -> Symbol:
        if lib_id in self._symbol_cache:
            return self._symbol_cache[lib_id]
        info = self._cache.get_symbol_info(lib_id)
        if info is None:
            raise KeyError(f"Symbol not found: {lib_id!r}")

        # Compute lib-coord bbox from pins + graphics. ksa exposes this via
        # geometry/symbol_bbox.py — easiest path is to ask for it via a dummy
        # placed component, but to keep things clean we compute a simple pin
        # extent here and let the placer use that. (Refine later with graphics
        # bbox when we hit a case where pin extent alone is wrong.)
        if info.pins:
            xs = [p.position.x for p in info.pins]
            ys = [p.position.y for p in info.pins]
            bbox = _SimpleBBox(min(xs), min(ys), max(xs), max(ys))
        else:
            bbox = _SimpleBBox(0, 0, 0, 0)

        sym = Symbol(lib_id, info, bbox)
        self._symbol_cache[lib_id] = sym
        return sym


@dataclass(frozen=True)
class _SimpleBBox:
    """Lightweight lib-coord bbox. Same shape as ksa's BoundingBox so .min_x etc work."""
    min_x: float
    min_y: float
    max_x: float
    max_y: float
