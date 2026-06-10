"""Layout tree — the structured IR between classification and placement.

`classify()` produces a *flat* `LayoutPlan` — a role per part, a kind per net,
a list of feedback dividers. `build_layout_tree()` reorganises that into the
hierarchy a placer actually lays out: an anchor IC, the input / output / ground
rails, and a set of archetype-tagged **groups** — the input filter, the output
cap bank, the feedback divider, the config caps, the power inductor.

The group, not the part, is the unit of layout. Each archetype has a known
canonical shape:

  - `series_filter`  — caps + an inline series element (ferrite / series R) on
                       one rail; laid out as a run along the rail trunk.
  - `rail_cap_bank`  — caps on a rail, no series element; an evenly-spaced run
                       hanging off the rail.
  - `divider_stack`  — a two-resistor feedback divider; one vertical column.
  - `config_cap`     — bypass cap(s) on a control pin; placed at that pin.
  - `power_inductor` — the switch-node inductor; hugs the IC's SW pins.
  - `loose`          — parts the rules did not group; a fallback column.

A placer (or a constraint emitter) dispatches on the archetype rather than
re-deriving structure per part. This is also the IR a future LLM tree-proposer
would emit for circuits the rules below don't recognise — so it stays
serialisable and free of geometry. Coordinates are the placer's job; the tree
carries only structure plus the IC body **side** each group attaches to.

Single-IC scope: `anchor` is set only when the netlist has exactly one IC —
the case the placer rebuild targets first. A zero / multi-IC netlist still
yields a tree but `anchor` is None.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from pinflow_api.emit.classify import (
    LayoutPlan,
    NetKind,
    RailSide,
    Role,
    classify,
)
from pinflow_api.emit.netlist import Netlist


class Archetype(str, Enum):
    SERIES_FILTER = "series_filter"
    RAIL_CAP_BANK = "rail_cap_bank"
    DIVIDER_STACK = "divider_stack"
    CONFIG_CAP = "config_cap"               # cap on a single IC control pin
    CONTROL_RESISTOR = "control_resistor"   # R between an IC pin and a rail/gnd
    BOOTSTRAP = "bootstrap"                 # 2-pin part between two IC pins
    POWER_INDUCTOR = "power_inductor"
    SHUNT_BRANCH = "shunt_branch"           # chain rail→…→GND, no IC contact
    SIGNAL_STAIRCASE = "signal_staircase"   # row of taps below the IC body
    LOOSE = "loose"


@dataclass
class GroupNode:
    """One archetype-tagged group of parts the placer lays out as a unit."""

    archetype: Archetype
    members: list[str]              # refdeses, natural-sorted
    side: str | None                # IC body edge it attaches to: L/R/T/B
    rail: str | None                # rail net this group serves, if any
    label: str                      # human-readable, for traces / cards


@dataclass
class LayoutTree:
    """Structured layout IR for one subcircuit. Geometry-free."""

    netlist: Netlist
    plan: LayoutPlan
    anchor: str | None              # the single IC, or None (0 / many ICs)
    input_rail: str | None          # net name
    output_rail: str | None
    ground: str | None
    groups: list[GroupNode] = field(default_factory=list)

    def group_of(self, refdes: str) -> GroupNode | None:
        return next((g for g in self.groups if refdes in g.members), None)

    def summary(self) -> dict:
        """Compact dict for the debug trace / verification."""
        return {
            "anchor": self.anchor,
            "rails": {
                "input": self.input_rail,
                "output": self.output_rail,
                "ground": self.ground,
            },
            "groups": [
                {
                    "archetype": g.archetype.value,
                    "members": g.members,
                    "side": g.side,
                    "rail": g.rail,
                }
                for g in self.groups
            ],
        }


# --- helpers -----------------------------------------------------------------

def _ref_prefix(ref: str) -> str:
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref


def _natural_key(ref: str) -> tuple[str, int, str]:
    prefix = _ref_prefix(ref)
    tail = ref[len(prefix):]
    return (prefix, int(tail) if tail.isdigit() else 0, ref)


class _UnionFind:
    """Net-name union-find — merges the nets a series element bridges into one
    logical rail, so caps on either side of a ferrite group together."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        self._parent[self.find(b)] = self.find(a)


def _nongnd_nets(refdes: str, plan: LayoutPlan) -> list[str]:
    """The non-ground net names a part touches."""
    pc = plan.parts.get(refdes)
    if pc is None:
        return []
    return [
        n for n in pc.nets
        if (nc := plan.nets.get(n)) is not None and nc.kind != NetKind.GROUND
    ]


def _touches_rail(refdes: str, plan: LayoutPlan) -> bool:
    """True if the part sits on a true power rail (vs. only signal nets) —
    distinguishes an input/output filter element from the switch inductor."""
    return any(
        plan.nets[n].kind == NetKind.RAIL
        for n in _nongnd_nets(refdes, plan)
    )


def _group_side(members: list[str], plan: LayoutPlan, anchor: str | None) -> str | None:
    """IC body edge (L/R/T/B) the group's non-ground nets land on — the modal
    side of the IC pins they contact. None when no member touches the IC."""
    if anchor is None:
        return None
    sides: list[str] = []
    for r in members:
        for net_name in _nongnd_nets(r, plan):
            nc = plan.nets.get(net_name)
            if nc is None:
                continue
            sides += [c.pin.side for c in nc.ic_contacts if c.ic_refdes == anchor]
    return Counter(sides).most_common(1)[0][0] if sides else None


# --- build -------------------------------------------------------------------

def build_layout_tree(netlist: Netlist) -> LayoutTree:
    """Classify `netlist` and reorganise it into a `LayoutTree`.

    Pure apart from `classify`'s pinmap step (which places each IC into a
    throwaway schematic — see `emit.classify`).
    """
    plan = classify(netlist)
    anchor = plan.ics[0] if len(plan.ics) == 1 else None

    # --- rails: first input / output / ground net of each kind --------------
    input_rail = output_rail = ground = None
    for name, nc in plan.nets.items():
        if nc.kind == NetKind.GROUND:
            if ground is None:
                ground = name
        elif nc.kind == NetKind.RAIL:
            if nc.rail_side == RailSide.OUTPUT:
                output_rail = output_rail or name
            else:
                input_rail = input_rail or name

    # --- logical rails: series elements merge the nets they bridge ----------
    uf = _UnionFind()
    for refdes, pc in plan.parts.items():
        if pc.role == Role.SERIES_ELEMENT:
            nn = _nongnd_nets(refdes, plan)
            for other in nn[1:]:
                uf.union(nn[0], other)

    def _logical_rail(refdes: str) -> str | None:
        nn = _nongnd_nets(refdes, plan)
        return uf.find(nn[0]) if nn else None

    input_lr = uf.find(input_rail) if input_rail else None
    output_lr = uf.find(output_rail) if output_rail else None

    # net_name → list[refdes] — used by the shunt-chain walker to find the
    # other parts on an internal signal net. Built here so several rules can
    # share it.
    net_members: dict[str, list[str]] = {}
    for net in netlist.nets:
        for ep in net.endpoints:
            net_members.setdefault(net.name, []).append(ep.ref)

    groups: list[GroupNode] = []
    claimed: set[str] = set()

    # --- feedback dividers (claimed first — they own their resistors) -------
    for d in plan.dividers:
        members = sorted([d.high_refdes, d.low_refdes], key=_natural_key)
        groups.append(GroupNode(
            Archetype.DIVIDER_STACK, members,
            _group_side(members, plan, anchor), d.sensed_net,
            f"feedback divider ({d.high_refdes}/{d.low_refdes})",
        ))
        claimed.update(members)

    # --- power inductor: switch-node-only inductors -------------------------
    # An L* that bridges two switch-side signals (a buck-boost's main coil)
    # belongs here. An L* that touches a power rail (a boost's coil from the
    # input rail up to SW) reads as a series element on that rail and folds
    # into the rail filter — its right place is inline on the rail trunk, not
    # at the switch pins. Refdes-restricted so rail-less series caps /
    # resistors (boot caps, config dividers) are not mistaken for inductors.
    inductors = sorted(
        (r for r, pc in plan.parts.items()
         if r not in claimed and pc.role != Role.IC
         and _ref_prefix(r) == "L"
         and not _touches_rail(r, plan)),
        key=_natural_key,
    )
    if inductors:
        groups.append(GroupNode(
            Archetype.POWER_INDUCTOR, inductors,
            _group_side(inductors, plan, anchor), None, "power inductor",
        ))
        claimed.update(inductors)

    # --- a rail filter / bank: caps + series elements on one logical rail ---
    def _rail_group(rail_net: str | None, rail_lr: str | None, cap_role: Role,
                    side_default: str, label: str) -> None:
        if rail_net is None or rail_lr is None:
            return
        members = [
            r for r, pc in plan.parts.items()
            if r not in claimed
            and _logical_rail(r) == rail_lr
            and (pc.role == cap_role
                 or (pc.role == Role.SERIES_ELEMENT and _touches_rail(r, plan)))
        ]
        if not members:
            return
        members.sort(key=_natural_key)
        has_series = any(
            plan.parts[r].role == Role.SERIES_ELEMENT for r in members
        )
        arch = Archetype.SERIES_FILTER if has_series else Archetype.RAIL_CAP_BANK
        groups.append(GroupNode(
            arch, members,
            _group_side(members, plan, anchor) or side_default, rail_net, label,
        ))
        claimed.update(members)

    _rail_group(input_rail, input_lr, Role.INPUT_CAP, "L", "input filter")
    _rail_group(output_rail, output_lr, Role.OUTPUT_CAP, "R", "output bank")

    # --- config caps: bypass caps on a control pin --------------------------
    config = sorted(
        (r for r, pc in plan.parts.items()
         if r not in claimed and pc.role == Role.CONFIG_CAP),
        key=_natural_key,
    )
    if config:
        groups.append(GroupNode(
            Archetype.CONFIG_CAP, config,
            _group_side(config, plan, anchor), None, "config caps",
        ))
        claimed.update(config)

    # --- control resistors: a pull / bias / series-control resistor that
    # touches exactly one IC control pin (a non-rail, non-ground signal). Pull-
    # ups (EN to rail, ILIM to ground) and small compensation resistors land
    # here — they share the cap-on-control-pin emitter's geometry: one part per
    # pin, anchored at that pin's Y, X just past the IC side. Without this,
    # they fell to the column-below-IC `loose` fallback and the router had to
    # run leashes around the IC body for every one. -------------------------
    def _ic_signal_nets(refdes: str) -> list[str]:
        """Non-rail / non-ground nets this part touches that themselves
        contact the anchor IC. Counted as nets (not pin numbers) so a multi-
        pin signal — an SW node with 4 SW pins on the package, an internal
        VOUT bus — doesn't inflate the count and break bootstrap detection."""
        if anchor is None:
            return []
        out: list[str] = []
        pc = plan.parts.get(refdes)
        if pc is None:
            return []
        for net_name in pc.nets:
            nc = plan.nets.get(net_name)
            if nc is None or nc.kind in (NetKind.GROUND, NetKind.RAIL):
                continue
            if any(c.ic_refdes == anchor for c in nc.ic_contacts):
                out.append(net_name)
        return out

    ctrl_rs = sorted(
        (r for r, pc in plan.parts.items()
         if r not in claimed and pc.role != Role.IC
         and _ref_prefix(r) == "R"
         and len(_ic_signal_nets(r)) == 1),
        key=_natural_key,
    )
    if ctrl_rs:
        groups.append(GroupNode(
            Archetype.CONTROL_RESISTOR, ctrl_rs,
            _group_side(ctrl_rs, plan, anchor), None, "control resistors",
        ))
        claimed.update(ctrl_rs)

    # --- bootstrap: a 2-pin part bridging two IC signal nets, neither rail
    # nor ground. A boost converter's bootstrap cap (BOOT↔SW), a buck's FSW
    # resistor (FSW↔SW), or a sense resistor between two adjacent control
    # pins — all sit *between* two IC pins rather than hanging off one. The
    # emitter places them just above the IC, straddling the two pins. ------
    boots = sorted(
        (r for r, pc in plan.parts.items()
         if r not in claimed and pc.role != Role.IC
         and len(_ic_signal_nets(r)) == 2),
        key=_natural_key,
    )
    if boots:
        groups.append(GroupNode(
            Archetype.BOOTSTRAP, boots,
            _group_side(boots, plan, anchor), None, "bootstrap parts",
        ))
        claimed.update(boots)

    # --- shunt branch: a chain of 2+ parts forming a series path from a rail
    # to ground, NOT touching the IC. An LED indicator (rail → D → R → GND), a
    # discharge resistor stack on a rail, a sense divider that pre-dates the
    # IC's FB pin — all sit *vertically* alongside the rail they sense,
    # between the rail trunk and ground. Detected by walking each non-claimed
    # part's internal-signal nets: if the chain has exactly one rail endpoint
    # and exactly one ground endpoint and no IC contact, it's a shunt. -------
    def _shunt_chain(seed: str) -> list[str] | None:
        """Walk from `seed` along internal-signal nets, return the chain if
        it terminates at a rail on one end and ground on the other (and
        touches no IC pin or other already-claimed part)."""
        pc = plan.parts.get(seed)
        if pc is None or pc.role == Role.IC or seed in claimed:
            return None
        # Each member must be 2-pin (cap/R/diode/inductor) — we walk through
        # internal-signal nets and only chains of 2-pin parts make sense.
        # Membership is built greedily: start from `seed`, follow each net
        # endpoint, accumulate the connected component.
        visited: set[str] = set()
        frontier = [seed]
        chain: list[str] = []
        rail_end_count = 0
        ground_end_count = 0
        while frontier:
            cur = frontier.pop()
            if cur in visited or cur in claimed:
                continue
            visited.add(cur)
            pc_cur = plan.parts.get(cur)
            if pc_cur is None or pc_cur.role == Role.IC:
                return None
            # 2-pin: each part has exactly 2 distinct nets.
            if len(pc_cur.nets) != 2:
                return None
            chain.append(cur)
            for net_name in pc_cur.nets:
                nc = plan.nets.get(net_name)
                if nc is None:
                    return None
                if nc.kind == NetKind.GROUND:
                    # Ground endpoint OK — both rails and GND legitimately
                    # touch the IC. The shunt sits *across* them.
                    ground_end_count += 1
                    continue
                if nc.kind == NetKind.RAIL:
                    rail_end_count += 1
                    continue
                # Internal signal net. If it touches the anchor IC, the
                # chain is tied to a control/feedback pin and another
                # archetype (divider / control_resistor) owns it — abort.
                if anchor and any(c.ic_refdes == anchor
                                  for c in nc.ic_contacts):
                    return None
                # Walk through to other 2-pin member(s) on this internal net.
                for other in net_members.get(net_name, []):
                    if other == cur:
                        continue
                    if other in claimed:
                        return None
                    other_pc = plan.parts.get(other)
                    if other_pc is None or other_pc.role == Role.IC:
                        return None
                    frontier.append(other)
        # A valid shunt chain has at least 2 members, exactly 1 rail
        # endpoint, exactly 1 ground endpoint.
        if (len(chain) >= 2 and rail_end_count == 1
                and ground_end_count == 1):
            return sorted(chain, key=_natural_key)
        return None

    shunt_seeds = sorted(
        (r for r, pc in plan.parts.items()
         if r not in claimed and pc.role != Role.IC),
        key=_natural_key,
    )
    for seed in shunt_seeds:
        if seed in claimed:
            continue
        chain = _shunt_chain(seed)
        if chain is None:
            continue
        # Identify the rail this branch sits next to (for side / rail tag).
        rail_net = None
        for r in chain:
            for net_name in plan.parts[r].nets:
                nc = plan.nets.get(net_name)
                if nc is not None and nc.kind == NetKind.RAIL:
                    rail_net = net_name
                    break
            if rail_net:
                break
        # Side: opposite of the input filter — same as the rail's side.
        side = "R" if rail_net == output_rail else "L"
        groups.append(GroupNode(
            Archetype.SHUNT_BRANCH, chain, side, rail_net,
            f"shunt branch ({' → '.join(chain)})",
        ))
        claimed.update(chain)

    # --- signal staircase: ≥2 same-side single-pin taps that cluster in Y.
    # When a dense IC fans 3+ control pins out the same horizontal side and
    # each pin's bypass cap / pull resistor sits at its OWN Y (the original
    # CONFIG_CAP / CONTROL_RESISTOR emitters), pin-Y collisions force the
    # column-bump fallback and the parts march far outboard, colliding with
    # the rail bank. The staircase idiom places those taps in a horizontal
    # row BELOW the IC body at a shared row_y, each at its own lane_x —
    # decoupling tap Y from source pin Y so multiple control pins don't
    # crowd the same column. The wire from each IC pin to its tap is then
    # an L: horizontal at the source pin's Y (above all the tap bodies),
    # then vertical drop at lane_x to the tap pin. That horizontal-trunk-Y
    # is conveyed to the router via `PlacerResult.rail_y_hints`; without
    # it the router would route the trunk at the tap-pin Y and the wire
    # would cut through neighbouring tap bodies. Idea ported from
    # /examples/sid-experiments greedy placer (commit 996f5bc). ---------
    _STAIRCASE_CLUSTER_WINDOW = 12.70          # mm: max Y span of the cluster
    _STAIRCASE_MIN_MEMBERS = 3
    # /examples greedy fires at 2; we use 3 because at 2 the column layout
    # is almost always cleaner (fewer wires, fewer labels). At 3+ a dense
    # IC's source-pin cluster compresses enough that the column-bump
    # fallback kicks in and the staircase row outscores it.

    def _staircase_ic_pin(refdes: str) -> "PinInfo | None":
        """The IC pin a staircase-eligible single-IC-pin tap connects to.
        Returns None for parts touching 0 or 2+ IC signal pins (those are
        BOOTSTRAP or loose).

        Parts whose *other* pin sits on a non-rail / non-ground inter-part
        net (e.g. R9 in a COMP→R9→C19→GND compensation network) are
        eligible — the staircase emitter marks all such inter-part nets
        `label_only` so connectivity is by name, not by drawn wire, and
        C19 staying in CONFIG_CAP doesn't drag a long wire across the row."""
        pc = plan.parts.get(refdes)
        if pc is None or anchor is None:
            return None
        ic_pins: list = []
        for net_name in pc.nets:
            nc = plan.nets.get(net_name)
            if nc is None:
                continue
            if nc.kind in (NetKind.GROUND, NetKind.RAIL):
                continue
            ic_here = [c.pin for c in nc.ic_contacts if c.ic_refdes == anchor]
            if ic_here:
                ic_pins.extend(ic_here)
        return ic_pins[0] if len(ic_pins) == 1 else None

    # Candidates: members of CONFIG_CAP / CONTROL_RESISTOR groups whose IC
    # pin is on a vertical side (L / R). Top / bottom pins keep their
    # existing column treatment — the staircase only solves the horizontal
    # fan-out case.
    candidates: dict[str, "PinInfo"] = {}
    for g in groups:
        if g.archetype not in (Archetype.CONFIG_CAP,
                               Archetype.CONTROL_RESISTOR):
            continue
        for r in g.members:
            pi = _staircase_ic_pin(r)
            if pi is not None and pi.side in ("L", "R"):
                candidates[r] = pi

    by_side: dict[str, list[tuple[str, "PinInfo"]]] = {"L": [], "R": []}
    for r, pi in candidates.items():
        by_side[pi.side].append((r, pi))

    staircase_refs: set[str] = set()
    for side, items in by_side.items():
        if len(items) < _STAIRCASE_MIN_MEMBERS:
            continue
        # Sort bottom-most source pin first. `PinInfo.y` is in the lib frame
        # (+Y up, bottom-most = most negative Y), so ascending by `pi.y`
        # puts the bottom-most pin at items[0]. The first lane after the
        # IC body edge thus goes to the bottom-most pin, matching the
        # /examples staircase: shortest drop = closest lane, taller drops
        # fan outward. Then restrict to the bottom cluster — pins higher
        # up the side (e.g. FB mid-IC, when COMP/VCC/ILIM/SS sit much
        # lower) would force a long rail at their Y across the IC body.
        items.sort(key=lambda rp: rp[1].y)
        bottom_y = items[0][1].y
        cluster = [(r, pi) for r, pi in items
                   if pi.y - bottom_y <= _STAIRCASE_CLUSTER_WINDOW]
        if len(cluster) < _STAIRCASE_MIN_MEMBERS:
            continue
        members = [r for r, _ in cluster]
        groups.append(GroupNode(
            Archetype.SIGNAL_STAIRCASE, members, side, None,
            f"signal staircase ({side}): {', '.join(members)}",
        ))
        staircase_refs.update(members)

    # Peel staircase members out of their CONFIG_CAP / CONTROL_RESISTOR
    # origin groups so the original emitters see only what they still own.
    if staircase_refs:
        new_groups: list[GroupNode] = []
        for g in groups:
            if g.archetype in (Archetype.CONFIG_CAP,
                               Archetype.CONTROL_RESISTOR):
                remaining = [r for r in g.members if r not in staircase_refs]
                if not remaining:
                    continue
                new_groups.append(GroupNode(
                    g.archetype, remaining, g.side, g.rail, g.label,
                ))
            else:
                new_groups.append(g)
        groups = new_groups

    # --- loose: everything the rules did not group --------------------------
    loose = sorted(
        (r for r, pc in plan.parts.items()
         if r not in claimed and pc.role != Role.IC),
        key=_natural_key,
    )
    if loose:
        groups.append(GroupNode(
            Archetype.LOOSE, loose,
            _group_side(loose, plan, anchor), None, "other parts",
        ))

    return LayoutTree(
        netlist=netlist,
        plan=plan,
        anchor=anchor,
        input_rail=input_rail,
        output_rail=output_rail,
        ground=ground,
        groups=groups,
    )
