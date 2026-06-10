from fastapi import APIRouter, File, HTTPException, UploadFile

from pinflow_api.datasheet_parse import ChipExtract, parse_datasheet

router = APIRouter()

_MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB


@router.post("/parse")
async def parse(file: UploadFile = File(...)) -> dict:
    """Accept a datasheet PDF, return a structured ChipExtract via Claude.

    Form field: `file` (multipart/form-data, application/pdf).
    """
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=400, detail=f"expected PDF, got {file.content_type}"
        )
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="empty file")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF too large ({len(pdf_bytes)} bytes; max {_MAX_PDF_BYTES})",
        )
    try:
        extract: ChipExtract = parse_datasheet(pdf_bytes)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"parse failed: {type(e).__name__}: {e}")
    return {"extract": extract.model_dump()}
