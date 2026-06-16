"""Headless schematic → PNG rendering.

`kicad-cli` exports SVG/PDF but never raster, and the only rendering in the
tree today is an ad-hoc `sch export pdf` duplicated inside the test scripts
that a human then opens by hand. This module is the one reusable raster path:
given a `.kicad_sch` file, a staged working copy, or raw `(kicad_sch ...)`
text, it produces a PNG screenshot with no KiCad GUI — so scripts and the
agent loop can *look at* what they emitted (visual iteration / regression
diffs / feeding a render back to the model).

Pipeline: `kicad-cli sch export pdf` → PDF → raster. `pdftoppm` (poppler) is
preferred for fidelity + a `--dpi` knob; `sips` (built into macOS) is the
zero-dependency fallback. One of the two is essentially always present on a
dev box; we raise a clear error naming the install if neither is.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Same hardcoded macOS KiCad 10 binary the rest of the tree uses. Reused
# (not re-declared) so it stays one of the *three* hardcoded paths to
# generalize for cross-platform support, not a fourth.
from pinflow_api.kicad_cli import _KCLI


class RenderError(RuntimeError):
    """Schematic could not be rendered to PNG."""


def _to_sch_file(source: str | Path, tmpd: Path) -> Path:
    """Resolve `source` to a `.kicad_sch` file on disk inside `tmpd`.

    Accepts a path (str/Path) to an existing file, or raw schematic
    S-expression text (written to a temp file). Disambiguated by whether
    the text looks like an S-expression vs. names an existing file.
    """
    if isinstance(source, Path) or "\n" not in str(source):
        p = Path(source)
        if p.is_file():
            return p
    text = str(source)
    if not text.lstrip().startswith("(kicad_sch"):
        raise RenderError(
            "source is neither an existing file nor (kicad_sch ...) text"
        )
    sch = tmpd / "subject.kicad_sch"
    sch.write_text(text)
    return sch


def _export_pdf(sch: Path, tmpd: Path, *, theme: str | None,
                black_and_white: bool, exclude_drawing_sheet: bool,
                white_background: bool, pages: str | None) -> Path:
    if not _KCLI.is_file():
        raise RenderError(f"kicad-cli not found at {_KCLI}")
    pdf = tmpd / "render.pdf"
    cmd = [str(_KCLI), "sch", "export", "pdf", "-o", str(pdf)]
    if exclude_drawing_sheet:
        cmd.append("-e")  # tighter content, less A4 whitespace in the raster
    if black_and_white:
        cmd.append("-b")
    if white_background:
        # Drop KiCad's cream theme fill; pdftoppm then rasters the empty
        # background as white.
        cmd.append("-n")
    if theme:
        cmd += ["-t", theme]
    if pages:
        cmd += ["--pages", pages]
    cmd.append(str(sch))
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if not pdf.is_file():
        raise RenderError(
            f"kicad-cli pdf export failed (exit {cp.returncode}):\n"
            f"{cp.stderr or cp.stdout}"
        )
    return pdf


def _pdf_to_png(pdf: Path, out_path: Path, *, dpi: int,
                crop_px: tuple[int, int, int, int] | None = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("pdftoppm"):
        # -singlefile drops the "-1" page suffix and writes "<prefix>.png".
        # Build prefix/produced by string-stripping, not Path.with_suffix —
        # the latter mangles a multi-dot name (a.b.c.png → prefix a.b.c →
        # with_suffix(".png") → a.b.png, a file pdftoppm never wrote).
        name = out_path.name
        stem = name[:-4] if name.lower().endswith(".png") else name
        prefix = out_path.parent / stem
        crop = []
        if crop_px is not None:
            x, y, w, h = crop_px
            crop = ["-x", str(x), "-y", str(y), "-W", str(w), "-H", str(h)]
        cp = subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-singlefile",
             *crop, str(pdf), str(prefix)],
            capture_output=True, text=True, timeout=60,
        )
        produced = out_path.parent / f"{stem}.png"
        if produced.is_file():
            if produced != out_path:
                produced.replace(out_path)
            return
        raise RenderError(
            f"pdftoppm failed (exit {cp.returncode}):\n{cp.stderr or cp.stdout}"
        )

    if shutil.which("sips"):  # macOS built-in; ignores dpi, rasters at native
        print(
            "render: WARNING — pdftoppm not found; falling back to sips, "
            "which rasters the full page at fixed ~72dpi (no --dpi, no crop). "
            "Install poppler for high-fidelity renders: brew install poppler",
            file=sys.stderr,
        )
        cp = subprocess.run(
            ["sips", "-s", "format", "png", str(pdf), "--out", str(out_path)],
            capture_output=True, text=True, timeout=60,
        )
        if out_path.is_file():
            return
        raise RenderError(
            f"sips failed (exit {cp.returncode}):\n{cp.stderr or cp.stdout}"
        )

    raise RenderError(
        "no PDF rasterizer found — install poppler (`brew install poppler`, "
        "gives pdftoppm) or run on macOS (sips)"
    )


def _crop_px(sch: Path, dpi: int, margin_mm: float
             ) -> tuple[int, int, int, int] | None:
    """Pixel crop rect tight to drawn content, or None if it can't be found.

    KiCad exports the schematic 1:1 onto its page, so a point at (x, y) mm
    is at (x·dpi/25.4, y·dpi/25.4) px from the raster's top-left (verified
    empirically: A4 → 1754×1240 @150dpi). We take `content_bbox` in mm,
    pad by `margin_mm`, and convert.
    """
    try:
        import kicad_sch_api as ksa

        from pinflow_api.emit.bbox import content_bbox
        s = ksa.load_schematic(str(sch))
        bb = content_bbox(s, sch.read_text())
    except Exception:
        return None
    if bb is None:
        return None
    k = dpi / 25.4
    x0 = max(0.0, bb[0] - margin_mm)
    y0 = max(0.0, bb[1] - margin_mm)
    x1 = bb[2] + margin_mm
    y1 = bb[3] + margin_mm
    w = round((x1 - x0) * k)
    h = round((y1 - y0) * k)
    if w <= 0 or h <= 0:
        return None
    return (round(x0 * k), round(y0 * k), w, h)


def render_schematic(
    source: str | Path,
    out_path: str | Path,
    *,
    dpi: int = 300,
    theme: str | None = None,
    black_and_white: bool = False,
    exclude_drawing_sheet: bool = True,
    white_background: bool = False,
    crop: bool = True,
    margin_mm: float = 4.0,
    pages: str | None = None,
) -> Path:
    """Render a schematic to a PNG at `out_path`. Returns the written path.

    `source` is a path to a `.kicad_sch` file *or* raw `(kicad_sch ...)`
    text (so callers can render a staging working copy without spilling it
    themselves). `dpi` and `crop` only take effect on the pdftoppm path
    (the `sips` fallback renders the full page). `crop` trims the raster to
    drawn content + `margin_mm` so a small subcircuit isn't lost in A4
    whitespace — the point of a screenshot fed back to a model.
    """
    out = Path(out_path)
    with tempfile.TemporaryDirectory(prefix="pinflow_render_") as td:
        tmpd = Path(td)
        sch = _to_sch_file(source, tmpd)
        pdf = _export_pdf(
            sch, tmpd, theme=theme, black_and_white=black_and_white,
            exclude_drawing_sheet=exclude_drawing_sheet,
            white_background=white_background, pages=pages,
        )
        crop_px = _crop_px(sch, dpi, margin_mm) if crop else None
        _pdf_to_png(pdf, out, dpi=dpi, crop_px=crop_px)
    return out


def render_schematic_bytes(source: str | Path, **kw) -> bytes:
    """`render_schematic` returning PNG bytes (for in-process/agent use)."""
    with tempfile.TemporaryDirectory(prefix="pinflow_render_") as td:
        out = render_schematic(source, Path(td) / "out.png", **kw)
        return out.read_bytes()
