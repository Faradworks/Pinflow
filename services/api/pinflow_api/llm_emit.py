"""LLM-driven subcircuit emitter.

Pipeline:
  ChipExtract + lib_id
    → Claude generates a Python module with `def build() -> ksa.Schematic`
    → execute the module in a subprocess (same venv, isolated scope)
    → serialize the returned Schematic to clipboard-format S-exp via sch_to_string

Generates *code*, not raw S-exp, because:
  - kicad-sch-api validates lib_ids and pin numbers at runtime — errors land with
    stack traces, not silently malformed S-exp.
  - The code is debuggable / reviewable / cacheable.
  - Verifier loop (Day 3) can feed errors back to the LLM as text.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

from pinflow_api import llm
from pydantic import BaseModel

from .datasheet_parse import ChipExtract
from .kicad_cli import ErcReport, run_erc
from .settings import settings


# ERC rules that are inherent to a subcircuit-without-context and should NOT
# trigger a verifier retry — the user's main schematic provides the missing
# upstream sources.
_EXTERNAL_RULES = (
    "power_pin_not_driven",  # VIN/GND want an upstream power source
    "pin_not_driven",        # input pin expects external signal
)


class EmitAttempt(BaseModel):
    builder_code: str
    sexp: str
    erc_violations: int  # total
    erc_actionable: int  # after filtering external rules


class EmittedSubcircuit(BaseModel):
    builder_code: str
    sexp: str  # full (kicad_sch ...) document — convert to clipboard format on the way out
    lib_id: str
    erc_total: int
    erc_actionable: int
    attempts: int  # 1 = first shot was clean; >1 means verifier ran


_API_REFERENCE = """\
import kicad_sch_api as ksa

sch = ksa.create_schematic("Title")  # → ksa.Schematic

sch.components.add(
    lib_id="Library:Symbol",        # e.g. "Device:R", "Device:C", "Regulator_Switching:TPS628436DRL"
    reference="U1",                 # "U" ICs, "R" resistors, "C" caps, "L" inductors, "Y" crystals, "#PWR" power symbols
    value="100nF",                  # display value text
    position=(150.0, 100.0),        # mm. Multiples of 2.54 only.
    footprint="",                   # optional, "" if unknown
    add_all_units=False,            # True for multi-unit symbols (RP2040 has 2 units; most chips are 1)
    unit_spacing=80.0,              # only relevant when add_all_units=True
)

sch.add_wire_between_pins("U1", "1", "C1", "1")  # pin numbers are strings
"""

_CONVENTIONS = """\
- Positions in mm. SNAP all coordinates to the 2.54 mm grid (multiples of 2.54: 95.0, 97.54, 100.0, 102.54, ...).
- Y axis INVERTED: smaller Y = visually higher.
- Place the main chip near (150, 100). Spread passives 10–30 mm around it; do not overlap.

POWER NETS — use power symbols, not bare net labels:
- KiCad has dedicated power symbols: lib_id="power:GND", "power:+3V3", "power:+5V", "power:VBUS", "power:VCC".
  Add them with `sch.components.add(lib_id="power:GND", reference="#PWR1", value="GND", position=(x,y))`.
  References use the "#PWR" prefix and a unique number. Power symbols have a single pin "1".
- For ANY arbitrary named net (VIN, VOUT, MISO, USB_DM, etc.), use a generic power symbol if standard
  (+3V3, GND, +5V, VBUS); otherwise use a "power:VCC" symbol with value overridden to your name and the
  reference still "#PWRn". Wire the symbol's pin "1" to the relevant component pin via add_wire_between_pins.

WIRING RULES (every pin must be terminated):
- Decoupling cap: pin 1 → chip power pin (via wire); pin 2 → a `power:GND` symbol's pin 1 (via wire).
- Pull-up / feedback resistor: pin 1 → chip signal pin; pin 2 → power rail symbol (e.g. power:+3V3 or power:GND).
- Inductor on a switch node: pin 1 → chip SW pin; pin 2 → output node (and that output node should also have
  a `power:VCC` (named "VOUT") symbol attached so the net is named).
- The chip's own GND/power pins also need wires. Add a `power:GND` symbol next to the chip's GND pin and wire
  it. Same for VIN: add a `power:VCC`-style symbol named "VIN" (or use `power:+3V3` / `power:+5V` if appropriate).
- Enable (EN) pin on a regulator: typical default is to tie EN to VIN for always-on; wire EN pin to the same
  node a "VIN" power symbol drives.

PASSIVE LIB_IDS:
- "Device:R", "Device:C", "Device:L"
- "Device:Crystal_GND24" (4-pin SMD crystal)
- "power:GND", "power:+3V3", "power:+5V", "power:VBUS", "power:VCC" (use VCC + override value for arbitrary named rails)

DO NOT use sch.add_label() — power symbols are how nets get named in this codebase.
"""

_SYSTEM = textwrap.dedent(
    f"""\
    You generate Python code that builds a KiCad subcircuit with kicad-sch-api.

    API surface (use ONLY these primitives — do NOT call any other ksa method):
    {_API_REFERENCE}

    Conventions:
    {_CONVENTIONS}

    Output: a Python module with a single top-level function `def build() -> ksa.Schematic:`.
    The function constructs the schematic and RETURNS it (does not save, does not serialize).
    Imports allowed: ONLY `import kicad_sch_api as ksa`.
    Output ONLY the module text. Do not wrap in markdown fences. Do not add explanatory prose.
    """
).strip()


def emit_subcircuit(
    extract: ChipExtract,
    lib_id: str,
    multi_unit: bool = False,
    max_repair_attempts: int = 2,
    extra_lib_path: Optional["Path"] = None,
    user_prompt: Optional[str] = None,
) -> EmittedSubcircuit:
    """Generate the builder, execute, ERC, and repair via LLM feedback if needed.

    `extra_lib_path` adds a directory to ksa's symbol search at runtime — used
    when lib_id points at an LCSC-fetched symbol in `_easyeda_cache/`.

    `user_prompt` injects free-form user guidance into the emit prompt.

    Counts only "actionable" ERC violations (excludes rules inherent to
    subcircuits-without-context). Returns the best attempt — even if
    violations remain after max_repair_attempts.
    """
    if not llm.available():
        raise RuntimeError(llm.NOT_CONFIGURED_MSG)

    code = _generate_code(extract, lib_id, multi_unit=multi_unit, user_prompt=user_prompt)
    sexp = _execute_builder(code, extra_lib_path=extra_lib_path)
    report = run_erc(sexp)
    attempts = 1

    for _ in range(max_repair_attempts):
        actionable = report.filtered(exclude_rules=_EXTERNAL_RULES)
        if not actionable:
            break
        feedback = report.to_llm_feedback(exclude_rules=_EXTERNAL_RULES)
        try:
            new_code = _repair_code(
                extract, lib_id, prev_code=code, feedback=feedback,
                multi_unit=multi_unit, user_prompt=user_prompt,
            )
            new_sexp = _execute_builder(new_code, extra_lib_path=extra_lib_path)
        except BuilderExecutionError:
            break
        new_report = run_erc(new_sexp)
        attempts += 1
        new_actionable = new_report.filtered(exclude_rules=_EXTERNAL_RULES)
        if len(new_actionable) <= len(actionable):
            code, sexp, report = new_code, new_sexp, new_report
            if not new_actionable:
                break

    return EmittedSubcircuit(
        builder_code=code,
        sexp=sexp,
        lib_id=lib_id,
        erc_total=report.total,
        erc_actionable=len(report.filtered(exclude_rules=_EXTERNAL_RULES)),
        attempts=attempts,
    )


def _generate_code(
    extract: ChipExtract,
    lib_id: str,
    multi_unit: bool,
    user_prompt: Optional[str] = None,
) -> str:
    example_code = _read_example_builder()
    prompt_block = (
        f"\nUser guidance (free-text, may include desired Vout, biasing, target net names, "
        f"or 'extract only X part of the circuit'):\n{user_prompt}\n"
        if user_prompt and user_prompt.strip()
        else ""
    )
    user_msg = textwrap.dedent(
        f"""\
        Build the subcircuit for:

        chip:        {extract.chip}
        package:     {extract.package}
        lib_id:      {lib_id}
        multi_unit:  {multi_unit}    # if True, pass add_all_units=True when adding the chip
        {prompt_block}
        Chip extract (pin map + recommended passives):
        {extract.model_dump_json(indent=2)}

        Reference shape — an existing builder for a different chip. Adapt the SHAPE,
        not the lib_ids or values:
        --- begin reference ---
        {example_code}
        --- end reference ---

        Now write the build() module for {extract.chip}. Use lib_id={lib_id!r} for the chip.
        """
    )

    client = llm.make_client()
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    code = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()

    # The model sometimes wraps in ```python despite instructions — strip if present.
    if code.startswith("```"):
        first_nl = code.find("\n")
        code = code[first_nl + 1 :] if first_nl != -1 else code[3:]
        if code.endswith("```"):
            code = code[:-3]
        code = code.strip()
    return code


def _read_example_builder() -> str:
    """Use the RP2040 builder as the few-shot. RP2040 is multi-unit so the LLM
    sees the add_all_units pattern; pin layout is rich enough to teach the shape.
    """
    return (Path(__file__).parent / "builders" / "rp2040.py").read_text()


def _repair_code(
    extract: ChipExtract,
    lib_id: str,
    prev_code: str,
    feedback: str,
    multi_unit: bool,
    user_prompt: Optional[str] = None,
) -> str:
    """Ask Claude to repair the builder against ERC feedback. Returns full
    replacement code (not a diff).
    """
    guidance = (
        f"\nKeep honoring the user's original guidance: {user_prompt}\n"
        if user_prompt and user_prompt.strip()
        else ""
    )
    user_msg = textwrap.dedent(
        f"""\
        The previous build() function for {extract.chip} produced a schematic that
        passed parsing but has these ERC violations:

        {feedback}
        {guidance}

        Previous code:
        --- begin previous ---
        {prev_code}
        --- end previous ---

        Common fixes:
        - label_dangling: replace the bare label with a `power:GND` / `power:+3V3` /
          `power:VCC` (with name override) symbol whose pin "1" sits at the SAME
          position; wire the relevant component pin to that symbol's pin "1".
        - pin_not_connected on a passive's pin 2: add the missing wire to a power
          symbol (GND for return, +rail for pull-ups).
        - pin_not_connected on a chip GND pin: add a power:GND symbol next to it
          and wire it.
        - pin_not_connected on EN: typical fix is to wire EN to a power symbol
          named "VIN" (always-on default).

        Reuse passive component values, lib_ids, and overall layout where you can.
        Output the FULL replacement module — no diff, no fences, no prose.
        Use lib_id={lib_id!r} for the chip; multi_unit={multi_unit}.
        """
    )

    client = llm.make_client()
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    code = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    if code.startswith("```"):
        first_nl = code.find("\n")
        code = code[first_nl + 1 :] if first_nl != -1 else code[3:]
        if code.endswith("```"):
            code = code[:-3]
        code = code.strip()
    return code


def _execute_builder(code: str, extra_lib_path: Optional[Path] = None) -> str:
    """Run the generated module in a subprocess; capture the serialized schematic.

    `extra_lib_path` is added to kicad-sch-api's symbol cache before the
    builder runs — needed when the chip's symbol came from easyeda2kicad and
    lives in `_easyeda_cache/` rather than KiCad's bundled libs.
    """
    preamble = ""
    if extra_lib_path is not None:
        preamble = (
            "import kicad_sch_api as _ksa_bootstrap\n"
            f"_ksa_bootstrap.get_symbol_cache().discover_libraries({[str(extra_lib_path)]!r})\n"
        )

    wrapper = (
        preamble
        + code
        + "\n\n# --- pinflow runner ---\n"
        + "import sys as _sys\n"
        + "from pinflow_api.builders._common import sch_to_string as _sch_to_string\n"
        + "_sys.stdout.write(_sch_to_string(build()))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", wrapper],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise BuilderExecutionError(
            f"generated builder failed (exit {result.returncode}):\n{result.stderr}",
            stderr=result.stderr,
            code=code,
        )
    return result.stdout


class BuilderExecutionError(RuntimeError):
    """Raised when the LLM-generated builder code fails at runtime.

    `stderr` and `code` are exposed so the verifier loop (Day 3) can feed them
    back to Claude for a retry.
    """

    def __init__(self, message: str, stderr: str, code: str):
        super().__init__(message)
        self.stderr = stderr
        self.code = code
