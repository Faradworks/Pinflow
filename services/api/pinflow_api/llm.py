"""Single entry point for constructing Anthropic clients.

This is the load-bearing seam for the onboarding "LLM provider" choice.
EVERY Anthropic client in the codebase is built
here so the self-hosted (bring-your-own-key) path and the Pinflow Cloud (metered
gateway) path differ in exactly one place — and so the eventual Fork B migration
(move the agent loop to the cloud) never has to hunt down scattered
`anthropic.Anthropic(...)` calls. Do not construct clients directly elsewhere.

Resolution order for the credential + endpoint:

1. An explicit `cfg` argument — the agent loop passes `state.llm`.
2. A request-scoped contextvar set by `llm_scope(cfg)` — lets tool-internal
   calls (e.g. `parse_datasheet` building its own client) be metered without
   threading a config object through every tool signature.
3. The process settings / `.env` — the dev, self-hosted-service, and
   smoke-test path. When nothing else is set, behavior is identical to before
   this seam existed.

Providers:

- `self`          → `Anthropic(api_key=…)` against Anthropic's default endpoint.
                    Key from a request header (desktop BYO-keys) or `.env`.
- `pinflow-cloud` → `Anthropic(base_url=<gateway>, api_key=<Pinflow JWT>)`. The
                    SDK sends the key as `x-api-key`; the gateway reads the JWT
                    there, gates on credit balance, and proxies to Anthropic.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Iterator, NamedTuple, Optional

from anthropic import Anthropic

from pinflow_api import cloud_session
from pinflow_api.settings import settings

PROVIDER_SELF = "self"
PROVIDER_CLOUD = "pinflow-cloud"

NOT_CONFIGURED_MSG = (
    "No LLM provider configured — set ANTHROPIC_API_KEY in services/api/.env "
    "(self-hosted), or sign in to Pinflow Cloud."
)


@dataclass(frozen=True)
class LLMConfig:
    """Per-request LLM routing. Every field optional; None inherits from settings.

    Built from request headers (`config_from_headers`) and stashed on
    `ConversationState.llm` so a suspended conversation resumes on the same
    provider.
    """

    provider: Optional[str] = None  # PROVIDER_SELF | PROVIDER_CLOUD
    api_key: Optional[str] = None   # Anthropic key (self) OR Pinflow JWT (cloud)
    base_url: Optional[str] = None  # gateway base_url (cloud); None = default


# Set by llm_scope(); read by make_client()/available() when no explicit cfg is
# passed. Default None → fall through to settings.
_current: "contextvars.ContextVar[Optional[LLMConfig]]" = contextvars.ContextVar(
    "pinflow_llm", default=None
)


class _Resolved(NamedTuple):
    provider: str
    api_key: str
    base_url: Optional[str]


def _resolve(cfg: Optional[LLMConfig]) -> _Resolved:
    cfg = cfg if cfg is not None else _current.get()
    provider = (getattr(cfg, "provider", None) or settings.pinflow_llm_provider
                or PROVIDER_SELF)
    if provider == PROVIDER_CLOUD:
        # Credential precedence: an explicit per-request token (Authorization:
        # Bearer), else the desktop-login session token held by this process
        # (routes/auth.py → cloud_session). The token never comes from the
        # renderer in the normal flow — the local service holds it.
        api_key = getattr(cfg, "api_key", None) or cloud_session.get_token() or ""
        base_url = getattr(cfg, "base_url", None) or settings.pinflow_cloud_url or ""
        return _Resolved(PROVIDER_CLOUD, api_key, base_url or None)
    # self (and any unknown provider falls back to self for safety)
    api_key = getattr(cfg, "api_key", None) or settings.anthropic_api_key or ""
    return _Resolved(PROVIDER_SELF, api_key, None)


def available(cfg: Optional[LLMConfig] = None) -> bool:
    """True if make_client(cfg) would succeed. Replaces the scattered
    `if not settings.anthropic_api_key` guards so the cloud path (which has no
    local key, only a JWT) isn't wrongly rejected."""
    r = _resolve(cfg)
    if r.provider == PROVIDER_CLOUD:
        return bool(r.api_key and r.base_url)
    return bool(r.api_key)


def make_client(cfg: Optional[LLMConfig] = None) -> Anthropic:
    """Construct an Anthropic client for the resolved provider."""
    r = _resolve(cfg)
    if r.provider == PROVIDER_CLOUD:
        if not (r.api_key and r.base_url):
            raise RuntimeError(NOT_CONFIGURED_MSG)
        return Anthropic(api_key=r.api_key, base_url=r.base_url)
    if not r.api_key:
        raise RuntimeError(NOT_CONFIGURED_MSG)
    return Anthropic(api_key=r.api_key)


@contextlib.contextmanager
def llm_scope(cfg: Optional[LLMConfig]) -> Iterator[None]:
    """Bind `cfg` as the ambient config for the duration of a synchronous block
    (no `yield` to the event loop inside). The agent loop wraps tool dispatch in
    this so a tool's own `make_client()` resolves to the same provider as the
    turn. `cfg=None` is a no-op (resolution falls through to settings)."""
    token = _current.set(cfg)
    try:
        yield
    finally:
        _current.reset(token)


def current() -> Optional[LLMConfig]:
    """The ambient request config, if any (for diagnostics/tracing)."""
    return _current.get()


def config_from_headers(headers) -> Optional[LLMConfig]:
    """Build an LLMConfig from request headers, or None when no LLM-routing
    header is present (preserving the pure-settings path).

    Headers (case-insensitive):
      X-Pinflow-LLM-Provider: self | pinflow-cloud
      X-Anthropic-Api-Key:    user's own key (self, BYO-keys)
      Authorization:          Bearer <Pinflow JWT> (pinflow-cloud)
    """
    provider = (headers.get("x-pinflow-llm-provider") or "").strip().lower() or None
    auth = headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None
    byo_key = headers.get("x-anthropic-api-key") or None

    if not provider and not bearer and not byo_key:
        return None  # nothing to route — keep the settings/.env path

    if provider == PROVIDER_CLOUD or (provider is None and bearer):
        return LLMConfig(provider=PROVIDER_CLOUD, api_key=bearer)
    return LLMConfig(provider=PROVIDER_SELF, api_key=byo_key)
