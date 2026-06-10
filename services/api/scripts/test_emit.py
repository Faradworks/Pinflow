"""End-to-end test: parse → resolve → emit → validate via kicad-cli.

    cd services/api
    .venv/bin/python scripts/test_emit.py
"""

import subprocess
from pathlib import Path

from pinflow_api.datasheet_parse import parse_datasheet
from pinflow_api.llm_emit import emit_subcircuit
from pinflow_api.symbol_resolver import resolve_lib_id

KCLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"


def main() -> None:
    fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "tps62843_datasheet.pdf"

    print("[1/4] parsing datasheet...")
    extract = parse_datasheet(fixture.read_bytes())
    print(f"      chip={extract.chip!r}  package={extract.package!r}  pins={len(extract.pins)}")

    print("[2/4] resolving lib_id...")
    lib_id = resolve_lib_id(extract.chip, package_hint=extract.package)
    if lib_id is None:
        raise SystemExit(f"no KiCad lib_id resolved for chip {extract.chip!r}")
    print(f"      lib_id={lib_id}")

    print("[3/4] emitting builder via Claude (this calls the LLM, with ERC repair loop)...")
    emitted = emit_subcircuit(extract, lib_id)
    print(f"      builder_code: {len(emitted.builder_code)} chars, {emitted.builder_code.count(chr(10))} lines")
    print(f"      sexp:         {len(emitted.sexp)} chars")
    print(f"      ERC: {emitted.erc_actionable} actionable / {emitted.erc_total} total ({emitted.attempts} attempt{'s' if emitted.attempts != 1 else ''})")

    print("[4/4] validating S-exp via kicad-cli sch export pdf...")
    out_sch = Path("/tmp/pinflow_emit_test.kicad_sch")
    out_sch.write_text(emitted.sexp)
    out_pdf = Path("/tmp/pinflow_emit_test.pdf")
    out_pdf.unlink(missing_ok=True)
    r = subprocess.run([KCLI, "sch", "export", "pdf", "-o", str(out_pdf), str(out_sch)], capture_output=True, text=True)
    if r.returncode != 0 or not out_pdf.is_file():
        print("\n--- generated builder code (for diagnosis) ---")
        print(emitted.builder_code)
        print("--- end code ---\n")
        print(f"FAIL: kicad-cli rejected the schematic.\nstdout={r.stdout}\nstderr={r.stderr}")
        raise SystemExit(1)

    print(f"\nGREEN. PDF rendered to {out_pdf} ({out_pdf.stat().st_size:,} bytes).")
    print(f"\n--- generated builder code ({len(emitted.builder_code)} chars) ---")
    print(emitted.builder_code)


if __name__ == "__main__":
    main()
