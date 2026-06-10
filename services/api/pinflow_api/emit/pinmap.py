"""Per-pin geometry + semantics for a placed IC — the layout grammar's foundation.

The IC symbol's pinout *encodes the intended schematic layout*: which edge of
the symbol body a pin sits on tells the placer which side that pin's support
components belong. KiCad library authors draw symbols by convention — supply
in on the left, output on the right, ground at the bottom — so reading the
pinout back is how the placer recovers a human's layout intent.

`extract_pinmap` turns a placed component (or the unit-list of a multi-unit
symbol) into `PinInfo` records the classifier and placer consume:
  - `name` / `number` — the pin's identity,
  - `etype` — KiCad electrical type (`power_in`, `power_out`, `output`,
    `input`, `passive`, …) — distinguishes a supply pin from a signal pin,
  - `side` — which body edge (L/R/T/B) the pin sits on,
  - `(x, y)` — absolute connection-point coordinates in the placed frame.

Side is derived from pin position, not pin rotation: each pin is assigned to
the nearest edge of the pin-cloud bounding box. That sidesteps KiCad's pin-
angle convention entirely and is robust to the component's own rotation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

import kicad_sch_api as ksa

Side = Literal["L", "R", "T", "B"]


@dataclass(frozen=True)
class PinInfo:
    """One pin of a placed IC, with the geometry the layout grammar needs."""

    number: str
    name: str
    etype: str   # KiCad electrical type: power_in/power_out/output/input/passive/...
    side: Side   # body edge the pin sits on, from nearest-edge classification
    x: float     # absolute connection-point coordinate (placed frame)
    y: float

    @property
    def is_power(self) -> bool:
        return self.etype in ("power_in", "power_out")


def _pin_xy(pin) -> tuple[float, float]:
    pos = pin.position
    x = pos.x if hasattr(pos, "x") else pos[0]
    y = pos.y if hasattr(pos, "y") else pos[1]
    return float(x), float(y)


def _norm_etype(pin_type) -> str:
    """Normalize ksa's `pin_type` to a bare lowercase string.

    ksa hands back a `PinType` enum (`PinType.POWER_IN`); the classifier
    wants the plain KiCad token (`power_in`). Works whether ksa returns the
    enum or, in a future version, a plain string.
    """
    raw = getattr(pin_type, "name", None) or str(pin_type)
    return raw.split(".")[-1].lower()


def extract_pinmap(comps) -> list[PinInfo]:
    """Return `PinInfo` for every pin of a placed IC.

    `comps` is a single ksa Component or the list sharing one refdes (the
    members of a multi-unit symbol). Pins are unioned across units; side
    classification runs over the union's bounding box. (For a multi-unit
    symbol whose units are fanned far apart, side is approximate — single-
    unit ICs, the common case, classify exactly.)
    """
    if not isinstance(comps, (list, tuple)):
        comps = [comps]

    raw: list[tuple[str, str, str, float, float]] = []
    for c in comps:
        for p in c.pins:
            x, y = _pin_xy(p)
            raw.append((str(p.number), str(p.name), _norm_etype(p.pin_type), x, y))
    if not raw:
        return []

    xs = [r[3] for r in raw]
    ys = [r[4] for r in raw]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    out: list[PinInfo] = []
    for number, name, etype, x, y in raw:
        # KiCad schematic Y grows downward: min_y is the top edge.
        dist: dict[Side, float] = {
            "L": x - min_x,
            "R": max_x - x,
            "T": y - min_y,
            "B": max_y - y,
        }
        side = min(dist, key=dist.get)  # type: ignore[arg-type]
        out.append(PinInfo(number, name, etype, side, x, y))
    return out


def pinmap_for_lib_id(lib_id: str, *, reference: str = "U1") -> list[PinInfo]:
    """Convenience: place `lib_id` into a throwaway schematic and read its pins.

    For callers that need the pinout before the real placement exists (the
    classifier). The IC is placed at rotation 0 so `side` reflects the
    symbol's own drawn orientation.
    """
    sch = ksa.create_schematic("_pinmap")
    sch.components.add(
        lib_id=lib_id, reference=reference, value=reference, position=(100.0, 100.0)
    )
    comps = [c for c in sch.components if c.reference == reference]
    return extract_pinmap(comps)


def by_number(pins: Iterable[PinInfo]) -> dict[str, PinInfo]:
    """Index a pinmap by pin number — the key `NetlistEndpoint.pin` carries."""
    return {p.number: p for p in pins}
