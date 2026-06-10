"""End-to-end pipeline: PDF datasheet → clipboard-ready subcircuit S-exp."""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from pinflow_api.builders._common import to_clipboard_format
from pinflow_api.datasheet_parse import ChipExtract, parse_datasheet
from pinflow_api.easyeda import detect_lcsc_codes
from pinflow_api.llm_emit import BuilderExecutionError, emit_subcircuit
from pinflow_api.symbol_resolver import resolve

router = APIRouter()

_MAX_PDF_BYTES = 50 * 1024 * 1024


@router.post("/generate")
async def generate(
    file: UploadFile = File(...),
    prompt: str = Form(""),
) -> dict:
    """Datasheet PDF + optional user prompt → emitted subcircuit.

    Pipeline:
      parse_datasheet (Claude PDF input, biased by prompt)
        → detect LCSC codes in prompt
        → resolve symbol (LCSC via easyeda2kicad → bundled-libs fallback)
        → emit_subcircuit (Claude generates kicad-sch-api Python, biased by prompt)
        → ERC repair loop
        → clipboard-format S-exp

    Returns: extract, lib_id, sexp_clipboard, builder_code, erc, source
    ("bundled" or "easyeda"). Path B install for `easyeda` source uses the
    cached _easyeda_cache file directly via lib_id.
    """
    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(status_code=400, detail=f"expected PDF, got {file.content_type}")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="empty file")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"PDF too large ({len(pdf_bytes)} bytes; max {_MAX_PDF_BYTES})",
        )

    user_prompt = (prompt or "").strip() or None

    try:
        extract: ChipExtract = parse_datasheet(pdf_bytes, user_prompt=user_prompt)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"parse: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"parse failed: {type(e).__name__}: {e}")

    lcsc_codes = detect_lcsc_codes(user_prompt)
    resolved = resolve(extract.chip, package_hint=extract.package, lcsc_codes=lcsc_codes)
    if resolved is None:
        hint = (
            " Try adding an LCSC part code (e.g. 'LCSC: C460356') to the prompt — "
            "Pinflow will fetch the symbol from EasyEDA."
            if not lcsc_codes
            else f" Tried LCSC code(s) {lcsc_codes} but easyeda2kicad fetch failed."
        )
        raise HTTPException(
            status_code=422,
            detail=f"could not resolve a KiCad library symbol for chip {extract.chip!r}.{hint}",
        )

    try:
        emitted = emit_subcircuit(
            extract,
            resolved.lib_id,
            extra_lib_path=resolved.extra_lib_path,
            user_prompt=user_prompt,
        )
    except BuilderExecutionError as e:
        raise HTTPException(
            status_code=500,
            detail=f"builder execution failed:\n{e.stderr[:1000]}",
        )

    return {
        "extract": extract.model_dump(),
        "lib_id": emitted.lib_id,
        "sexp_clipboard": to_clipboard_format(emitted.sexp),
        "builder_code": emitted.builder_code,
        "source": resolved.source,
        "erc": {
            "total": emitted.erc_total,
            "actionable": emitted.erc_actionable,
            "attempts": emitted.attempts,
        },
    }
