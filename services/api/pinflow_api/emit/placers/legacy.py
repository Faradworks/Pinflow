"""Legacy column placer — `emit.netlist_to_sch.place`, re-exported as an engine.

Kept as a fallback for zero / multi-IC netlists (cplace defers to it when no
single anchor IC is identified) and as a regression reference. The
`netlist_to_sch` module also still hosts the shared placer substrate
(`PlacerError`, `PlacerResult`, `_pin_xy`, `_place_and_measure`, …) consumed
by the other engines — untangling that is out of scope of the placer reorg.
"""

from pinflow_api.emit.netlist_to_sch import PlacerError, PlacerResult, place

__all__ = ["PlacerError", "PlacerResult", "place"]
