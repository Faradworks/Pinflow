"""Parse a kicadsexpr netlist (output of `kicad-cli sch export netlist --format kicadsexpr`).

Extracts the connectivity + component metadata KiCad's netlister produces.
Does NOT recover user-set symbol properties (MPN, Datasheet URL, Manufacturer) —
those don't round-trip through the netlist export. See `sch_properties.py` for
direct `.kicad_sch` property reads; the two are joined by refdes inside
`graph.builder.build_design_graph`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pinflow_api.builders._common import _matching_close


@dataclass
class ComponentMeta:
    """Connectivity-side metadata for one component. No user properties."""

    refdes: str
    value: str = ""
    footprint: str = ""
    lib_id: str | None = None  # "<lib>:<part>" from <libsource>


@dataclass
class ParsedNetlist:
    components: dict[str, ComponentMeta] = field(default_factory=dict)
    nets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # net_name -> [(ref, pin_num)]


# Quoted-string capture used inside each (form ...) body.
_QSTR = r'"((?:[^"\\]|\\.)*)"'


def parse_kicadsexpr(text: str) -> ParsedNetlist:
    """Parse a kicadsexpr netlist text into components + nets."""
    components: dict[str, ComponentMeta] = {}
    nets: dict[str, list[tuple[str, str]]] = {}

    for block in _iter_forms(text, "components"):
        for comp_block in _iter_subforms(block, "comp"):
            meta = _parse_comp(comp_block)
            if meta is not None:
                components[meta.refdes] = meta

    for block in _iter_forms(text, "nets"):
        for net_block in _iter_subforms(block, "net"):
            name, pins = _parse_net(net_block)
            if name:
                # Deterministic but multiple-pin nets on the same component are fine
                nets[name] = pins

    return ParsedNetlist(components=components, nets=nets)


# ---------------------------------------------------------------------------
# Form iteration
# ---------------------------------------------------------------------------


def _iter_forms(text: str, head: str):
    """Yield the body slice of every `(<head> ...)` form found in text.

    The yielded slice starts immediately after the head name so subsequent
    sub-form iteration sees the body's child forms directly.
    """
    needle = f"({head}"
    pos = 0
    while True:
        i = text.find(needle, pos)
        if i < 0:
            return
        end_of_head = i + len(needle)
        # confirm head is followed by whitespace or '(' (avoid prefix matches like "components_x")
        if end_of_head < len(text) and text[end_of_head] not in " \t\n\r(":
            pos = end_of_head
            continue
        close = _matching_close(text, i)
        if close < 0:
            return
        yield text[end_of_head:close]
        pos = close + 1


def _iter_subforms(body: str, head: str):
    """Yield each direct child `(<head> ...)` form within a parent body, as raw slice.

    Tolerates a leading head identifier in `body` (e.g. when caller passed a
    raw `(name args ...)` slice without trimming) by skipping to the first `(`.
    """
    head_re = re.compile(r"\(\s*([A-Za-z_][\w]*)")
    n = len(body)
    pos = body.find("(")  # skip any leading head identifier / whitespace
    if pos < 0:
        return
    while pos < n:
        # skip whitespace
        while pos < n and body[pos] in " \t\n\r":
            pos += 1
        if pos >= n or body[pos] != "(":
            break
        close = _matching_close(body, pos)
        if close < 0:
            break
        m = head_re.match(body[pos:])
        if m and m.group(1) == head:
            yield body[pos : close + 1]
        pos = close + 1


# ---------------------------------------------------------------------------
# Per-form extractors
# ---------------------------------------------------------------------------


_RE_REF = re.compile(r"\(\s*ref\s+" + _QSTR + r"\s*\)")
_RE_VALUE = re.compile(r"\(\s*value\s+" + _QSTR + r"\s*\)")
_RE_FOOTPRINT = re.compile(r"\(\s*footprint\s+" + _QSTR + r"\s*\)")
_RE_LIBSOURCE = re.compile(
    r"\(\s*libsource\s+\(\s*lib\s+" + _QSTR + r"\s*\)\s*\(\s*part\s+" + _QSTR + r"\s*\)",
    re.DOTALL,
)
_RE_NET_NAME = re.compile(r"\(\s*name\s+" + _QSTR + r"\s*\)")


def _parse_comp(block: str) -> ComponentMeta | None:
    m_ref = _RE_REF.search(block)
    if not m_ref:
        return None
    ref = m_ref.group(1)

    m_val = _RE_VALUE.search(block)
    m_fp = _RE_FOOTPRINT.search(block)
    m_libsrc = _RE_LIBSOURCE.search(block)

    lib_id: str | None = None
    if m_libsrc:
        lib_id = f"{m_libsrc.group(1)}:{m_libsrc.group(2)}"

    return ComponentMeta(
        refdes=ref,
        value=m_val.group(1) if m_val else "",
        footprint=m_fp.group(1) if m_fp else "",
        lib_id=lib_id,
    )


def _parse_net(block: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (net_name, [(ref, pin_number), ...])."""
    m_name = _RE_NET_NAME.search(block)
    if not m_name:
        return "", []
    name = m_name.group(1)

    pins: list[tuple[str, str]] = []
    for node_block in _iter_subforms(_strip_outer(block), "node"):
        m_ref = _RE_REF.search(node_block)
        # pin number lives in (pin "N") inside the node — use a local regex
        m_pin = re.search(r"\(\s*pin\s+" + _QSTR + r"\s*\)", node_block)
        if m_ref and m_pin:
            pins.append((m_ref.group(1), m_pin.group(1)))

    return name, pins


def _strip_outer(block: str) -> str:
    """Strip the outermost `(...)` from a form for sub-iteration."""
    if block.startswith("(") and block.endswith(")"):
        return block[1:-1]
    return block
