"""Smoke test: two ask_user calls in one model response must not brick the
conversation.

Regression for the 2026-06-12 scenario-battery find: the model emitted two
`ask_user` tool_use blocks in a single response. The loop deferred only the
last one, the resume answer reattached to that id, and the FIRST ask_user's
tool_use was left without a tool_result — Anthropic then rejected every
subsequent request on the conversation with a 400 ("`tool_use` ids were found
without `tool_result` blocks"), permanently dead-ending the chat.

Drives the real run_chat/run_resume with a stubbed Anthropic client (no
network, no key). The stub's second create() call performs the same pairing
check the API does and raises if any tool_use id lacks a tool_result.

Run: cd services/api && .venv/bin/python scripts/test_ask_user_dedup.py
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pinflow_api import llm  # noqa: E402
from pinflow_api.agent import loop as agent_loop  # noqa: E402
from pinflow_api.agent import state as st  # noqa: E402


def _block(**kw):
    return SimpleNamespace(**kw)


def _response(blocks, stop_reason):
    return SimpleNamespace(
        content=blocks, stop_reason=stop_reason, usage=None)


def _check_pairing(messages):
    """The API-side invariant: every assistant tool_use id must have a
    tool_result in the immediately following user message."""
    for i, m in enumerate(messages):
        content = m.get("content")
        if m.get("role") != "assistant" or isinstance(content, str):
            continue
        ids = {b["id"] for b in content
               if isinstance(b, dict) and b.get("type") == "tool_use"}
        if not ids:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else {}
        nxt_content = nxt.get("content") or []
        answered = {b.get("tool_use_id") for b in nxt_content
                    if isinstance(b, dict) and b.get("type") == "tool_result"}
        dangling = ids - answered
        if dangling:
            raise AssertionError(
                f"messages.{i}: tool_use ids without tool_result: {dangling}")


class _RawWrap:
    """Mimic the SDK's with_raw_response wrapper: .parse() → the Message, plus
    .headers (the loop reads gateway credit headers off this; None here)."""

    def __init__(self, response):
        self._response = response
        self.headers = None

    def parse(self):
        return self._response


class _StubClient:
    def __init__(self):
        self.calls = 0
        # The loop calls messages.with_raw_response.create(...).
        self.messages = types.SimpleNamespace(
            with_raw_response=types.SimpleNamespace(
                create=lambda **kw: _RawWrap(self._create(**kw))
            )
        )

    def _create(self, *, messages, **_kw):
        self.calls += 1
        if self.calls == 1:
            # The pathological response: text + TWO ask_user tool_uses.
            return _response(
                [
                    _block(type="text", text="Two quick questions."),
                    _block(type="tool_use", id="toolu_first", name="ask_user",
                           input={"question": "Include the USB-C block?",
                                  "options": ["Yes", "No"]}),
                    _block(type="tool_use", id="toolu_second", name="ask_user",
                           input={"question": "Which regulator?",
                                  "options": ["LDO", "Buck"]}),
                ],
                "tool_use",
            )
        # Every later call replays the conversation — enforce the invariant
        # the real API enforces, then end the turn.
        _check_pairing(messages)
        return _response([_block(type="text", text="ok, done.")], "end_turn")


def main() -> int:
    stub = _StubClient()
    llm.make_client = lambda cfg=None: stub
    llm.available = lambda cfg=None: True

    conv = "c_test_askdedup"
    events = list(agent_loop.run_chat(conv, "power my sensor at 1.8V"))
    kinds = [e.get("kind") for e in events]
    assert "suspended" in kinds, f"expected suspension, got {kinds}"

    state = st.get_or_create(conv)
    # Exactly one pending question, and it is the FIRST ask_user.
    assert state.pending_question is not None
    assert state.pending_question.tool_use_id == "toolu_first", (
        state.pending_question.tool_use_id)
    # The duplicate already has its synthetic result stashed for the flush.
    stashed = {r.get("tool_use_id") for r in state.pending_tool_results}
    assert "toolu_second" in stashed, stashed

    events = list(agent_loop.run_resume(conv, "Yes"))
    texts = " ".join(json.dumps(e) for e in events)
    assert "anthropic error" not in texts, texts
    assert stub.calls >= 2, "resume never reached the model"

    print("PASS  duplicate ask_user is superseded; conversation survives resume")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
