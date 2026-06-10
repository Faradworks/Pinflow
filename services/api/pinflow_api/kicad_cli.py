"""Wrapper around `kicad-cli` for headless validation of generated schematics.

Used by the verifier loop to ERC the LLM-emitted output and feed violations
back to the model for repair.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

# macOS KiCad 10. Generalize alongside other hardcoded paths when we go cross-platform.
_KCLI = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")


class ErcViolation(BaseModel):
    rule: str  # e.g. "label_dangling", "pin_not_connected", "power_pin_not_driven"
    severity: str  # "error" | "warning"
    location: str  # human-readable location, e.g. "(127.00 mm, 80.01 mm): Label 'VIN'"


class ErcReport(BaseModel):
    violations: list[ErcViolation]
    raw: str

    @property
    def total(self) -> int:
        return len(self.violations)

    def filtered(self, *, exclude_rules: tuple[str, ...] = ()) -> list[ErcViolation]:
        return [v for v in self.violations if v.rule not in exclude_rules]

    def to_llm_feedback(self, *, exclude_rules: tuple[str, ...] = ()) -> str:
        items = self.filtered(exclude_rules=exclude_rules)
        if not items:
            return "(no actionable ERC violations)"
        lines = [f"- [{v.rule}] {v.location}" for v in items]
        return "\n".join(lines)


def export_netlist(sch_path: Path) -> str:
    """Export a `.kicad_sch` to a kicadsexpr netlist via `kicad-cli sch export netlist`.

    Returns the raw netlist text. Operates on the file at `sch_path` as-is —
    the caller is responsible for passing the staged working-copy path when
    a stage exists (see `staging.get(...).temp_path`).
    """
    if not _KCLI.is_file():
        raise RuntimeError(f"kicad-cli not found at {_KCLI}")
    if not sch_path.is_file():
        raise FileNotFoundError(f"schematic not found: {sch_path}")

    with tempfile.TemporaryDirectory(prefix="pinflow_netlist_") as tmpd:
        out = Path(tmpd) / "netlist.net"
        result = subprocess.run(
            [
                str(_KCLI), "sch", "export", "netlist",
                "--format", "kicadsexpr",
                "-o", str(out),
                str(sch_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not out.is_file():
            raise RuntimeError(
                f"kicad-cli netlist export failed (exit {result.returncode}):\n"
                f"{result.stderr or result.stdout}"
            )
        return out.read_text()


def validate_parseable(sch_text: str) -> Optional[str]:
    """Confirm KiCad's own parser accepts `sch_text`.

    Returns None when it parses cleanly — and also when kicad-cli is
    unavailable (can't judge, so don't block). Returns a one-line reason when
    kicad-cli rejects it. `staging.commit` runs this on the staged bytes
    before they overwrite the user's real project, so a malformed schematic
    (e.g. an unterminated string) can't brick their file.

    The text is written to a `.kicad_sch`-named temp file — kicad-cli keys
    off the extension and refuses anything else. Netlist export is the probe:
    it fully parses the schematic (reading its embedded `lib_symbols`, so a
    missing *external* library is not a parse failure) and is cheap enough
    for a user-gated commit.
    """
    if not _KCLI.is_file():
        return None  # no kicad-cli — cannot validate, fail open
    with tempfile.TemporaryDirectory(prefix="pinflow_validate_") as tmpd:
        probe = Path(tmpd) / "check.kicad_sch"
        probe.write_text(sch_text, encoding="utf-8")
        try:
            export_netlist(probe)
            return None
        except RuntimeError as e:
            return (str(e).splitlines() or ["kicad-cli rejected the schematic"])[0]


def run_erc(sch_text: str) -> ErcReport:
    """Run `kicad-cli sch erc` on the given schematic text. Returns a parsed report."""
    if not _KCLI.is_file():
        raise RuntimeError(f"kicad-cli not found at {_KCLI}")

    with tempfile.TemporaryDirectory(prefix="pinflow_erc_") as tmpd:
        sch = Path(tmpd) / "subject.kicad_sch"
        sch.write_text(sch_text)
        rpt = Path(tmpd) / "report.rpt"
        result = subprocess.run(
            [str(_KCLI), "sch", "erc", "-o", str(rpt), str(sch)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if not rpt.is_file():
            raise RuntimeError(
                f"kicad-cli sch erc failed (exit {result.returncode}):\n{result.stderr or result.stdout}"
            )
        raw = rpt.read_text()

    return ErcReport(violations=_parse(raw), raw=raw)


# Each violation in the report has the shape:
#   [rule_name]: human description
#       ; error  (or  ; warning)
#       @(x mm, y mm): location detail
_BLOCK = re.compile(
    r"\[(?P<rule>[a-z_]+)\]:[^\n]*\n"
    r"\s*;\s*(?P<sev>error|warning)\s*\n"
    r"\s*(?P<loc>@\([^\)]+\):[^\n]+)",
    re.MULTILINE,
)


def _parse(report: str) -> list[ErcViolation]:
    violations = []
    for m in _BLOCK.finditer(report):
        violations.append(
            ErcViolation(
                rule=m.group("rule"),
                severity=m.group("sev"),
                location=m.group("loc").strip(),
            )
        )
    return violations
