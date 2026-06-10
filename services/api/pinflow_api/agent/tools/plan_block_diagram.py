"""Tool: plan_block_diagram.

LLM call returning a typed block graph. The frontend renders it as a
BlockDiagramCard; the model follows up with ask_user to confirm before any
subcircuit gets generated. For multi-block requests, the whole block diagram
is confirmed once, up front, before generation.
"""

from __future__ import annotations

from pinflow_api.planner import plan

SCHEMA = {
    "name": "plan_block_diagram",
    "description": (
        "Propose a block diagram for a multi-IC schematic request. Nodes are "
        "functional roles (e.g. 'USB-C input', '5V buck'), edges are typed "
        "interfaces (e.g. '+VBUS', 'I2C', 'GND'). Call this FIRST for any ask "
        "that involves more than a single chip. Returns a block graph JSON; "
        "follow up with ask_user to confirm before generating any subcircuit."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "What the user wants to build (their original ask, paraphrased).",
            },
        },
        "required": ["goal"],
    },
}


def run(state, goal: str | None = None, **_) -> dict:
    if not goal or not goal.strip():
        return {"status": "missing_input", "hint": "goal is required."}

    try:
        diagram = plan(goal)
    except RuntimeError as e:
        return {"status": "planner_failed", "error": str(e)}

    nodes = [{"id": n.id, "role": n.role, **({"mpn": n.mpn} if n.mpn else {})}
             for n in diagram.nodes]
    edges = [{"from": e.from_, "to": e.to, "interface": e.interface}
             for e in diagram.edges]
    return {"status": "ok", "nodes": nodes, "edges": edges}
