// Base URL + LLM-routing headers come from the onboarding config (lib/config),
// so they update at runtime when the user switches provider / custom backend.
import { getApiBase, getLlmHeaders } from "./config";

export type CloudAuthStatus = {
  signed_in: boolean;
  pending: boolean;
  user_id?: string | null;
  email?: string | null;
  name?: string | null;
};

export type CloudCredits = {
  signed_in: boolean;
  configured: boolean;
  balance?: number;
  next_expiry?: string | null;
  error?: string;
};

// Live LLM-cost meter, applied from `cost` SSE events (see lib backend
// agent/events.py::ev_cost). `requestCredits` is the running spend for the
// current user message; `estimated` true → a local token→credit estimate
// (rendered with a ~) rather than an authoritative gateway charge.
export type CostInfo = {
  requestCredits: number;
  requestUsd: number;
  conversationCredits: number;
  estimated: boolean;
  balance: number | null;
  provider: string;
};

export type ActiveProject =
  | { detected: false }
  | {
      detected: true;
      name: string;
      schematic: string;
      path: string | null;
      schematic_path: string | null;
      schematic_source: string | null;
      staged: boolean;
      stage_stale: boolean;
      highlighted: boolean;
    };

export type StagePayload = {
  schematic_path: string;
  source: string;
  stale: boolean;
};

export type CommitResult = {
  file_written: boolean;
};

export type DiffResult = {
  diff: string | null;
  has_changes: boolean;
};

export type AttachmentRef = {
  attachment_id: string;
  filename: string;
  mime: string;
  size: number;
};

export type UploadResult = {
  conversation_id: string;
  attachments: AttachmentRef[];
};

// Streamed event from /agent/chat. `kind` matches the frontend Message
// union variants (`ai`, `tool`, `thinking`, `action`, `system`) plus a
// few control kinds (`meta`, `suspended`, `done`).
export type ChatEvent = {
  kind: string;
} & Record<string, any>;

export type StreamHandlers = {
  onEvent: (e: ChatEvent) => void;
  onDone?: () => void;
  onError?: (err: unknown) => void;
};

export type StreamHandle = { close: () => void };

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${getApiBase()}${path}`);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${getApiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    throw new Error(`${path} → ${r.status}${detail ? ` ${detail}` : ""}`);
  }
  return r.json();
}

export const api = {
  /** Probe the local service's /health. True once it's up. Gates the UI until
   *  the bundled backend (a ~15s cold-start sidecar in packaged builds) is
   *  reachable, so the first calls don't surface as WebKit "Load failed". */
  async health(): Promise<boolean> {
    try {
      const r = await fetch(`${getApiBase()}/health`);
      return r.ok;
    } catch {
      return false;
    }
  },

  async detectActiveProject(): Promise<ActiveProject> {
    return get<ActiveProject>("/kicad/active-project");
  },

  async stageSchematic(schematicPath: string): Promise<StagePayload> {
    return post<StagePayload>("/schematic/stage", { schematic_path: schematicPath });
  },

  async updateSchematicSource(
    schematicPath: string,
    source: string,
  ): Promise<StagePayload> {
    return post<StagePayload>("/schematic/update", {
      schematic_path: schematicPath,
      source,
    });
  },

  async commitSchematic(
    schematicPath: string,
    opts: { force?: boolean } = {},
  ): Promise<CommitResult> {
    return post<CommitResult>("/schematic/commit", {
      schematic_path: schematicPath,
      force: opts.force ?? false,
    });
  },

  async discardSchematic(schematicPath: string): Promise<{ discarded: boolean }> {
    return post<{ discarded: boolean }>("/schematic/discard", {
      schematic_path: schematicPath,
    });
  },

  async getSchematicDiff(schematicPath: string): Promise<DiffResult> {
    return get<DiffResult>(
      `/schematic/diff?schematic_path=${encodeURIComponent(schematicPath)}`,
    );
  },

  async uploadAttachments(
    conversationId: string | null,
    files: File[],
  ): Promise<UploadResult> {
    const form = new FormData();
    if (conversationId) form.append("conversation_id", conversationId);
    for (const f of files) form.append("files", f, f.name);
    const r = await fetch(`${getApiBase()}/agent/attachments`, {
      method: "POST",
      body: form,
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      throw new Error(`/agent/attachments → ${r.status}${detail ? ` ${detail}` : ""}`);
    }
    return r.json();
  },

  chatStream(
    userText: string,
    conversationId: string | null,
    handlers: StreamHandlers,
    attachmentIds: string[] = [],
  ): StreamHandle {
    return streamSSE(
      "/agent/chat",
      {
        user_text: userText,
        conversation_id: conversationId,
        attachment_ids: attachmentIds.length ? attachmentIds : undefined,
      },
      handlers,
    );
  },

  chatResume(
    conversationId: string,
    answer: string,
    handlers: StreamHandlers,
    attachmentIds: string[] = [],
  ): StreamHandle {
    return streamSSE(
      "/agent/chat/resume",
      {
        conversation_id: conversationId,
        answer,
        attachment_ids: attachmentIds.length ? attachmentIds : undefined,
      },
      handlers,
    );
  },

  // --- Pinflow Cloud login (always targets the local service) ---------------
  async startCloudLogin(): Promise<{ state: string; login_url: string; opened: boolean }> {
    const r = await fetch(`${getApiBase()}/auth/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!r.ok) throw new Error(`/auth/start → ${r.status}`);
    return r.json();
  },

  async cloudAuthStatus(state?: string): Promise<CloudAuthStatus> {
    const q = state ? `?state=${encodeURIComponent(state)}` : "";
    const r = await fetch(`${getApiBase()}/auth/status${q}`);
    if (!r.ok) throw new Error(`/auth/status → ${r.status}`);
    return r.json();
  },

  async cloudLogout(): Promise<void> {
    await fetch(`${getApiBase()}/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  },

  // Full sign-out: clears the Pinflow session AND the Clerk browser session
  // (via /auth/signout), so a different account can sign in next.
  async cloudSignout(): Promise<{ opened: boolean; signout_url: string | null }> {
    return post<{ opened: boolean; signout_url: string | null }>(
      "/auth/signout-start",
      {},
    );
  },

  // Free BYOK key check via the local service → Anthropic GET /v1/models.
  async validateAnthropicKey(
    key: string,
  ): Promise<{ valid: boolean; unknown?: boolean; error?: string }> {
    const r = await fetch(`${getApiBase()}/auth/validate-key`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    });
    if (!r.ok) throw new Error(`/auth/validate-key → ${r.status}`);
    return r.json();
  },

  async cloudCredits(): Promise<CloudCredits> {
    const r = await fetch(`${getApiBase()}/cloud/credits`);
    if (!r.ok) throw new Error(`/cloud/credits → ${r.status}`);
    return r.json();
  },

  async cloudTopup(
    amountUsd: number,
  ): Promise<{ ok: boolean; checkout_url?: string; opened?: boolean; reason?: string }> {
    const r = await fetch(`${getApiBase()}/cloud/topup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount_usd: amountUsd }),
    });
    if (!r.ok) throw new Error(`/cloud/topup → ${r.status}`);
    return r.json();
  },
};

// EventSource only does GET — POST + SSE requires fetch + manual frame parsing.
function streamSSE(
  path: string,
  body: unknown,
  { onEvent, onDone, onError }: StreamHandlers,
): StreamHandle {
  const controller = new AbortController();
  const handle: StreamHandle = { close: () => controller.abort() };

  (async () => {
    try {
      const r = await fetch(`${getApiBase()}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getLlmHeaders() },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!r.ok) {
        const detail = await r.text().catch(() => "");
        throw new Error(`${path} → ${r.status}${detail ? ` ${detail}` : ""}`);
      }
      if (!r.body) throw new Error("no response body");

      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by blank lines.
        let nl: number;
        while ((nl = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, nl);
          buffer = buffer.slice(nl + 2);
          const event = parseSSEFrame(frame);
          if (event) onEvent(event);
        }
      }
      onDone?.();
    } catch (err) {
      if ((err as { name?: string })?.name === "AbortError") return;
      onError?.(err);
    }
  })();

  return handle;
}

function parseSSEFrame(frame: string): ChatEvent | null {
  let kind = "";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) kind = line.slice(6).trim();
    else if (line.startsWith("data:")) data = line.slice(5).trim();
  }
  if (!kind) return null;
  try {
    const parsed = data ? JSON.parse(data) : {};
    return { kind, ...parsed };
  } catch {
    return null;
  }
}
