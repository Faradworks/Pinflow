"""Event types streamed from /agent/chat.

Each event maps 1:1 to a variant of the frontend `Message` discriminated
union in `apps/desktop/src/components/chat/types.ts`, plus a few control
kinds (`meta`, `suspended`, `done`) that aren't messages but flow control.

The wire format is SSE: `event: <kind>\\ndata: <json>\\n\\n`.
"""

from __future__ import annotations

import json
import uuid


def new_id() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def ev_meta(conversation_id: str) -> dict:
    return {"kind": "meta", "conversation_id": conversation_id}


def ev_ai(
    text: str,
    *,
    questions: list[dict] | None = None,
    diff: list[dict] | None = None,
    confirm: bool | None = None,
    locked: bool | None = None,
    cost: dict | None = None,
) -> dict:
    e: dict = {"kind": "ai", "id": new_id(), "text": text}
    if questions is not None:
        e["questions"] = questions
    if diff is not None:
        e["diff"] = diff
    if confirm is not None:
        e["confirm"] = confirm
    if locked is not None:
        e["locked"] = locked
    if cost is not None:
        e["cost"] = cost  # forward "cost to finish" estimate for a Confirm/Discard gate
    return e


def ev_tool(tool: str, title: str, meta: list[dict]) -> dict:
    return {"kind": "tool", "id": new_id(), "tool": tool, "title": title, "meta": meta}


def ev_block_diagram(nodes: list[dict], edges: list[dict]) -> dict:
    return {"kind": "block_diagram", "id": new_id(), "nodes": nodes, "edges": edges}


def ev_design_spec(spec: dict) -> dict:
    return {"kind": "design_spec", "id": new_id(), "spec": spec}


def ev_resolve_parts(rows: list[dict]) -> dict:
    return {"kind": "resolve_parts", "id": new_id(), "rows": rows}


def ev_signin_required(hint: str) -> dict:
    return {"kind": "signin_required", "id": new_id(), "hint": hint}


def ev_cost(meter, *, model: str, provider: str) -> dict:
    """Live cost meter for the running turn. Not a chat Message — the frontend
    applies it to a standalone running-meter line (App-level state), so it
    carries no id. `estimated` True → the figure is a local token→credit
    estimate (shown with a ~), False → authoritative from gateway headers."""
    return {
        "kind": "cost",
        "request_credits": round(meter.request_credits, 4),
        "request_usd": round(meter.request_usd, 5),
        "conversation_credits": round(meter.conversation_credits, 4),
        "estimated": meter.request_estimated,
        "balance": meter.last_balance,
        "model": model,
        "provider": provider,
        # Token counts (agent loop's own calls), shown alongside credits/USD.
        "request_tokens": meter.request_tokens,
        "request_input_tokens": meter.request_input_tokens,
        "request_output_tokens": meter.request_output_tokens,
        "conversation_tokens": meter.conversation_tokens,
    }


def ev_thinking(text: str, streaming: bool) -> dict:
    return {"kind": "thinking", "id": new_id(), "text": text, "streaming": streaming}


def ev_system(text: str) -> dict:
    return {"kind": "system", "id": new_id(), "text": text}


def ev_suspended() -> dict:
    return {"kind": "suspended"}


def ev_done() -> dict:
    return {"kind": "done"}


def to_sse(event: dict) -> str:
    kind = event["kind"]
    data = {k: v for k, v in event.items() if k != "kind"}
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"
