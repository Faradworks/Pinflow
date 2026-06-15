"""Offline unit checks for pinflow_api.cost — USD pricing, the gateway-header
reader, and CostMeter balance-delta accounting. Pure stdlib (no Anthropic /
network), so it runs in check_all.py's offline gate and via:

    python3 scripts/test_cost.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pinflow_api import cost  # noqa: E402


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class _Headers:
    """Minimal case-insensitive .get, like httpx.Headers."""

    def __init__(self, d: dict):
        self._d = {k.lower(): v for k, v in d.items()}

    def get(self, k: str) -> Optional[str]:
        return self._d.get(k.lower())


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def test_call_cost_usd_opus():
    # 1M input + 1M output on Opus 4.8 = $5 + $25 = $30.
    usd = cost.call_cost_usd("claude-opus-4-8", _Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert _approx(usd, 30.0), usd


def test_cache_multipliers():
    # 1M cache-read on Opus at 0.1× input ($5) = $0.50; 1M cache-write at 1.25× = $6.25.
    read = cost.call_cost_usd("claude-opus-4-8", _Usage(cache_read_input_tokens=1_000_000))
    write = cost.call_cost_usd("claude-opus-4-8", _Usage(cache_creation_input_tokens=1_000_000))
    assert _approx(read, 0.50), read
    assert _approx(write, 6.25), write


def test_sonnet_pricing():
    usd = cost.call_cost_usd("claude-sonnet-4-6", _Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert _approx(usd, 18.0), usd


def test_unknown_model_falls_back_to_opus():
    a = cost.call_cost_usd("some-future-model", _Usage(input_tokens=1_000_000))
    b = cost.call_cost_usd("claude-opus-4-8", _Usage(input_tokens=1_000_000))
    assert _approx(a, b), (a, b)


def test_usage_missing_fields_is_zero():
    # A usage object with no cache fields (or None) must not raise.
    assert cost.call_cost_usd("claude-opus-4-8", None) == 0.0
    assert cost.call_cost_usd("claude-opus-4-8", _Usage()) == 0.0


def test_parse_gateway_credits():
    charged, balance = cost.parse_gateway_credits(
        _Headers({"X-Pinflow-Credits-Charged": "0.42", "X-Pinflow-Credits-Balance": "17.5"})
    )
    assert _approx(charged, 0.42) and _approx(balance, 17.5), (charged, balance)
    # Absent / None / unparseable → None, never a raise.
    assert cost.parse_gateway_credits(None) == (None, None)
    assert cost.parse_gateway_credits(_Headers({})) == (None, None)
    assert cost.parse_gateway_credits(_Headers({"x-pinflow-credits-charged": "n/a"})) == (None, None)


def test_meter_balance_delta_cloud():
    m = cost.CostMeter()
    # First cloud call: charged 0.5, balance 19.5 → pre-call 20.0 (start anchor).
    m.record(charged=0.5, balance=19.5, usd=0.45)
    assert _approx(m.request_credits, 0.5) and _approx(m.conversation_credits, 0.5)
    assert m.request_estimated is False and _approx(m.last_balance, 19.5)
    # A tool-internal call the loop never sees still lands in the delta via the
    # next balance the loop DOES see (balance drops by 1.5 more, spent anywhere).
    m.record(charged=0.3, balance=18.0, usd=0.27)
    assert _approx(m.request_credits, 2.0), m.request_credits  # 20.0 - 18.0
    # reset_request clears the request anchor but keeps the conversation one.
    m.reset_request()
    assert m.request_credits == 0.0 and m.approved_ceiling is None
    assert _approx(m.conversation_credits, 2.0)
    # The next request's first call re-anchors the request start from its pre-call.
    m.record(charged=0.1, balance=17.9, usd=0.09)
    assert _approx(m.request_credits, 0.1)       # 18.0 - 17.9
    assert _approx(m.conversation_credits, 2.1)  # 20.0 - 17.9


def test_meter_self_path_usd_estimate():
    m = cost.CostMeter()
    # No gateway balance → USD estimate accrues, credits stay 0, estimated flips.
    m.record(charged=None, balance=None, usd=0.45)
    m.record(charged=None, balance=None, usd=0.30)
    assert m.request_credits == 0.0 and m.conversation_credits == 0.0
    assert _approx(m.request_usd, 0.75) and m.request_estimated is True
    m.reset_request()
    assert m.request_usd == 0.0 and m.request_estimated is False
    assert _approx(m.conversation_usd, 0.75)  # conversation total survives reset


def test_token_tracking():
    m = cost.CostMeter()
    u = _Usage(input_tokens=100, output_tokens=50, cache_creation_input_tokens=10, cache_read_input_tokens=200)
    m.record(charged=None, balance=None, usd=0.01, usage=u)   # self/BYOK path
    assert m.request_input_tokens == 100 and m.request_output_tokens == 50
    assert m.request_cache_tokens == 210                      # creation + read
    assert m.request_tokens == 360 and m.conversation_tokens == 360
    # Cloud path tracks tokens too (alongside the balance-delta credits).
    m.record(charged=0.5, balance=9.5, usd=0.0, usage=_Usage(input_tokens=40))
    assert m.request_tokens == 400 and m.conversation_tokens == 400
    m.reset_request()
    assert m.request_tokens == 0 and m.conversation_tokens == 400  # conversation survives reset


def test_credit_ratio_observed_not_hardcoded():
    m = cost.CostMeter()
    assert m.credit_ratio == 1.0  # default until a real charge is seen
    m.record(charged=0.30, balance=9.70, usd=0.20)   # synthetic fixture ratio, not a real rate
    assert _approx(m.credit_ratio, 1.5), m.credit_ratio
    m.record(charged=0.15, balance=9.55, usd=0.10)
    assert _approx(m.credit_ratio, 0.45 / 0.30)      # accumulated, still 1.5


def test_estimate_followup_usd_scales_with_turns():
    one = cost.estimate_followup_usd("claude-opus-4-8", 30_000, turns=1)
    three = cost.estimate_followup_usd("claude-opus-4-8", 30_000, turns=3)
    assert one > 0 and _approx(three, one * 3)
    assert cost.estimate_followup_usd("claude-opus-4-8", 30_000, turns=0) == 0.0


def test_gate_estimate_cloud_credits_and_self_usd():
    m = cost.CostMeter()
    m.record(charged=0.30, balance=9.70, usd=0.20)   # synthetic fixture ratio, balance 9.70
    msgs = [{"role": "user", "content": "x" * 8000}]

    cloud = cost.gate_estimate(m, "claude-opus-4-8", msgs, staged=False, provider="pinflow-cloud")
    assert cloud["unit"] == "credits" and cloud["hi"] > cloud["lo"] > 0
    assert cloud["balance"] == 9.70

    # Self/BYOK → USD, no balance, no credit margin applied.
    selfp = cost.gate_estimate(m, "claude-opus-4-8", msgs, staged=True, provider="self")
    assert selfp["unit"] == "usd" and selfp["balance"] is None

    # The post-stage commit gate (1 turn) is cheaper than the design gate (3 turns).
    cloud_commit = cost.gate_estimate(m, "claude-opus-4-8", msgs, staged=True, provider="pinflow-cloud")
    assert cloud_commit["hi"] < cloud["hi"]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} cost checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
