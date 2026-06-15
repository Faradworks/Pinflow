"""Offline integration check for the cost meter + spend-cap gate in the agent
loop. Drives the REAL loop (run_chat / run_resume) against a fake Anthropic
client, so no key / network. Needs the venv's deps (anthropic, fastapi, …) for
the loop's imports:

    .venv/bin/python scripts/test_cost_loop.py
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pinflow_api import llm  # noqa: E402
from pinflow_api.agent import loop as agent_loop  # noqa: E402
from pinflow_api.agent import state as st  # noqa: E402
from pinflow_api.settings import settings  # noqa: E402


def _usage(inp=0, out=0, cw=0, cr=0):
    return types.SimpleNamespace(
        input_tokens=inp, output_tokens=out,
        cache_creation_input_tokens=cw, cache_read_input_tokens=cr,
    )


def _text(s):
    return types.SimpleNamespace(type="text", text=s)


def _tool_use(name, tid, inp=None):
    return types.SimpleNamespace(type="tool_use", id=tid, name=name, input=inp or {})


def _resp(stop_reason, content, usage):
    return types.SimpleNamespace(stop_reason=stop_reason, content=content, usage=usage)


class _FakeRaw:
    def __init__(self, response, headers=None):
        self._response = response
        self.headers = headers  # None → no gateway headers → local estimate path

    def parse(self):
        return self._response


class _FakeClient:
    """messages.with_raw_response.create(...) pops the next queued response and
    records the `model` kwarg (so tests can assert the resolved agent model)."""

    def __init__(self, queue):
        self._queue = queue
        self.last_model = None
        outer = self

        class _WRR:
            def create(self, **kwargs):
                outer.last_model = kwargs.get("model")
                resp, headers = outer._queue.pop(0)
                return _FakeRaw(resp, headers)

        class _Messages:
            with_raw_response = _WRR()

        self.messages = _Messages()


# The single fake client a _patch() set up uses, so tests can read .last_model.
_LAST_CLIENT = None


def _patch(queue):
    """Patch the loop's LLM seam to use a fake client over `queue`, and stub the
    context block so we don't pull in schematic machinery. Returns an undo fn."""
    global _LAST_CLIENT
    _LAST_CLIENT = _FakeClient(queue)
    saved = (llm.available, llm.make_client, llm.provider_of, agent_loop.build_context_block)
    llm.available = lambda cfg=None: True
    llm.make_client = lambda cfg=None: _LAST_CLIENT
    llm.provider_of = lambda cfg=None: "pinflow-cloud"
    agent_loop.build_context_block = lambda state: "ctx"

    def undo():
        (llm.available, llm.make_client, llm.provider_of, agent_loop.build_context_block) = saved

    return undo


def test_cost_event_includes_tokens():
    # The cost event carries token counts (request_tokens + in/out) for both paths.
    queue = [(_resp("end_turn", [_text("done")], _usage(inp=300, out=120)), None)]
    undo = _patch(queue)
    try:
        events = list(agent_loop.run_chat("c_tok", "hi"))
    finally:
        undo()
    ce = next(e for e in events if e.get("kind") == "cost")
    assert ce["request_tokens"] == 420, ce
    assert ce["request_input_tokens"] == 300 and ce["request_output_tokens"] == 120, ce
    assert ce["conversation_tokens"] == 420, ce


def test_agent_model_selection():
    from pinflow_api.settings import settings
    # Default (no agent_model) → the Opus agent model.
    undo = _patch([(_resp("end_turn", [_text("ok")], _usage()), None)])
    try:
        list(agent_loop.run_chat("c_m1", "hi"))
        assert _LAST_CLIENT.last_model == settings.anthropic_agent_model
    finally:
        undo()
    # Sonnet alias → settings.anthropic_model.
    undo = _patch([(_resp("end_turn", [_text("ok")], _usage()), None)])
    try:
        list(agent_loop.run_chat("c_m2", "hi", llm_config=llm.LLMConfig(agent_model="sonnet")))
        assert _LAST_CLIENT.last_model == settings.anthropic_model
    finally:
        undo()


def test_cost_event_emitted_on_estimate_path():
    # No gateway headers → self/BYOK path: USD estimate, no credits, no margin.
    queue = [(_resp("end_turn", [_text("done")], _usage(inp=1_000_000, out=0)), None)]
    undo = _patch(queue)
    try:
        events = list(agent_loop.run_chat("c_cost_1", "hi"))
    finally:
        undo()
    cost_events = [e for e in events if e.get("kind") == "cost"]
    assert len(cost_events) == 1, [e["kind"] for e in events]
    ce = cost_events[0]
    # 1M input on Opus = $5.00 estimated; credits stay 0 (no gateway), estimated.
    assert ce["request_usd"] == 5.0, ce
    assert ce["request_credits"] == 0.0 and ce["estimated"] is True, ce
    assert any(e.get("kind") == "done" for e in events)


def test_cost_event_authoritative_from_headers():
    headers = {"x-pinflow-credits-charged": "0.42", "x-pinflow-credits-balance": "12.3"}
    queue = [(_resp("end_turn", [_text("ok")], _usage(inp=999)), headers)]
    undo = _patch(queue)
    try:
        events = list(agent_loop.run_chat("c_cost_2", "hi"))
    finally:
        undo()
    ce = next(e for e in events if e.get("kind") == "cost")
    # Balance-delta from one call: pre-call 12.72 − 12.3 = 0.42 credits, exact.
    assert ce["request_credits"] == 0.42 and ce["estimated"] is False, ce
    assert ce["balance"] == 12.3, ce


def _hdr(charged: str, balance: str) -> dict:
    return {"x-pinflow-credits-charged": charged, "x-pinflow-credits-balance": balance}


def test_spend_cap_suspends_then_continue_and_stop():
    saved_cap = settings.pinflow_credit_cap_per_request
    settings.pinflow_credit_cap_per_request = 0.01  # tiny → trips after one turn
    # Turn 1 emits a tool_use (loop continues to the cap check); balance headers
    # give it real credit spend (pre-call 10.0 − 9.95 = 0.05 ≥ 0.01).
    turn1 = (_resp("tool_use", [_tool_use("remove_components", "tu_1")], _usage()), _hdr("0.05", "9.95"))
    try:
        # --- Continue path ---
        queue = [turn1]
        undo = _patch(queue)
        try:
            events = list(agent_loop.run_chat("c_cap_go", "do a lot"))
            assert any(e.get("kind") == "suspended" for e in events), [e["kind"] for e in events]
            s = st.get("c_cap_go")
            assert s.pending_question is not None and s.pending_question.kind == "cost_cap"
            assert any(e.get("kind") == "cost" for e in events)
            # Continue: queue a finishing turn, resume, expect it drives to done.
            queue.append((_resp("end_turn", [_text("continued")], _usage()), _hdr("0.01", "9.94")))
            r_events = list(agent_loop.run_resume("c_cap_go", "Continue"))
            assert any(e.get("kind") == "done" for e in r_events), [e["kind"] for e in r_events]
            # Continue approves the rest of the request (one prompt per request).
            assert s.cost.approved_ceiling == float("inf"), s.cost.approved_ceiling
        finally:
            undo()

        # --- Stop path ---
        # turn1 is consumed by run_chat (it's what trips the cap); the sentinel
        # must survive — Stop must NOT drive another LLM turn.
        sentinel = (_resp("end_turn", [_text("should not run")], _usage()), _hdr("0.0", "9.95"))
        queue2 = [turn1, sentinel]
        undo2 = _patch(queue2)
        try:
            list(agent_loop.run_chat("c_cap_stop", "do a lot"))
            assert len(queue2) == 1, "run_chat should consume only turn1"
            r_events = list(agent_loop.run_resume("c_cap_stop", "Stop"))
            kinds = [e["kind"] for e in r_events]
            assert "system" in kinds and "done" in kinds, kinds
            assert len(queue2) == 1, "Stop should not have consumed the sentinel"
        finally:
            undo2()
    finally:
        settings.pinflow_credit_cap_per_request = saved_cap


def test_gate_cost_attached_to_confirm_discard():
    # A response that asks a Confirm/Discard question carries a forward "cost to
    # finish" estimate on the emitted ai event (rendered on the ConfirmBar).
    ask = _tool_use("ask_user", "tu_ask",
                    {"question": "Apply this design spec?", "options": ["Confirm", "Discard"]})
    queue = [(_resp("tool_use", [ask], _usage()), _hdr("0.30", "9.70"))]
    undo = _patch(queue)
    try:
        events = list(agent_loop.run_chat("c_gate", "add a buck"))
    finally:
        undo()
    with_cost = [e for e in events if e.get("kind") == "ai" and e.get("cost")]
    assert with_cost, [e.get("kind") for e in events]
    c = with_cost[0]["cost"]
    assert c["unit"] == "credits" and c["hi"] >= c["lo"] > 0, c
    assert c["balance"] == 9.70, c


def test_non_gate_ask_user_has_no_cost():
    # An ordinary (non Confirm/Discard) ask_user carries no cost estimate.
    ask = _tool_use("ask_user", "tu_ask",
                    {"question": "Which regulator?", "options": ["LDO", "Buck"]})
    queue = [(_resp("tool_use", [ask], _usage()), _hdr("0.1", "9.9"))]
    undo = _patch(queue)
    try:
        events = list(agent_loop.run_chat("c_gate2", "power it"))
    finally:
        undo()
    ai = [e for e in events if e.get("kind") == "ai"]
    assert ai and all(not e.get("cost") for e in ai), ai


def test_cap_disabled_by_default_does_not_suspend():
    assert settings.pinflow_credit_cap_per_request == 0.0  # default off
    # Big spend (5.0 credits) but cap off → reach the boundary, don't gate.
    queue = [
        (_resp("tool_use", [_tool_use("remove_components", "tu_x")], _usage()), _hdr("5.0", "5.0")),
        (_resp("end_turn", [_text("done")], _usage()), _hdr("0.0", "5.0")),
    ]
    undo = _patch(queue)
    try:
        events = list(agent_loop.run_chat("c_nocap", "hi"))
    finally:
        undo()
    assert not any(e.get("kind") == "suspended" for e in events)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} cost-loop checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
