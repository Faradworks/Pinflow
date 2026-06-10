"""Placer engine registry.

One file per engine. A placer is any callable matching:

    placer(netlist: Netlist, *, title: str = "Subcircuit") -> PlacerResult

`get_placer(name)` returns the engine; `place(nl, ...)` is a thin convenience
that calls the default. The agent tool and the eval harness both go through
`get_placer` so swapping the production engine is a one-name change here.

Current registry:

  - `auto` — gate: runs cplace by default, falls back to greedy when the
    layout tree shows the dense-IC pattern (`SIGNAL_STAIRCASE` and
    `DIVIDER_STACK` on the same IC side, where cplace's independent
    archetype emitters coordinate poorly and produce inter-archetype body
    overlaps the rubric undercounts).
  - `cplace` — constraint-based archetype placer; declarative `Anchor` /
    `MinGap` / `Offset` set per archetype, deterministic per-axis solver.
    The production default when the layout tree is well-coordinated.
  - `greedy` — subprocess wrapper around `/examples` BFS placer. Handles
    the dense-IC failure mode cleanly via the cap-island + signal-
    staircase + power-at-edge idiom. Not deterministic byte-identical
    across runs; gate-fails on some sparse-IC topologies — so it is
    *not* a general-purpose replacement, only the dense-IC branch.
  - `legacy` — the older imperative column placer (`emit.netlist_to_sch.place`).
    Kept as a fallback for zero / multi-IC netlists and as a regression
    reference; also still consumed by the older one-shot `/generate` chain
    (`routes/generate.py`).
  - `llm_placer` — *experimental*. LLM emits per-part `Anchor` constraints,
    the deterministic solver resolves the rest. Not on the agent path; used
    only by the dedicated eval scripts.
"""

from __future__ import annotations

from typing import Callable

from pinflow_api.emit.netlist import Netlist
from pinflow_api.emit.netlist_to_sch import PlacerError, PlacerResult


PlacerFn = Callable[..., PlacerResult]

DEFAULT_PLACER = "auto"


def _load_cplace() -> PlacerFn:
    from pinflow_api.emit.placers.cplace import cplace
    return cplace


def _load_legacy() -> PlacerFn:
    from pinflow_api.emit.placers.legacy import place
    return place


def _load_llm() -> PlacerFn:
    from pinflow_api.emit.placers.llm_placer import cplace_with_llm_best_of
    return cplace_with_llm_best_of


def _load_greedy() -> PlacerFn:
    from pinflow_api.emit.placers.greedy import greedy
    return greedy


def _is_dense_ic_pattern(tree) -> bool:
    """Detect the failure mode where cplace's archetype coordination breaks
    down: a SIGNAL_STAIRCASE *and* a DIVIDER_STACK on the same IC side. On
    tps61088 this is the trigger that produces the C19/R10 + R6/R11 inter-
    archetype overlaps. Other dense-IC patterns (e.g. multiple BOOTSTRAPs
    plus dense control resistors) may want their own gate later — keep
    the check explicit so it grows by demonstrated failure modes, not by
    speculation."""
    from pinflow_api.emit.layout_tree import Archetype
    staircase_sides = {
        g.side for g in tree.groups
        if g.archetype == Archetype.SIGNAL_STAIRCASE
    }
    if not staircase_sides:
        return False
    divider_sides = {
        g.side for g in tree.groups
        if g.archetype == Archetype.DIVIDER_STACK
    }
    return bool(staircase_sides & divider_sides)


def _load_auto() -> PlacerFn:
    """Returns a closure that picks cplace vs greedy per netlist by
    inspecting its layout tree. Greedy depends on `/examples` being
    installed; if greedy raises `PlacerError` (e.g. /examples missing,
    subprocess timeout) the gate falls back to cplace.

    `source_schematic` kw is forwarded to greedy only — cplace doesn't
    need / accept it. Other kwargs (e.g. `tree`, `extras_x`) are forwarded
    to cplace only."""
    from pinflow_api.emit.layout_tree import build_layout_tree

    def auto(netlist: Netlist, *, title: str = "Subcircuit", **kw):
        tree = build_layout_tree(netlist)
        if _is_dense_ic_pattern(tree):
            greedy_kw = {
                k: v for k, v in kw.items() if k in {"source_schematic"}
            }
            try:
                return _load_greedy()(netlist, title=title, **greedy_kw)
            except PlacerError:
                # Greedy unavailable or failed — fall through to cplace.
                pass
        cplace_kw = {
            k: v for k, v in kw.items() if k != "source_schematic"
        }
        return _load_cplace()(netlist, title=title, **cplace_kw)

    return auto


# Lazy loaders so importing the registry doesn't pull in Anthropic / kicad-sch-api
# until an engine is actually requested.
_REGISTRY: dict[str, Callable[[], PlacerFn]] = {
    "auto": _load_auto,
    "cplace": _load_cplace,
    "greedy": _load_greedy,
    "legacy": _load_legacy,
    "llm_placer": _load_llm,
}


def list_placers() -> list[str]:
    return sorted(_REGISTRY)


def get_placer(name: str | None = None) -> PlacerFn:
    """Resolve a placer by name; `None` returns the default (`cplace`)."""
    key = name or DEFAULT_PLACER
    if key not in _REGISTRY:
        raise KeyError(
            f"unknown placer {key!r}; known: {', '.join(list_placers())}"
        )
    return _REGISTRY[key]()


def place(netlist: Netlist, *, title: str = "Subcircuit",
          placer: str | None = None) -> PlacerResult:
    """Convenience wrapper: resolve `placer` (default `cplace`) and call it."""
    return get_placer(placer)(netlist, title=title)


__all__ = [
    "DEFAULT_PLACER",
    "PlacerError",
    "PlacerFn",
    "PlacerResult",
    "get_placer",
    "list_placers",
    "place",
]
