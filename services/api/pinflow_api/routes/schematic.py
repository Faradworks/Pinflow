from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pinflow_api import staging

router = APIRouter(prefix="/schematic")


class SchematicPathBody(BaseModel):
    schematic_path: str


class UpdateBody(SchematicPathBody):
    source: str


class CommitBody(SchematicPathBody):
    force: bool = False


def _stage_payload(s: staging.StagedSchematic) -> dict:
    return {
        "schematic_path": str(s.schematic_path),
        "source": s.working_copy,
        "stale": s.is_stale(),
    }


@router.post("/stage")
def stage(body: SchematicPathBody) -> dict:
    try:
        s = staging.stage(Path(body.schematic_path))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _stage_payload(s)


@router.post("/update")
def update(body: UpdateBody) -> dict:
    try:
        s = staging.update(Path(body.schematic_path), body.source)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _stage_payload(s)


@router.post("/commit")
def commit(body: CommitBody) -> dict:
    try:
        r = staging.commit(Path(body.schematic_path), force=body.force)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except staging.StaleStageError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"file_written": r.file_written}


@router.post("/discard")
def discard(body: SchematicPathBody) -> dict:
    return {"discarded": staging.discard(Path(body.schematic_path))}


@router.get("/diff")
def diff(schematic_path: str) -> dict:
    d = staging.diff(Path(schematic_path))
    if d is None:
        return {"diff": None, "has_changes": False}
    return {"diff": d, "has_changes": bool(d.strip())}
