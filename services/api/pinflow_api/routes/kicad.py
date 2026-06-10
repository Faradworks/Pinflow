from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from typing import Optional

from pinflow_api import staging
from pinflow_api.builders import lib_id_for
from pinflow_api.kicad_detect import detect
from pinflow_api.sym_lib import (
    build_pinflow_lib,
    extract_symbol_text,
    merge_symbol_into_lib,
)
from pinflow_api.sym_lib_table import ensure_pinflow_entry

router = APIRouter(prefix="/kicad")


@router.get("/active-project")
def active_project() -> dict:
    """Detect the KiCad project currently open on this machine.

    Returns {detected: false} if none. Otherwise
    {detected: true, name, schematic, path, schematic_source, schematic_path,
     staged, stage_stale}.

    `path` may be null even when detected — IPC tells us the project name
    reliably, but resolving the directory depends on lsof.

    `schematic_path` is the absolute path of the active .kicad_sch when known
    (null if either `path` or `schematic` is missing). Callers pass this back
    to `/schematic/*` endpoints to address the staging slot.

    `schematic_source` is the *staged* working copy if a stage exists for that
    file, otherwise the real file's contents. When staged it carries
    preview-only highlight rectangles around new/changed content (the clean
    working copy is what commit writes — the highlight never reaches disk).
    `staged` distinguishes staged from real; `highlighted` is true when the
    served source actually has highlights injected; `stage_stale` is true if
    the real file's mtime advanced since the stage was created (the user
    likely saved in KiCad — viewer should warn).
    """
    proj = detect()
    if proj is None:
        return {"detected": False}

    sch_path = _resolve_schematic_path(proj.path, proj.schematic)
    source: Optional[str] = None
    staged = False
    stale = False
    highlighted = False
    if sch_path is not None:
        existing = staging.get(sch_path)
        if existing is not None:
            # Serve the highlighted preview (block + per-component outlines
            # around new/changed content) for display. The clean working copy
            # is what commit writes — the highlight never reaches disk.
            preview = staging.preview_source(sch_path)
            source = preview if preview is not None else existing.working_copy
            staged = True
            stale = existing.is_stale()
            highlighted = preview is not None and preview != existing.working_copy
        else:
            source = _read_text_safe(sch_path)

    return {
        "detected": True,
        **proj.model_dump(),
        "schematic_path": str(sch_path) if sch_path else None,
        "schematic_source": source,
        "staged": staged,
        "stage_stale": stale,
        "highlighted": highlighted,
    }


def _resolve_schematic_path(project_path: Optional[str], schematic: str) -> Optional[Path]:
    if not project_path or not schematic:
        return None
    return Path(project_path).parent / schematic


def _read_text_safe(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except (OSError, UnicodeDecodeError):
        return None


class InstallSymbolRequest(BaseModel):
    project_path: str  # full path to .kicad_pro
    chip_id: Optional[str] = None  # one of the static cohort (CHIPS dict)
    lib_id: Optional[str] = None  # OR a fully-qualified KiCad lib_id (e.g. from /generate)


@router.post("/install-symbol-to-project")
def install_symbol_to_project(req: InstallSymbolRequest) -> dict:
    """Write/merge the chip's symbol into <project_dir>/pinflow.kicad_sym and
    register the library in <project_dir>/sym-lib-table.

    Accepts either `chip_id` (looks up the static cohort) or `lib_id` (any
    KiCad bundled symbol — used by /generate's dynamic flow).

    Stays local always (filesystem write) — does NOT cloud-lift with /chips.
    """
    pro = Path(req.project_path)
    if not pro.is_file() or pro.suffix != ".kicad_pro":
        raise HTTPException(
            status_code=400, detail=f"project_path is not a .kicad_pro file: {req.project_path}"
        )
    project_dir = pro.parent
    pinflow_lib = project_dir / "pinflow.kicad_sym"
    table = project_dir / "sym-lib-table"

    if req.lib_id:
        lib_id = req.lib_id
    elif req.chip_id:
        try:
            lib_id = lib_id_for(req.chip_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown chip: {req.chip_id}")
    else:
        raise HTTPException(status_code=400, detail="provide either chip_id or lib_id")

    try:
        new_symbol = extract_symbol_text(lib_id)
    except (FileNotFoundError, KeyError, ValueError) as e:
        raise HTTPException(status_code=500, detail=f"symbol extraction failed: {e}")

    if pinflow_lib.is_file():
        merged = merge_symbol_into_lib(pinflow_lib.read_text(), new_symbol)
        action_lib = "merged"
    else:
        merged = build_pinflow_lib([new_symbol])
        action_lib = "created"
    pinflow_lib.write_text(merged)

    action_table = ensure_pinflow_entry(table)

    return {
        "symbol_lib_path": str(pinflow_lib),
        "sym_lib_table_path": str(table),
        "lib_action": action_lib,
        "table_action": action_table,
    }
