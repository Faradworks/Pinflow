"""Render a `DesignGraph` + cached MPN profiles as the LLM-facing digest.

The per-IC block is what makes the agent able to reason about a specific
chip's neighborhood — bridges between pins, decoupling caps, power-rail
membership. A whole-schematic wrapper concatenates per-IC blocks under a
header for `read_active_schematic`.
"""

from __future__ import annotations

import re

from pinflow_api.graph.models import (
    ComponentType,
    DesignGraph,
    NetType,
)
from pinflow_api.profile import ComponentProfile

# Power/ground nets with more than this many connections collapse to a
# summary line in the per-pin listing.
_GROUND_NET_MAX_COMPONENTS = 5


# ---------------------------------------------------------------------------
# Pin / thermal-pad helpers
# ---------------------------------------------------------------------------


def _pin_sort_key(pin: str) -> tuple:
    m = re.match(r"^(\d+)", pin)
    if m:
        return (0, int(m.group(1)), pin)
    return (1, 0, pin)


_THERMAL_PAD_NAME_RE = re.compile(
    r"\b(e[\s\-]?pad|epad|ep|dap|thermal\s*pad|exposed\s*(?:pad|paddle)|die[\s\-]?(?:attach\s*)?pad)\b",
    re.IGNORECASE,
)


def _is_thermal_pad_pin(pin) -> bool:
    """Heuristic: does a pintable entry describe the exposed/thermal pad?"""
    for field in (getattr(pin, "name", None), getattr(pin, "description", None)):
        if field and _THERMAL_PAD_NAME_RE.search(str(field)):
            return True
    number = str(getattr(pin, "number", "")).strip()
    if number and not number.isdigit() and _THERMAL_PAD_NAME_RE.search(number):
        return True
    return False


def _profile_for(graph: DesignGraph, ref: str, profiles_by_mpn: dict[str, ComponentProfile]) -> ComponentProfile | None:
    comp = graph.components.get(ref)
    if not comp or not comp.mpn:
        return None
    return profiles_by_mpn.get(comp.mpn)


# ---------------------------------------------------------------------------
# Per-IC context block
# ---------------------------------------------------------------------------


def build_ic_context(
    graph: DesignGraph,
    profiles_by_mpn: dict[str, ComponentProfile],
    ref: str,
) -> str:
    """Build a text summary of one IC's neighborhood.

    Shows every pin, its net, and every component connected to that net
    (with values). Power/ground nets with many connections are summarized.
    """
    comp = graph.components.get(ref)
    if not comp:
        return f"Component '{ref}' not found in design graph."

    profile = _profile_for(graph, ref, profiles_by_mpn)
    lines: list[str] = []

    # --- Header ------------------------------------------------------------
    lines.append(f"Component: {ref} ({comp.mpn or comp.value})")
    # lib_id is the installed symbol id — surfacing it lets a replicate/edit
    # flow reuse the exact symbol directly instead of spending a turn on
    # search_symbols to rediscover it.
    if comp.lib_id:
        lines.append(f"lib_id: {comp.lib_id}")
    if profile:
        if profile.manufacturer:
            lines.append(f"Manufacturer: {profile.manufacturer}")
        if profile.description:
            lines.append(f"Description: {profile.description}")
        if profile.package:
            lines.append(f"Package: {profile.package}")
    elif comp.mpn:
        lines.append("Profile: (not yet extracted — call get_component_profile)")
    lines.append("")

    # --- Build pin list ----------------------------------------------------
    # Each entry: (pin_num, pin_name|None, net_name|None, note|None)
    pin_entries: list[tuple[str, str | None, str | None, str | None]] = []
    matched_schematic_pins: set[str] = set()
    unmatched_ep_entries: list = []

    if profile and profile.chosen_pintable:
        for p in sorted(profile.chosen_pintable, key=lambda x: _pin_sort_key(str(x.number))):
            net_name = comp.pins.get(str(p.number))
            if net_name is not None:
                matched_schematic_pins.add(str(p.number))
                pin_entries.append((str(p.number), p.name, net_name, None))
            elif _is_thermal_pad_pin(p):
                unmatched_ep_entries.append(p)
            else:
                pin_entries.append((str(p.number), p.name, None, None))
    else:
        for pn in sorted(comp.pins.keys(), key=_pin_sort_key):
            matched_schematic_pins.add(pn)
            pin_entries.append((pn, None, comp.pins[pn], None))

    # Orphan schematic pins (present in netlist but not in profile pintable).
    # Commonly the EP/thermal pad under a user-chosen pin number.
    orphan_pins = [pn for pn in comp.pins if pn not in matched_schematic_pins]
    orphan_pins.sort(key=_pin_sort_key)

    fused_ep_note = (
        "exposed pad / thermal pad — datasheet pintable lists this "
        "without a usable pin number; matched to orphan schematic pin"
    )
    if len(unmatched_ep_entries) == 1 and len(orphan_pins) == 1:
        ep_row = unmatched_ep_entries[0]
        orphan_pin = orphan_pins[0]
        pin_entries.append((orphan_pin, ep_row.name, comp.pins[orphan_pin], fused_ep_note))
        unmatched_ep_entries = []
        orphan_pins = []

    # --- Per-pin neighborhood listing -------------------------------------
    seen_nets: set[str] = set()

    for pin_num, pin_name, net_name, note in pin_entries:
        name_str = f" ({pin_name})" if pin_name else ""
        note_str = f"  [{note}]" if note else ""

        if not net_name:
            lines.append(f"Pin {pin_num}{name_str} → [unconnected]{note_str}")
            lines.append("")
            continue

        net = graph.nets.get(net_name)
        if not net:
            lines.append(f"Pin {pin_num}{name_str} → {net_name}")
            lines.append("")
            continue

        voltage_str = f", {net.voltage}V" if net.voltage is not None else ""
        lines.append(
            f"Pin {pin_num}{name_str} → {net_name} "
            f"[{net.net_type.value}{voltage_str}]{note_str}"
        )

        if net_name in seen_nets:
            lines.append("  (same net as above)")
            lines.append("")
            continue
        seen_nets.add(net_name)

        neighbors = [
            pc for pc in net.pins
            if pc.component_ref != ref and pc.component_ref in graph.components
        ]

        # Summarize large ground/power nets. Count unique COMPONENTS, not
        # pin attachments — a connector grounding through 5 pins is one
        # component, and "5 connectors" misleads the model into inventing
        # hardware that doesn't exist.
        if len(neighbors) > _GROUND_NET_MAX_COMPONENTS and net.net_type in (NetType.GROUND, NetType.POWER):
            by_type: dict[str, set[str]] = {}
            for pc in neighbors:
                nb = graph.components[pc.component_ref]
                by_type.setdefault(nb.component_type.value, set()).add(pc.component_ref)
            parts = [
                f"{len(refs)} {ctype}{'s' if len(refs) > 1 else ''}"
                for ctype, refs in sorted(by_type.items())
            ]
            n_unique = len({pc.component_ref for pc in neighbors})
            lines.append(f"  {n_unique} components on this net: {', '.join(parts)}")
            # List ICs specifically — too important to summarize away.
            for pc in neighbors:
                nb = graph.components[pc.component_ref]
                if nb.component_type == ComponentType.IC:
                    pin_name_str = ""
                    nb_profile = _profile_for(graph, nb.reference, profiles_by_mpn)
                    if nb_profile:
                        for p in nb_profile.chosen_pintable:
                            if str(p.number) == str(pc.pin_number):
                                pin_name_str = f" ({p.name})"
                                break
                    lines.append(f"  {nb.reference}: {nb.mpn or nb.value} [pin {pc.pin_number}{pin_name_str}]")
        else:
            for pc in neighbors:
                nb = graph.components[pc.component_ref]
                mpn_str = f", {nb.mpn}" if nb.mpn else ""
                pin_name_str = ""
                nb_profile = _profile_for(graph, nb.reference, profiles_by_mpn)
                if nb_profile:
                    for p in nb_profile.chosen_pintable:
                        if str(p.number) == str(pc.pin_number):
                            pin_name_str = f" ({p.name})"
                            break
                lines.append(
                    f"  {nb.reference}: {nb.value}{mpn_str}"
                    f" [pin {pc.pin_number}{pin_name_str}]"
                )

        lines.append("")

    # --- Bridges: components spanning two of this IC's nets ----------------
    pin_name_by_num = {pn: pname for pn, pname, _, _ in pin_entries if pname}
    ic_net_to_pins: dict[str, list[str]] = {}
    for ic_pin, ic_net in comp.pins.items():
        if ic_net:
            ic_net_to_pins.setdefault(ic_net, []).append(ic_pin)
    ic_nets_set = set(ic_net_to_pins.keys())

    def _label_endpoint(net: str) -> str:
        pins = sorted(ic_net_to_pins.get(net, []), key=_pin_sort_key)
        prefix = "pins" if len(pins) > 1 else "pin"
        names = [pin_name_by_num.get(p) for p in pins]
        named = [n for n in names if n]
        if named:
            unique = list(dict.fromkeys(named))
            return f"{prefix} {'/'.join(pins)} ({'/'.join(unique)}, {net})"
        return f"{prefix} {'/'.join(pins)} ({net})"

    def _is_signal_net(name: str) -> bool:
        n = graph.nets.get(name)
        return bool(n and n.net_type == NetType.SIGNAL)

    bridge_lines: list[str] = []
    skipped_power_only = 0
    for nb_ref, nb in graph.components.items():
        if nb_ref == ref:
            continue
        nets_touched = {n for n in nb.pins.values() if n in ic_nets_set}
        if len(nets_touched) < 2:
            continue
        if not any(_is_signal_net(n) for n in nets_touched):
            skipped_power_only += 1
            continue
        nets_sorted = sorted(nets_touched)
        endpoints = " ↔ ".join(_label_endpoint(n) for n in nets_sorted)
        mpn_str = f", {nb.mpn}" if nb.mpn else ""
        value_str = nb.value if nb.value else nb.component_type.value
        bridge_lines.append(f"  {nb.reference}: {value_str}{mpn_str} — bridges {endpoints}")

    if bridge_lines or skipped_power_only:
        lines.append(f"Bridges between {ref}'s pins (signal-bearing only):")
        if bridge_lines:
            bridge_lines.sort()
            lines.extend(bridge_lines)
        if skipped_power_only:
            lines.append(
                f"  ({skipped_power_only} additional bypass/rail-sharing bridges "
                f"between power & ground nets, omitted — see per-pin listing for counts)"
            )
        lines.append("")

    # --- Orphan schematic pins / unmatched EP rows ------------------------
    if orphan_pins or unmatched_ep_entries:
        lines.append("Additional schematic pins (not in datasheet pintable):")
        if unmatched_ep_entries:
            ep_names = ", ".join(
                f"{p.name} (pintable #{p.number})" for p in unmatched_ep_entries
            )
            lines.append(
                f"  (datasheet pintable lists these without a schematic-matched "
                f"pin number — likely the exposed pad: {ep_names})"
            )
        for pn in orphan_pins:
            net_name = comp.pins.get(pn)
            if not net_name:
                continue
            net = graph.nets.get(net_name)
            if net is None:
                lines.append(f"  Pin {pn} → {net_name}")
                continue
            voltage_str = f", {net.voltage}V" if net.voltage is not None else ""
            lines.append(f"  Pin {pn} → {net_name} [{net.net_type.value}{voltage_str}]")
        if not orphan_pins and unmatched_ep_entries:
            lines.append(
                "  (no matching orphan schematic pin found — the EP may be "
                "genuinely unconnected in the schematic)"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Whole-schematic digest
# ---------------------------------------------------------------------------


def build_digest(
    graph: DesignGraph,
    profiles_by_mpn: dict[str, ComponentProfile] | None = None,
    *,
    project_name: str | None = None,
    schematic_basename: str | None = None,
    stage_stale: bool = False,
) -> str:
    """Render the whole-schematic digest: header + per-IC blocks.

    `profiles_by_mpn` is optional — without profiles, IC blocks show pin
    numbers but no names, and the "Profile: (not yet extracted — call
    get_component_profile)" hint surfaces so the agent knows what to do.

    `stage_stale` adds a warning line when the user saved in KiCad after
    the agent staged an edit — the staged working copy no longer reflects
    ground truth and the model should discuss/discard it.
    """
    profiles_by_mpn = profiles_by_mpn or {}
    lines: list[str] = []

    # Top-of-digest header
    if project_name or schematic_basename:
        ident = " / ".join(p for p in (project_name, schematic_basename) if p)
        lines.append(f"Active schematic: {ident}")
    if stage_stale:
        lines.append(
            "Stage: STALE — the user saved in KiCad after our last staged edit. "
            "The staged working copy no longer matches ground truth; consider "
            "discard_edit before making further changes."
        )
    by_type: dict[str, int] = {}
    for c in graph.components.values():
        by_type[c.component_type.value] = by_type.get(c.component_type.value, 0) + 1
    parts = [f"{n} {t}{'s' if n != 1 else ''}" for t, n in sorted(by_type.items())]
    lines.append(f"Components ({len(graph.components)} total): {', '.join(parts)}")

    rails = sorted(
        graph.power_nets(),
        key=lambda n: (n.net_type.value, n.name),
    )
    if rails:
        rail_descs = []
        for r in rails:
            v = f"{r.voltage}V" if r.voltage is not None else "?"
            rail_descs.append(f"{r.name}({v}, {len(r.pins)} pins)")
        lines.append(f"Power/ground rails: {', '.join(rail_descs)}")
    else:
        lines.append("Power/ground rails: (none recognized — nets may be auto-named)")
    lines.append("")

    # ICs with unresolved MPN — important hint for the agent.
    ic_refs = graph.components_by_type(ComponentType.IC)
    unresolved = [r for r in ic_refs if not graph.components[r].mpn]
    if unresolved:
        lines.append(f"ICs missing MPN (call resolve_mpn): {', '.join(unresolved)}")
        lines.append("")

    # Per-IC blocks
    for ref in ic_refs:
        lines.append("=" * 60)
        lines.append(build_ic_context(graph, profiles_by_mpn, ref))
        lines.append("")

    # Connector blocks. Without these, anything attached only to a
    # connector's signal pins (USB-C CC pulldowns, ESD parts on D+/D-) is
    # INVISIBLE to the model — it sees the count in the header, then denies
    # the parts exist when asked to edit them. build_ic_context is
    # component-agnostic: connectors simply have no MPN profile, so pins
    # render by number with their nets and neighbors.
    for ref in graph.components_by_type(ComponentType.CONNECTOR):
        lines.append("=" * 60)
        lines.append(build_ic_context(graph, profiles_by_mpn, ref))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
