from fastapi import APIRouter, HTTPException

from pinflow_api.builders import build_subcircuit, list_chips
from pinflow_api.builders._common import to_clipboard_format

router = APIRouter(prefix="/chips")


@router.get("")
def chips() -> list[dict]:
    return list_chips()


@router.get("/{chip_id}/subcircuit")
def subcircuit(chip_id: str) -> dict:
    """Returns eeschema-clipboard-format S-exp ready for ⌘V into an open schematic.

    NOT a full `(kicad_sch ...)` document — eeschema's paste handler expects the
    bare inner forms (lib_symbols, placed symbols, wires, labels, ...).
    """
    try:
        full_sch = build_subcircuit(chip_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown chip: {chip_id}")
    return {"sexp": to_clipboard_format(full_sch)}
