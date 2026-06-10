"""Declarative layout constraints + the per-axis solver.

The constraint engine is the layout architecture used by `emit.placers.cplace`.
Each archetype emits **declarative constraints** — a shared vocabulary
describing how parts relate — and this module resolves the constraint set into
coordinates. A new topology then
emits a different *combination* of the same primitives; the geometry
generalises instead of being re-coded.

Constraints are one-dimensional and solved an axis at a time — X and Y are
independent. That holds for schematic layout (alignment, even spacing, rails
are each per-axis) and keeps the solver a simple deterministic propagation.

Vocabulary — every constraint names *variables*; a variable is one part's (or
rail's) coordinate on the axis being solved:

  - `Anchor(var, value)`  — var == value. Pins the layout; normally the IC.
  - `Offset(a, b, delta)` — b == a + delta. A rigid relative position
                            (a cap hanging a fixed drop below its rail,
                            evenly-pitched bank members, a divider stack).
  - `MinGap(a, b, gap)`   — b >= a + gap. Ordering + non-overlap.

`solve(constraints)` resolves a set. The emitted graph is expected to be a
**tree rooted at the anchor(s)** — each variable positioned by one chain back
to an anchor — which a single propagation pass resolves exactly (`MinGap` then
packs tight against its neighbour). That is sufficient for the per-archetype
emitters, which position every group relative to the IC. A cross-linked graph
(global compaction, two anchors in tension) would need longest-path / Cassowary
— a deliberate future upgrade, flagged in `solve`; over-constrained input is
reported in `SolveResult.issues` rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_EPS = 1e-6


# --- the vocabulary ----------------------------------------------------------

@dataclass(frozen=True)
class Anchor:
    """`var == value` — an absolute coordinate. The layout's reference point."""

    var: str
    value: float


@dataclass(frozen=True)
class Offset:
    """`b == a + delta` — a rigid relative position."""

    a: str
    b: str
    delta: float


@dataclass(frozen=True)
class MinGap:
    """`b >= a + gap` — ordering with a minimum separation (non-overlap)."""

    a: str
    b: str
    gap: float


Constraint = Anchor | Offset | MinGap


@dataclass
class SolveResult:
    """Outcome of `solve` — resolved coordinates plus any structural issues."""

    pos: dict[str, float]
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


# --- the solver --------------------------------------------------------------

def _variables(constraints: list[Constraint]) -> set[str]:
    out: set[str] = set()
    for c in constraints:
        if isinstance(c, Anchor):
            out.add(c.var)
        else:
            out.add(c.a)
            out.add(c.b)
    return out


def solve(constraints: list[Constraint], *, fallback: float = 0.0) -> SolveResult:
    """Resolve a one-axis constraint set to a coordinate per variable.

    Propagates outward from the `Anchor`s: an `Offset` places its far end
    exactly, a `MinGap` places it packed tight (`b = a + gap`). Iterated until
    no variable changes — for the tree-shaped graphs the emitters produce this
    converges in a pass or two. Variables never reached by an anchor fall back
    to `fallback` and are flagged; an `Offset` / `MinGap` violated by two
    already-placed ends is flagged too. Issues never raise — a partial layout
    is still inspectable, and the caller (the placer) surfaces them.
    """
    issues: list[str] = []
    pos: dict[str, float] = {}

    for c in constraints:
        if isinstance(c, Anchor):
            if c.var in pos and abs(pos[c.var] - c.value) > _EPS:
                issues.append(
                    f"conflicting anchors on {c.var!r}: "
                    f"{pos[c.var]:g} vs {c.value:g}"
                )
            pos[c.var] = c.value

    relations = [c for c in constraints if not isinstance(c, Anchor)]
    allvars = _variables(constraints)

    # Outward propagation. One known endpoint of a relation determines the
    # other; bounded by the variable count (the propagation front advances one
    # hop per pass, and a tree is at most `len(allvars)` deep).
    for _ in range(len(allvars) + 2):
        changed = False
        for c in relations:
            d = c.delta if isinstance(c, Offset) else c.gap
            if c.a in pos and c.b not in pos:
                pos[c.b] = pos[c.a] + d
                changed = True
            elif c.b in pos and c.a not in pos:
                pos[c.a] = pos[c.b] - d
                changed = True
        if not changed:
            break

    # Consistency: a relation whose ends were both fixed by other paths.
    for c in relations:
        if c.a not in pos or c.b not in pos:
            continue
        span = pos[c.b] - pos[c.a]
        if isinstance(c, Offset) and abs(span - c.delta) > _EPS:
            issues.append(
                f"offset {c.a!r}->{c.b!r} wants {c.delta:g}, got {span:g}"
            )
        elif isinstance(c, MinGap) and span < c.gap - _EPS:
            issues.append(
                f"min-gap {c.a!r}->{c.b!r} wants >={c.gap:g}, got {span:g}"
            )

    for v in sorted(allvars):
        if v not in pos:
            pos[v] = fallback
            issues.append(f"{v!r} not reachable from any anchor")

    return SolveResult(pos=pos, issues=issues)
