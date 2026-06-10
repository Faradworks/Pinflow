// Persisted onboarding / backend configuration (localStorage-backed), plus the
// runtime API-base + LLM-routing headers the api layer reads.
//
// Mirrors lib/theme.ts. The module holds the live config so api.ts
// (getApiBase / getLlmHeaders) reflects changes the instant they're saved,
// while App.tsx keeps a React-state mirror to drive re-rendering.

const KEY = "pinflow.config";

export type LlmMode = "cloud" | "self";

export interface PinflowConfig {
  mode: LlmMode;
  /** Anthropic API key — `self` (bring-your-own-key) mode. */
  anthropicKey?: string;
}

const DEFAULT_API_BASE: string =
  (import.meta as any).env?.VITE_PINFLOW_API_URL ?? "http://127.0.0.1:8787";

let _config: PinflowConfig | null = readConfig();

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

/** Headers telling the backend which LLM provider to route a chat turn to.
 *  Empty when nothing applies (→ backend falls back to its own .env key). */
export function getLlmHeaders(): Record<string, string> {
  const c = _config;
  if (c?.mode === "self" && c.anthropicKey) {
    return {
      "X-Pinflow-LLM-Provider": "self",
      "X-Anthropic-Api-Key": c.anthropicKey,
    };
  }
  if (c?.mode === "cloud") {
    // The session JWT is held by the local service (cloud_session) and attached
    // server-side, so the desktop only flags the provider here.
    return { "X-Pinflow-LLM-Provider": "pinflow-cloud" };
  }
  // unset / self-without-key: the local service uses its own configured .env key.
  return {};
}
