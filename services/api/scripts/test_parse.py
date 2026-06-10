"""End-to-end smoke test for /parse.

Run after setting ANTHROPIC_API_KEY in services/api/.env :
    cd services/api
    .venv/bin/python scripts/test_parse.py
"""

from pathlib import Path

from pinflow_api.datasheet_parse import parse_datasheet
from pinflow_api.settings import settings


def main() -> None:
    pdf = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "tps62843_datasheet.pdf"
    if not pdf.is_file():
        raise SystemExit(f"missing fixture: {pdf}")

    if not settings.anthropic_api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY not set — create services/api/.env with the key"
        )

    print(f"sending {pdf.name} ({pdf.stat().st_size:,} bytes) to {settings.anthropic_model}…")
    extract = parse_datasheet(pdf.read_bytes())

    print()
    print(f"chip:    {extract.chip}")
    print(f"package: {extract.package}")
    print(f"pins:    {len(extract.pins)}")
    for p in extract.pins:
        print(f"  {p.number:>3}  {p.name:<10}  {p.type}")
    print()
    print(f"recommended_passives: {len(extract.recommended_passives)}")
    for p in extract.recommended_passives:
        pin = f" pin={p.chip_pin_number}" if p.chip_pin_number else ""
        print(f"  {p.component} {p.value:<10}  {p.purpose}{pin}")
    if extract.notes:
        print()
        print("notes:")
        for n in extract.notes:
            print(f"  - {n}")


if __name__ == "__main__":
    main()
