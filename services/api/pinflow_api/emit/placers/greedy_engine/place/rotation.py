"""Rotation chooser.

Given a Symbol and the set of pin numbers connected to GND and to power rails,
pick the rotation in {0, 90, 180, 270} that:

  - Maximizes the number of GND pins extending DOWNWARD from the body center
    (positive screen-Y from origin, since +Y is down on screen), AND
  - Maximizes the number of power pins extending UPWARD (negative screen-Y).

GND-down is the hard rule (chosen by the user as "Strict for components touching
GND"). When a component has GND pins, we prioritize getting those right. Power
pins go up as a soft preference among rotations that satisfy the GND rule.

For components without GND pins (e.g., an LED in a chain with anode→+V and
cathode→signal), there's no GND rule to satisfy. We pick rotation based on
which pin connects to the higher-potential net — that pin goes UP.

NO ASSUMPTIONS about symbol orientation. KiCad does not have a convention that
2-pin parts are vertical at rot=0 or that ICs are horizontal at rot=0 — a
symbol's natural shape can be anything. The rotation chooser ONLY inspects
where each pin lands after placement, never the symbol's "shape" in the abstract.

Tiebreak (when several rotations satisfy the same number of constraints): prefer
smaller rotation in the order (0, 180, 90, 270). This is arbitrary but stable
and matches the goldens' bias toward unrotated components.
"""

from __future__ import annotations

from pinflow_api.emit.placers.greedy_engine.symbols import Symbol


def _pin_extension_direction(pin) -> str | None:
    """Screen-direction in which `pin` extends from the body center (the
    placed symbol's origin). Returns 'up'/'down'/'left'/'right', or None if
    the pin is at the origin (degenerate)."""
    px, py = pin.x, pin.y
    if abs(px) < 0.001 and abs(py) < 0.001:
        return None
    if abs(py) >= abs(px):
        return "down" if py > 0 else "up"
    return "right" if px > 0 else "left"


def _score_rotation(sym: Symbol, rot: float,
                    gnd_pins: set[str], power_pins: set[str],
                    connect_pin: str | None,
                    connect_dir: str | None) -> tuple[int, int, int]:
    """Score how well a rotation satisfies the orientation rules.

    Returns (connect_match, gnd_down_count, power_up_count). Lexicographic
    ordering — connect_pin direction is the highest-priority constraint
    when supplied (fan-out: the connecting pin should face back toward the
    source, beating the +V-up / GND-down soft preferences).
    """
    placed = sym.place(0.0, 0.0, rot)
    gnd_down = 0
    power_up = 0
    connect_match = 0
    for pin in placed.pins:
        # Pin's (x, y) is its offset from body center (since origin = (0, 0)).
        if pin.number in gnd_pins and pin.y > 0.001:
            gnd_down += 1
        if pin.number in power_pins and pin.y < -0.001:
            power_up += 1
        if (connect_pin is not None and connect_dir is not None
                and pin.number == connect_pin
                and _pin_extension_direction(pin) == connect_dir):
            connect_match = 1
    return (connect_match, gnd_down, power_up)


def choose_rotation(sym: Symbol,
                    gnd_pins: set[str],
                    power_pins: set[str] | None = None,
                    connect_pin: str | None = None,
                    connect_dir: str | None = None) -> float:
    """Pick the best rotation in {0, 90, 180, 270}.

    `connect_pin` + `connect_dir`: optional hint that one specific pin should
    extend in a given screen direction (used by the greedy placer's fan-out
    to make the tap's source-facing pin point back at the source — which
    matters more than +V-up for inline series elements like ferrite beads
    and series resistors). When set, this becomes the highest-priority
    constraint; +V-up / GND-down are still used as tiebreakers among
    rotations that satisfy it (or among all rotations if none do).
    """
    power_pins = power_pins or set()
    best_rot = 0.0
    best_score = (-1, -1, -1)
    # Tiebreak order: 0 → 180 → 90 → 270. First match wins for equal scores.
    for rot in (0, 180, 90, 270):
        score = _score_rotation(sym, float(rot), gnd_pins, power_pins,
                                connect_pin, connect_dir)
        if score > best_score:
            best_score = score
            best_rot = float(rot)
    return best_rot
