"""LLM cost / credit metering for the chat agent loop.

The Pinflow Cloud gateway is the source of truth for credits. On each metered
`/v1/messages` call it returns two response headers:

    X-Pinflow-Credits-Charged   credits debited for THIS call          (float)
    X-Pinflow-Credits-Balance   the user's remaining balance after it  (float)

This module turns those into a live meter, with two paths:

  * **Cloud** — request/conversation spend is the **balance delta**: the balance
    just before the request started, minus the latest balance. That captures
    EVERY metered call against the account, including the extra calls a tool
    makes internally (parse_datasheet's PDF read, design_spec) — not just the
    agent loop's own calls. The per-call Charged header is used only to
    reconstruct the pre-call starting balance from the first call's response.

  * **Self / BYO-key** — there is no gateway and no credit balance; the user pays
    Anthropic directly. We show a best-effort **USD** estimate from public list
    prices instead. No Pinflow credit margin lives in this open-source client —
    the markup is the gateway's business and is deliberately kept server-side.

`call_cost_usd` only powers the self-path USD readout; on the cloud path credits
come straight from the gateway, so the client never needs to know the margin.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

# Per-1M-token USD list price (input, output), from the public Anthropic pricing
# table (https://docs.anthropic.com/en/docs/about-claude/pricing, 2026-06).
# Used only for the self/BYOK USD estimate. Unknown models fall back to the Opus
# rate — conservative, so an unrecognized model errs high rather than low.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (5.0, 25.0)

# Prompt-cache pricing relative to the base input rate (the loop caches with the
# default 5-minute ephemeral TTL: writes 1.25×, reads 0.1×). The API's
# `input_tokens` is the *uncached* remainder; cache_creation/cache_read are
# separate buckets priced at their own multipliers.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


def call_cost_usd(model: str, usage: Any) -> float:
    """Raw Anthropic USD cost for one response, from its `usage` object. Powers
    the self/BYOK USD estimate only. Defensive on the usage shape (any missing
    field counts as 0) so a stub or a cache-field-less provider can't raise."""
    in_rate, out_rate = _PRICING.get(model, _DEFAULT_PRICE)
    inp = _int(getattr(usage, "input_tokens", 0))
    out = _int(getattr(usage, "output_tokens", 0))
    cache_write = _int(getattr(usage, "cache_creation_input_tokens", 0))
    cache_read = _int(getattr(usage, "cache_read_input_tokens", 0))
    return (
        inp * in_rate
        + out * out_rate
        + cache_write * in_rate * _CACHE_WRITE_MULT
        + cache_read * in_rate * _CACHE_READ_MULT
    ) / 1_000_000


def parse_gateway_credits(headers: Any) -> tuple[Optional[float], Optional[float]]:
    """`(charged, balance)` from the gateway's per-call headers, each None when
    absent/unparseable. `headers` is an httpx.Headers (case-insensitive `.get`)
    or None (the self/BYOK path, where there is no gateway)."""
    if headers is None:
        return None, None
    get = getattr(headers, "get", None)
    if get is None:
        return None, None
    return _float(get("x-pinflow-credits-charged")), _float(get("x-pinflow-credits-balance"))


@dataclass
class CostMeter:
    """Running cost tally for one conversation, surfaced live to the UI.

    Cloud path (authoritative, credits): spend is `*_start_balance - last_balance`.
    `*_start_balance` is reconstructed once, from the first metered call of the
    scope, as `balance_after_call + charged_this_call`. Self/BYOK path (estimate,
    USD): `*_usd` is the summed local cost, and `*_estimated` flips True (the UI
    shows the USD figure with a `~`). `approved_ceiling` is the per-request spend
    gate's granted headroom (see the cost-cap flow in agent/loop.py).
    """

    conversation_start_balance: Optional[float] = None
    request_start_balance: Optional[float] = None
    last_balance: Optional[float] = None
    request_estimated: bool = False
    conversation_estimated: bool = False
    approved_ceiling: Optional[float] = None
    # Token counts (input / output / cache = creation+read), shown to the user
    # alongside credits (cloud) or USD (BYOK). Provider-agnostic — always tracked
    # from the response usage. NB: this is the agent loop's OWN calls; tokens a
    # tool spends internally (e.g. parse_datasheet's PDF read) aren't visible here
    # — on cloud those still land in the balance-delta credits, just not in tokens.
    request_input_tokens: int = 0
    request_output_tokens: int = 0
    request_cache_tokens: int = 0
    conversation_input_tokens: int = 0
    conversation_output_tokens: int = 0
    conversation_cache_tokens: int = 0
    # Observed credits-per-USD, accumulated from real gateway charges so the
    # forward gate estimate (gate_estimate) can convert a USD projection into
    # credits WITHOUT hardcoding the gateway's margin — we measure it instead.
    _charged_sum: float = 0.0
    _usd_sum: float = 0.0

    def reset_request(self) -> None:
        self.request_start_balance = None
        self.request_estimated = False
        self.approved_ceiling = None
        self.request_input_tokens = 0
        self.request_output_tokens = 0
        self.request_cache_tokens = 0

    def start_segment(self) -> None:
        """Re-anchor the conversation-level credit/USD accumulators — called when
        the provider switches mid-conversation so "session" spend is measured from
        the switch, not a cloud+BYOK mix. Deliberately does NOT reset the token
        tallies (they span providers cleanly) or the observed credit_ratio (a
        property of the gateway, not the segment). Per-request fields reset
        separately via reset_request()."""
        self.conversation_start_balance = None
        self.request_start_balance = None
        self.last_balance = None
        self.conversation_estimated = False

    @property
    def request_tokens(self) -> int:
        return self.request_input_tokens + self.request_output_tokens + self.request_cache_tokens

    @property
    def conversation_tokens(self) -> int:
        return (self.conversation_input_tokens + self.conversation_output_tokens
                + self.conversation_cache_tokens)

    @property
    def credit_ratio(self) -> float:
        """Observed credits per USD, measured from real gateway charges. 1.0 until a
        charge is seen — the markup is the gateway's; we measure, never hardcode it."""
        return self._charged_sum / self._usd_sum if self._usd_sum > 0 else 1.0

    def record(
        self,
        *,
        charged: Optional[float],
        balance: Optional[float],
        usd: float,
        usage: Any = None,
    ) -> None:
        """Fold one LLM call into the meter. `balance`/`charged` from the gateway
        headers (cloud), `usd` the local estimate (used only off-gateway), `usage`
        the Anthropic usage object (for token counts; provider-agnostic)."""
        if usage is not None:
            inp = _int(getattr(usage, "input_tokens", 0))
            out = _int(getattr(usage, "output_tokens", 0))
            cache = (_int(getattr(usage, "cache_creation_input_tokens", 0))
                     + _int(getattr(usage, "cache_read_input_tokens", 0)))
            self.request_input_tokens += inp
            self.request_output_tokens += out
            self.request_cache_tokens += cache
            self.conversation_input_tokens += inp
            self.conversation_output_tokens += out
            self.conversation_cache_tokens += cache
        if balance is not None:
            # Authoritative cloud path. Reconstruct the pre-call balance to anchor
            # the deltas on first sight of each scope.
            pre_call = balance + (charged or 0.0)
            if self.conversation_start_balance is None:
                self.conversation_start_balance = pre_call
            if self.request_start_balance is None:
                self.request_start_balance = pre_call
            self.last_balance = balance
            # Learn the gateway's effective credits/USD from this call.
            if charged is not None and usd > 0:
                self._charged_sum += charged
                self._usd_sum += usd
        else:
            # No gateway charge — BYOK/self path. No USD figure is surfaced
            # (tokens-only); just flag the scope estimated so the UI shows tokens
            # rather than credits.
            self.request_estimated = True
            self.conversation_estimated = True

    @property
    def request_credits(self) -> float:
        return self._delta(self.request_start_balance)

    @property
    def conversation_credits(self) -> float:
        return self._delta(self.conversation_start_balance)

    def _delta(self, start: Optional[float]) -> float:
        if start is None or self.last_balance is None:
            return 0.0
        return max(0.0, round(start - self.last_balance, 4))


# --- Forward "cost to finish" estimate for the Confirm/Discard gates -----------
# A deliberately fuzzy, honest range — there is no exact pre-execution number for
# an agent. It works because at a gate the remaining work is bounded and known
# (place + resolve parts + summarize), and dominated by a few agent turns at the
# current — already-cached — context size. The big one-time cost (the datasheet
# read) is already spent by the design-spec gate, so this number is correctly
# small: it tells the user "finishing from here is cheap."

_EST_OUTPUT_TOKENS_PER_TURN = 1500   # a tool-call or short summary turn
_GATE_SYSTEM_TOOLS_TOKENS = 9000     # _SYSTEM + tool schemas, ~fixed per turn
# Expected remaining agent turns by gate: the post-stage commit gate needs only a
# summary turn; the design-spec gate still has place + resolve_parts ahead of it.
_TURNS_COMMIT_GATE = 1
_TURNS_DESIGN_GATE = 3


def approx_context_tokens(messages: list) -> int:
    """Very rough per-turn prompt size: the conversation so far + the fixed
    system/tools overhead, at ~4 chars/token. Only feeds the fuzzy gate estimate,
    so precision doesn't matter."""
    chars = 0
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        chars += len(c) if isinstance(c, str) else len(json.dumps(c, default=str))
    return chars // 4 + _GATE_SYSTEM_TOOLS_TOKENS


def estimate_followup_usd(model: str, context_tokens: int, *, turns: int) -> float:
    """Rough USD for `turns` more agent turns at the current context size. Per
    turn ≈ the cached context re-read (cache-read rate, since the prefix is
    cached) + a small fresh output."""
    in_rate, out_rate = _PRICING.get(model, _DEFAULT_PRICE)
    per_turn = (
        context_tokens * in_rate * _CACHE_READ_MULT
        + _EST_OUTPUT_TOKENS_PER_TURN * out_rate
    ) / 1_000_000
    return per_turn * max(0, turns)


def gate_estimate(
    meter: "CostMeter",
    model: str,
    messages: list,
    *,
    staged: bool,
    provider: str,
) -> Optional[dict]:
    """Fuzzy 'cost to finish from here' for a Confirm/Discard gate, as a range.
    Credits on the cloud path (USD projection × the observed credit ratio), and
    nothing on self/BYOK — a client-side USD figure would be misleading there, so
    the gate shows no forward estimate. None when there's nothing worth showing."""
    turns = _TURNS_COMMIT_GATE if staged else _TURNS_DESIGN_GATE
    usd = estimate_followup_usd(model, approx_context_tokens(messages), turns=turns)
    if usd <= 0:
        return None
    if provider == "pinflow-cloud":
        credits = usd * meter.credit_ratio
        return {
            "unit": "credits",
            "lo": round(credits * 0.5, 2),
            "hi": round(credits * 1.5, 2),
            "balance": meter.last_balance,
        }
    # Self/BYOK: no authoritative cost source, and a client-side USD figure would
    # be misleading (hand-kept price table; misses tool-internal tokens). Show no
    # forward estimate rather than a quietly-wrong one — consistent with the
    # per-request meter line, which is tokens-only for BYOK.
    return None


def _int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
