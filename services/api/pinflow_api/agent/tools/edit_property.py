"""Tool: edit_property.

Narrow, single-purpose mutation for the active schematic:
- `{type:"component", refdes:"R5"}` — set a component's value / footprint /
  arbitrary property.
- `{type:"net", name:"+3V3"}` — rename a net by walking every label-bearing
  form (local labels, hierarchical labels, and the value field on power
  symbols sitting on the net).

Power-symbol semantics: a net's KiCad-visible name is owed both to its
labels AND to any `power:*` symbol connected to it. The hardcoded power
symbols (`power:+3V3`, `power:GND`, etc.) bake the rail name into their
lib_id, so renaming the value alone leaves a visual inconsistency between
the symbol body and the (now renamed) net. The cleanest fix is to swap
the symbol's lib_id to a matching one when the rail name has a known
mapping, otherwise fall back to `power:VCC` (which carries the rail name
purely in its value field). The user sees the schematic update cleanly in
KiCad either way.

After every mutation: serialize → `staging.update` → refresh the design
graph so subsequent turns see the rename.
"""

from __future__ import annotations

from pinflow_api import staging
from pinflow_api.agent.schematic_sync import load_active_schematic
from pinflow_api.agent.tools._subcircuit_common import load_target_schematic
from pinflow_api.builders._common import sch_to_string

# Standard rail-name → power-library symbol. Kept in sync with the placer's
# `_POWER_LIB_IDS` (emit/netlist_to_sch.py). When the new rail name lands in
# this dict we swap the power symbol's lib_id to the matching one. Otherwise
# we fall back to `power:VCC` which uses its value field as the rail name.
_RAIL_TO_LIB_ID: dict[str, str] = {
    "GND": "power:GND",
    "AGND": "power:GNDA",
    "DGND": "power:GNDD",
    "PGND": "power:GNDPWR",
    "+3V3": "power:+3V3",
    "+3.3V": "power:+3V3",
    "+5V": "power:+5V",
    "+12V": "power:+12V",
    "+1V8": "power:+1V8",
    "VCC": "power:VCC",
    "VDD": "power:VDD",
}


SCHEMA = {
    "name": "edit_property",
    "description": (
        "Set a property on a component or rename a net in the staged "
        "schematic. Targets: {type:'component', refdes:'R5'} with "
        "key='value'|'footprint'|<any property name>; or {type:'net', "
        "name:'+3V3'} with key='name' and value=new net name. Net renames "
        "also update power symbols on the net. After this tool succeeds, "
        "follow up with ask_user(['Confirm','Discard']) then "
        "commit_edit / discard_edit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "object",
                "description": (
                    "{type:'component'|'net', refdes?:str (for components), "
                    "name?:str (for nets)}"
                ),
                "properties": {
                    "type": {"type": "string", "enum": ["component", "net"]},
                    "refdes": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["type"],
            },
            "key": {
                "type": "string",
                "description": (
                    "Property name. For components: 'value', 'footprint', or "
                    "any property name (MPN, Manufacturer, etc.). For nets: "
                    "must be 'name'."
                ),
            },
            "value": {"type": "string", "description": "New value."},
        },
        "required": ["target", "key", "value"],
    },
}


def _edit_component(
    sch, refdes: str, key: str, value: str
) -> tuple[bool, str]:
    """Apply a property edit. Returns (ok, message)."""
    comp = sch.components.get(refdes)
    if comp is None:
        return False, f"no component with refdes {refdes!r}"

    key_lower = key.lower()
    if key_lower == "value":
        comp.value = value
    elif key_lower == "footprint":
        comp.footprint = value
    else:
        # Anything else (MPN, Manufacturer, Description, etc.) goes through
        # set_property, which adds or updates the named field.
        try:
            comp.set_property(key, value)
        except Exception as e:
            return False, f"set_property({key!r}) failed: {e}"
    return True, f"set {key}={value!r} on {refdes}"


def _rename_net(sch, old_name: str, new_name: str) -> tuple[int, list[str]]:
    """Rename every occurrence of `old_name` to `new_name` in the schematic.

    Returns (count_of_changes, warnings).
    """
    changes = 0
    warnings: list[str] = []

    # Local labels.
    for lab in list(sch.labels):
        if lab.text == old_name:
            lab.text = new_name
            changes += 1

    # Hierarchical labels (present in hierarchical sheets — usually empty).
    for hl in list(getattr(sch, "hierarchical_labels", []) or []):
        if hl.text == old_name:
            hl.text = new_name
            changes += 1

    # Power symbols: their value field carries the rail name. For hardcoded
    # `power:<rail>` lib_ids we swap to a matching lib_id from the table;
    # otherwise we drop in `power:VCC` whose value is the rail name. Either
    # way we remove-and-re-add at the same position to avoid leaving stale
    # lib_id references that don't match the new rail name.
    target_lib_id = _RAIL_TO_LIB_ID.get(new_name, "power:VCC")

    to_replace: list[tuple[str, str, str, tuple[float, float], float]] = []
    for comp in list(sch.components):
        if not comp.lib_id.startswith("power:"):
            continue
        # Match by value (canonical) OR by lib_id encoding the old rail.
        matches = (
            comp.value == old_name
            or comp.lib_id == _RAIL_TO_LIB_ID.get(old_name)
        )
        if not matches:
            continue
        pos = (comp.position.x, comp.position.y)
        to_replace.append((comp.reference, comp.uuid, comp.lib_id, pos, comp.rotation))

    for ref, uuid, old_lib_id, pos, rotation in to_replace:
        if old_lib_id == target_lib_id:
            # Same lib_id; just update the value attribute.
            comp = sch.components.get(ref)
            if comp is not None:
                comp.value = new_name
                changes += 1
            continue
        try:
            sch.components.remove_by_uuid(uuid)
            sch.components.add(
                lib_id=target_lib_id,
                reference=ref,
                value=new_name,
                position=pos,
                rotation=rotation,
            )
            changes += 1
            if target_lib_id == "power:VCC":
                warnings.append(
                    f"replaced {ref} ({old_lib_id}) with power:VCC "
                    f"(value={new_name!r}) — verify visual in KiCad"
                )
        except Exception as e:
            warnings.append(
                f"could not swap power symbol {ref} ({old_lib_id} → "
                f"{target_lib_id}): {e}"
            )

    return changes, warnings


def run(
    state,
    target: dict | None = None,
    key: str | None = None,
    value: str | None = None,
    **_,
) -> dict:
    if not target or not isinstance(target, dict):
        return {"status": "missing_input", "hint": "target is required."}
    if key is None:
        return {"status": "missing_input", "hint": "key is required."}
    if value is None:
        return {"status": "missing_input", "hint": "value is required."}
    if state.active_sch_path is None:
        return {
            "status": "no_active_schematic",
            "hint": "Call read_active_schematic before edit_property.",
        }

    t = target.get("type")
    try:
        sch, target_source_text = load_target_schematic(state.active_sch_path)
    except Exception as e:
        return {"status": "load_failed", "error": f"{type(e).__name__}: {e}"}

    if t == "component":
        refdes = target.get("refdes")
        if not refdes:
            return {
                "status": "missing_input",
                "hint": "target.refdes is required for component edits.",
            }
        ok, msg = _edit_component(sch, refdes, key, str(value))
        if not ok:
            return {"status": "edit_failed", "hint": msg}
        summary = msg
        warnings: list[str] = []

    elif t == "net":
        if key.lower() != "name":
            return {
                "status": "bad_input",
                "hint": (
                    "For net targets, key must be 'name'. Use a component "
                    "target to edit anything else."
                ),
            }
        old_name = target.get("name")
        if not old_name:
            return {
                "status": "missing_input",
                "hint": "target.name is required for net renames.",
            }
        changes, warnings = _rename_net(sch, old_name, str(value))
        if changes == 0:
            return {
                "status": "unknown_net",
                "hint": (
                    f"no labels or power symbols matched {old_name!r}. "
                    "Check the digest for the exact rail name."
                ),
            }
        summary = f"renamed {changes} occurrence(s) of {old_name!r} → {value!r}"

    else:
        return {
            "status": "bad_input",
            "hint": f"unknown target.type {t!r}; expected 'component' or 'net'.",
        }

    try:
        merged_text = sch_to_string(
            sch, preserve_lib_symbols_from=target_source_text
        )
    except Exception as e:
        return {"status": "serialize_failed", "error": f"{type(e).__name__}: {e}"}

    staging.update(state.active_sch_path, merged_text)
    # Record the touched component so the viewer outlines just it (preview-
    # only). Net renames relabel rails rather than a part — nothing to box.
    if t == "component":
        staging.add_changed_refs(state.active_sch_path, [target.get("refdes")])
    # Refresh the design graph so subsequent turns see the rename.
    load_active_schematic(state)

    return {
        "status": "ok",
        "summary": summary,
        "warnings": warnings,
        "diff_available": True,
    }
