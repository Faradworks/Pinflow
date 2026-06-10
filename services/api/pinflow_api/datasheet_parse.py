"""Datasheet PDF → structured chip extract via Claude.

Uses the Anthropic API's native PDF input (Sonnet 4.6) + tool-use for typed output.
The LLM "calls" submit_chip_extract with a ChipExtract-shaped argument; we get
the structured input back and validate it with Pydantic.
"""

from __future__ import annotations

import base64
from typing import Optional

from pinflow_api import llm
from pydantic import BaseModel, Field

from .settings import settings  # noqa: F401  used in module-level import below


class Pin(BaseModel):
    number: str
    name: str
    type: str = Field(description='one of: "power_in", "power_out", "input", "output", "bidir", "passive", "gnd", "no_connect"')


class RecommendedPassive(BaseModel):
    purpose: str = Field(description="e.g. 'IOVDD decoupling', 'crystal load cap', 'RUN pull-up', 'feedback divider top'")
    component: str = Field(description="standard ref letter: 'C', 'R', 'L', 'Y' (crystal), 'D'")
    value: str = Field(description="e.g. '100nF', '10k', '12MHz', '2.2uH'")
    chip_pin_number: Optional[str] = Field(default=None, description="chip pin number this passive connects to, if applicable")


class VariantCandidate(BaseModel):
    """One entry from the datasheet's ordering / package-options table."""

    orderable_part: str = Field(description="full ordering code, e.g. 'TPS62840DLCR'")
    package: str = Field(description="package form factor, e.g. 'WSON-8', 'BGA-9'")
    package_code: str = Field(description="short package suffix used in KiCad symbol names, e.g. 'DLC', 'YBG'")
    pin_count: int = Field(description="number of pins on this variant's pintable")
    notes: str = Field(default="", description="e.g. temperature grade, tape-and-reel, stocked-volume hint")


class VariantPintable(BaseModel):
    """Pin map for one DISTINCT package pinout, shared by every orderable
    part with that package_code. Most chips have many orderable parts
    (temp grade / tape-and-reel) but only one or two real pinouts."""

    package_code: str = Field(description="short package suffix shared by all orderable parts with this pinout, e.g. 'DSJ', 'YBG'")
    package: str = Field(description="package form factor for this pinout, e.g. 'VSON-14', 'DSBGA-9'")
    pins: list[Pin] = Field(description="pin map (number, name, electrical type) for this package family")


class ChipExtract(BaseModel):
    chip: str = Field(description="primary part identifier without variant suffix, e.g. 'RP2040' or 'TPS62840'")
    package: str = Field(description="package of the CHOSEN variant, e.g. 'WSON-8 (DLC)'")
    variant_code: Optional[str] = Field(default=None, description="package_code of the chosen variant, e.g. 'DLC'")
    orderable_part: Optional[str] = Field(default=None, description="full ordering code of the chosen variant, e.g. 'TPS62840DLCR'")
    available_variants: list[VariantCandidate] = Field(
        default_factory=list,
        description="all variants from the ordering / package-options table. Single-variant chips can leave this empty.",
    )
    pintables: list[VariantPintable] = Field(
        default_factory=list,
        description="one entry per DISTINCT package pinout. Group every orderable part that shares a package_code / pin numbering into ONE entry — do NOT emit one per orderable part. Single-pinout chips can leave this empty.",
    )
    pins: list[Pin] = Field(description="pintable for the CHOSEN variant; must equal the matching `pintables` entry's pins")
    recommended_passives: list[RecommendedPassive]
    notes: list[str] = Field(default_factory=list, description="brief notes on layout/usage; <=3 items")


_SYSTEM = (
    "You extract the recommended-application schematic info from chip datasheets. "
    "Focus on:\n"
    "  1) the ordering-information / package-options table — populate "
    "`available_variants` for every orderable code listed (full part number, "
    "package, short package suffix used in the KiCad symbol name, pin count). "
    "Pick one as the default and set `variant_code` + `orderable_part` + "
    "`package` to that variant. Prefer the most commonly stocked / production "
    "volume variant; prefer non-BGA over BGA when both exist (BGA is harder to "
    "hand-assemble). If the user prompt names a specific variant, use that.\n"
    "  2) the pin map(s). Different package variants of the same chip can have "
    "different pin numbers and counts. Emit ONE `pintables` entry per DISTINCT "
    "pinout, keyed by package_code: if 18 orderable parts all share package "
    "VSON-14 (DSJ) that is exactly ONE `pintables` entry, not 18. Only emit a "
    "second entry when a package family genuinely renumbers pins (e.g. a "
    "DSBGA ball grid vs a SOT). Also set `pins` to the CHOSEN variant's "
    "pintable — it must duplicate the matching `pintables` entry.\n"
    "  3) recommended-application external components (decoupling caps, "
    "crystals, pull-ups, feedback dividers) with values from any 'Reference "
    "Schematic' / 'Application Circuit' / 'Typical Application' section.\n"
    "Ignore memory maps, register descriptions, and electrical-spec tables not "
    "relevant to the recommended application circuit. Always call the "
    "submit_chip_extract tool — never reply with prose."
)

_USER = (
    "Extract the chip's variant table, pin map for the chosen variant, and "
    "recommended application-circuit components from the attached datasheet. "
    "Call submit_chip_extract with the structured result."
)


def parse_datasheet(
    pdf_bytes: bytes,
    user_prompt: Optional[str] = None,
    variant_hint: Optional[str] = None,
) -> ChipExtract:
    if not llm.available():
        raise RuntimeError(llm.NOT_CONFIGURED_MSG)

    client = llm.make_client()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()

    tool = {
        "name": "submit_chip_extract",
        "description": "Submit the structured chip extract.",
        "input_schema": _flatten_schema(ChipExtract.model_json_schema()),
    }

    user_text = _USER
    if variant_hint and variant_hint.strip():
        user_text += (
            f"\n\nVariant requested by the user: {variant_hint.strip()}. "
            "Set `variant_code` / `orderable_part` / `package` / `pins` to this "
            "variant. Still populate `available_variants` with every variant in "
            "the ordering table for completeness."
        )
    if user_prompt and user_prompt.strip():
        user_text += (
            f"\n\nUser guidance — use this to bias which subcircuit / which "
            f"recommended values you extract:\n{user_prompt.strip()}"
        )

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=8192,
        system=_SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_chip_extract"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_chip_extract":
            return ChipExtract.model_validate(block.input)

    raise RuntimeError(f"model did not call submit_chip_extract; stop_reason={response.stop_reason}")


def _flatten_schema(schema: dict) -> dict:
    """Inline $defs into $ref sites so the schema is self-contained.

    Anthropic's tool input_schema does accept $ref + $defs, but inlining keeps
    things simple and avoids surprise across SDK / model version drift.
    """
    defs = schema.pop("$defs", {})

    def _resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if ref.startswith("#/$defs/"):
                    name = ref.split("/")[-1]
                    return _resolve(defs[name])
                return node
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(x) for x in node]
        return node

    return _resolve(schema)
