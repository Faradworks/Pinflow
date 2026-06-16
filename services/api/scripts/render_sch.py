"""Render a KiCad schematic to a PNG screenshot — the iteration-automation tool.

No KiCad GUI, no human in the loop: point it at a file, the live KiCad
project, a staged working copy, or piped S-expression text, and get a PNG.

    cd services/api
    .venv/bin/python scripts/render_sch.py board.kicad_sch              # -> _renders/<name>.png
    .venv/bin/python scripts/render_sch.py board.kicad_sch -o shot.png --dpi 300 --open
    .venv/bin/python scripts/render_sch.py --active                     # whatever KiCad has open
    .venv/bin/python scripts/render_sch.py --active --staged            # its staged working copy
    .venv/bin/python scripts/render_sch.py --stage /abs/board.kicad_sch # staged copy of a known path
    cat some.kicad_sch | .venv/bin/python scripts/render_sch.py -       # stdin S-exp

`--active` resolves via the same KiCad detection the app uses; with
`--staged` it renders the agent's in-memory working copy instead of the
on-disk file (the whole point — see what the agent staged before commit).
Exit 0 on success; the PNG path is printed last so callers can capture it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from pinflow_api import kicad_detect, staging
from pinflow_api.emit.render import RenderError, render_schematic

# Default output dir: services/api/_renders/ (gitignored, alongside the
# other _<name>/ scratch dirs — _traces, _components_cache). __file__ is
# services/api/scripts/render_sch.py, so parent.parent is services/api.
_RENDERS = Path(__file__).resolve().parent.parent / "_renders"


def _resolve_active(use_staged: bool) -> tuple[str | Path, str]:
    """Return (source, label) for the live KiCad project's active schematic."""
    proj = kicad_detect.detect()
    if proj is None:
        sys.exit("no KiCad project detected (is KiCad open with IPC enabled?)")
    if not proj.path or not proj.schematic:
        sys.exit(
            f"detected project {proj.name!r} but could not resolve its "
            f"schematic path (path={proj.path}, schematic={proj.schematic})"
        )
    sch_path = (Path(proj.path).parent / proj.schematic).resolve()
    if use_staged:
        st = staging.get(sch_path)
        if st is None:
            sys.exit(f"no stage exists for {sch_path} (nothing for the agent "
                     f"to have edited yet)")
        return st.working_copy, f"{sch_path.name} (staged)"
    if not sch_path.is_file():
        sys.exit(f"active schematic file not found: {sch_path}")
    return sch_path, sch_path.name


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a .kicad_sch to PNG.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("schematic", nargs="?",
                     help="path to a .kicad_sch, or '-' for S-exp on stdin")
    src.add_argument("--active", action="store_true",
                     help="render the schematic KiCad currently has open")
    src.add_argument("--stage", metavar="PATH",
                     help="render the staged working copy of PATH")
    ap.add_argument("--staged", action="store_true",
                    help="with --active: render the staged copy, not the file")
    ap.add_argument("-o", "--out",
                    help="output PNG path (default: services/api/_renders/<name>.png)")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--theme", help="kicad-cli color theme")
    ap.add_argument("--bw", action="store_true", help="black and white")
    ap.add_argument("--white", action="store_true",
                    help="white background instead of KiCad's cream theme fill")
    ap.add_argument("--frame", action="store_true",
                    help="keep the drawing sheet/title block (off by default)")
    ap.add_argument("--no-crop", action="store_true",
                    help="render the full page instead of cropping to content")
    ap.add_argument("--margin", type=float, default=4.0,
                    help="mm of whitespace kept around content when cropping")
    ap.add_argument("--open", action="store_true",
                    help="open the PNG when done (macOS `open`)")
    args = ap.parse_args()

    if args.active:
        source, label = _resolve_active(args.staged)
    elif args.stage:
        sch_path = Path(args.stage).resolve()
        st = staging.get(sch_path)
        if st is None:
            sys.exit(f"no stage exists for {sch_path}")
        source, label = st.working_copy, f"{sch_path.name} (staged)"
    elif args.schematic == "-":
        source, label = sys.stdin.read(), "stdin"
    elif args.schematic:
        source = Path(args.schematic).resolve()
        if not source.is_file():
            sys.exit(f"file not found: {source}")
        label = source.name
    else:
        ap.error("give a schematic path, '-', --active, or --stage")

    if args.out:
        out = Path(args.out).resolve()
    else:
        stem = Path(label.split(" ")[0]).stem or "schematic"
        out = _RENDERS / f"{stem}.png"

    try:
        render_schematic(
            source, out,
            dpi=args.dpi, theme=args.theme, black_and_white=args.bw,
            exclude_drawing_sheet=not args.frame,
            white_background=args.white,
            crop=not args.no_crop, margin_mm=args.margin,
        )
    except RenderError as e:
        sys.exit(f"render failed: {e}")

    size = out.stat().st_size
    print(f"rendered {label} -> {out} ({size // 1024} KB, {args.dpi} dpi)")
    if args.open:
        subprocess.run(["open", str(out)], check=False)
    print(out)


if __name__ == "__main__":
    main()
