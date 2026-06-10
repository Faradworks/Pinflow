"""Emit pipeline: turns chip extracts / netlists into placed kicad_sch text.

`layout.py` handles merge + wrap-around placement of generated subcircuits
into the user's existing schematic. The netlist-first placer
(`netlist_to_sch.py`) lands in a later step (design-doc §5).
"""
