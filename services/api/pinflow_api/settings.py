"""App-level settings loaded from env / .env.

The .env path is anchored to the package directory so that uvicorn picks it up
regardless of cwd (dev.sh runs from the repo root).
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"  # services/api/.env


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    # The chat agent loop (routes/agent.py -> agent/loop.py) is the hero path and
    # benefits from a stronger reasoner: it must author netlists that satisfy the
    # deterministic placer/validator in as few attempts as possible, or it burns
    # turns. It runs on this model; datasheet extraction / emit stay on
    # `anthropic_model` (Sonnet — validated for native-PDF input, cheaper for bulk
    # calls). Override via env if Opus is unavailable or too costly.
    anthropic_agent_model: str = "claude-opus-4-8"

    # LLM provider routing (see pinflow_api/llm.py).
    # "self" = call Anthropic directly with anthropic_api_key (self-hosted /
    # bring-your-own-key, the open-source default). "pinflow-cloud" = route LLM
    # calls through the Pinflow metering gateway at pinflow_cloud_url, billed
    # against the signed-in user's credits. Requests can override per-call via
    # headers; this is just the process default when no header is sent.
    pinflow_llm_provider: str = "self"
    pinflow_cloud_url: str = ""               # Pinflow Cloud base URL — routes LLM calls + parts queries
    # Hosted Clerk sign-in helper page for the desktop login flow (routes/auth.py).
    # Empty → /auth/start falls back to the built-in Clerk page (/auth/clerk) when
    # pinflow_clerk_publishable_key is set, else the no-Clerk /auth/dev stub.
    pinflow_login_url: str = ""
    # Clerk publishable key (pk_test_… / pk_live_…) for the desktop login flow.
    # When set (and pinflow_login_url is empty), /auth/start serves a built-in
    # Clerk sign-in page at /auth/clerk — same-origin with the callback, so no
    # separately-hosted helper page is needed (this is the dev / staging-Clerk
    # path). Non-secret by design: a publishable key ships in frontend code.
    pinflow_clerk_publishable_key: str = ""

    # Per-request LLM spend cap, in credits. When > 0, the agent loop pauses and
    # asks the user to Continue/Stop the FIRST time a single user message's running
    # cost crosses this ceiling; Continue approves the rest of that request (it
    # won't ask again until the next message). 0 disables the gate — the live meter
    # still shows. See the cost-cap flow in agent/loop.py and pinflow_api/cost.py.
    pinflow_credit_cap_per_request: float = 0.0


settings = Settings()
