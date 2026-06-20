// Persisted onboarding / backend configuration (localStorage-backed), plus the
// runtime API-base + LLM-routing headers the api layer reads.
//
// Mirrors lib/theme.ts. The module holds the live config so api.ts
// (getApiBase / getLlmHeaders) reflects changes the instant they're saved,
// while App.tsx keeps a React-state mirror to drive re-rendering.

const KEY = "pinflow.config";
const BYOK_KEY = "pinflow.byok_required";

export type LlmMode = "cloud" | "self";
/** Hero-loop model. `opus` is the default (strongest); `sonnet` is cheaper/faster. */
export type AgentModel = "opus" | "sonnet";

export interface PinflowConfig {
  mode: LlmMode;
  /** Anthropic API key — `self` (bring-your-own-key) mode. */
  anthropicKey?: string;
  /** Agent model preference; defaults to `opus` when unset. */
  model?: AgentModel;
}

const DEFAULT_API_BASE: string =
  (import.meta as any).env?.VITE_PINFLOW_API_URL ?? "http://127.0.0.1:8787";

let _config: PinflowConfig | null = readConfig();

// BYOK-required mode (server-side Anthropic key disabled). Gateway-served via
// /cloud/credits; cached here + persisted so getLlmHeaders() routes correctly on
// the very next chat send and before the first credits fetch resolves at launch.
let _serverLlmDisabled: boolean = (() => {
  try {
    return localStorage.getItem(BYOK_KEY) === "1";
  } catch {
    return false;
  }
})();

export function isServerLlmDisabled(): boolean {
  return _serverLlmDisabled;
}

export function setServerLlmDisabled(disabled: boolean): void {
  _serverLlmDisabled = disabled;
  try {
    if (disabled) localStorage.setItem(BYOK_KEY, "1");
    else localStorage.removeItem(BYOK_KEY);
  } catch {}
}

function readConfig(): PinflowConfig | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (p && (p.mode === "cloud" || p.mode === "self")) {
      return p as PinflowConfig;
    }
  } catch {}
  return null;
}

export function getConfig(): PinflowConfig | null {
  return _config;
}

export function isOnboarded(): boolean {
  return _config !== null;
}

export function saveConfig(cfg: PinflowConfig): void {
  _config = cfg;
  try {
    localStorage.setItem(KEY, JSON.stringify(cfg));
  } catch {}
}

export function clearConfig(): void {
  _config = null;
  try {
    localStorage.removeItem(KEY);
  } catch {}
}

/** Base URL for the Pinflow API service — the bundled-local default, or the
 *  build-time `VITE_PINFLOW_API_URL` override (dev / self-host-from-source). */
export function getApiBase(): string {
  return DEFAULT_API_BASE;
}

/** Headers telling the backend which LLM provider + agent model to use for a turn.
 *  The model header is always sent (defaults to opus); the provider headers are
 *  added per mode. Provider unset (self-without-key) → backend uses its .env key. */
export function getLlmHeaders(): Record<string, string> {
  const c = _config;
  const headers: Record<string, string> = {
    "X-Pinflow-Agent-Model": c?.model === "sonnet" ? "sonnet" : "opus",
  };
  // BYOK-required mode: the server-side key is disabled, so never route LLM
  // through the cloud gateway — force the user's own key regardless of mode. The
  // cloud session (parts search) is attached server-side and is unaffected.
  if (_serverLlmDisabled) {
    if (c?.anthropicKey) {
      headers["X-Pinflow-LLM-Provider"] = "self";
      headers["X-Anthropic-Api-Key"] = c.anthropicKey;
    }
    return headers;
  }
  if (c?.mode === "self" && c.anthropicKey) {
    headers["X-Pinflow-LLM-Provider"] = "self";
    headers["X-Anthropic-Api-Key"] = c.anthropicKey;
  } else if (c?.mode === "cloud") {
    // The session JWT is held by the local service (cloud_session) and attached
    // server-side, so the desktop only flags the provider here.
    headers["X-Pinflow-LLM-Provider"] = "pinflow-cloud";
  }
  return headers;
}
