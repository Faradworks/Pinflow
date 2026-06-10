"""Placement data types — what placers output, what emitters consume."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlacedComponent:
    """A component with chosen position and rotation."""
    ref: str
    lib_id: str
    value: str
    x: float
    y: float
    rotation: float                # 0, 90, 180, or 270
    footprint: str = ""


@dataclass(frozen=True)
class PlacedPowerFlag:
    """A power-flag symbol (#PWRxxx) attached to a wire endpoint.

    `net` is the net name this flag represents (e.g. '+5V', 'GND').
    `rotation` matters for +V flags vs GND flags — KiCad's GND symbol points
    down by default; +V symbols point up.
    """
    net: str
    x: float
    y: float
    rotation: float = 0.0

    @property
    def lib_id(self) -> str:
        return f"power:{self.net}"


@dataclass
class Placement:
    """Complete placement output for one schematic."""
    components: list[PlacedComponent] = field(default_factory=list)
    power_flags: list[PlacedPowerFlag] = field(default_factory=list)

    # Optional per-net rail-Y overrides. When the placer arranges a net's taps
    # in a way the writer's auto-histogram would route awkwardly (e.g., a
    # fan-out where the single source pin is at one Y and 2+ taps share another
    # Y), the placer can pin the horizontal rail to a specific Y. Keyed by
    # net name.
    routing_hints: dict[str, float] = field(default_factory=dict)

    # Bookkeeping the placer can use; emitter can ignore.
    notes: dict[str, str] = field(default_factory=dict)

    def by_ref(self, ref: str) -> PlacedComponent | None:
        for c in self.components:
            if c.ref == ref:
                return c
        return None
