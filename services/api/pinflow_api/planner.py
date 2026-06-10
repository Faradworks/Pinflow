"""LLM-driven block-diagram planner.

Given a user goal in natural language, return a typed block graph: functional
nodes (USB-C input, 5V buck, RP2040 MCU) and typed-interface edges (+VBUS,
+5V, +3V3, GND, I2C, USB DP/DM, …). The frontend renders this as a
`BlockDiagramCard`; the user confirms or edits it before any subcircuit gets
generated.

Tool-use schema mirrors `datasheet_parse.parse_datasheet`'s structured-output
pattern.
"""

from __future__ import annotations

from pinflow_api import llm
from pydantic import BaseModel, Field

from .settings import settings


class BlockNode(BaseModel):
    id: str = Field(description="short kebab-case id, e.g. 'usbc-input', 'mcu'")
    role: str = Field(description="functional role, e.g. 'USB-C input', 'RP2040 MCU'")
    mpn: str | None = Field(
        default=None,
        description=(
            "MPN of the chip implementing this block, if known. Leave null "
            "when the user hasn't picked a part yet."
        ),
    )


class BlockEdge(BaseModel):
    from_: str = Field(alias="from", description="source node id")
    to: str = Field(description="destination node id")
    interface: str = Field(
        description=(
            "Typed interface label, e.g. '+VBUS', '+5V', '+3V3', 'GND', "
            "'I2C', 'USB DP/DM', 'SWD'."
        )
    )

    model_config = {"populate_by_name": True}


class BlockDiagram(BaseModel):
    nodes: list[BlockNode]
    edges: list[BlockEdge]


_SYSTEM = (
    "You design block diagrams for electronics schematics. Given a user "
    "request, propose the minimum set of functional blocks and the typed "
    "interfaces between them.\n\n"
    "Rules:\n"
    "- Each node is a functional role (not a generic 'circuit'). Examples: "
    "'USB-C input', '5V buck', '3V3 LDO', 'RP2040 MCU', 'IMU', 'USB-PD sink'.\n"
    "- Each edge has a typed interface from this list when applicable: "
    "+VBUS, +5V, +3V3, +1V8, GND, I2C, SPI, UART, USB DP/DM, SWD, GPIO. "
    "Coin a new label only if none of these fits.\n"
    "- Keep the diagram small — usually 1–6 nodes. For single-chip asks, a "
    "single node is correct.\n"
    "- Always reply by calling submit_block_diagram — never prose.\n"
)


def plan(goal: str) -> BlockDiagram:
    """Return a block diagram for the user's goal."""
    if not llm.available():
        raise RuntimeError(llm.NOT_CONFIGURED_MSG)

    client = llm.make_client()

    tool = {
        "name": "submit_block_diagram",
        "description": "Submit the proposed block diagram.",
        "input_schema": _flatten_schema(BlockDiagram.model_json_schema(by_alias=True)),
    }

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2048,
        system=_SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "submit_block_diagram"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Propose a block diagram for the following request. "
                            "Call submit_block_diagram with the result.\n\n"
                            f"Request: {goal}"
                        ),
                    }
                ],
            }
        ],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_block_diagram":
            return BlockDiagram.model_validate(block.input)

    raise RuntimeError(
        f"model did not call submit_block_diagram; stop_reason={response.stop_reason}"
    )


def _flatten_schema(schema: dict) -> dict:
    """Inline $defs into $ref sites — same shape as datasheet_parse uses."""
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
