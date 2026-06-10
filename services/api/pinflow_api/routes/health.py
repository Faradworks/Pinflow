from fastapi import APIRouter

from pinflow_api import __version__

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}
