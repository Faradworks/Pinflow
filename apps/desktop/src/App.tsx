import { useEffect, useRef, useState } from "react";

import { ChatPanel } from "./components/chat/ChatPanel";
import type { StagedAttachment } from "./components/chat/ChatInput";
import type { Message } from "./components/chat/types";
import { SchematicView } from "./components/schematic/SchematicView";
import { TitleBar } from "./components/window-shell/TitleBar";
import type { KicadProject } from "./components/window-shell/KicadStatusChip";
import { api, type ChatEvent, type CostInfo, type StreamHandle } from "./lib/api";
import { readTheme, writeTheme, type Theme } from "./lib/theme";
import { getConfig, clearConfig, type PinflowConfig } from "./lib/config";
import { OnboardingScreen } from "./components/onboarding/OnboardingScreen";
import { SettingsModal } from "./components/onboarding/SettingsModal";

let _id = 0;
const uid = () => "m" + ++_id;
let _attKey = 0;
const attachmentKey = () => "att-local-" + ++_attKey;

// Tool calls that change the schematic on disk or in the staging working
// copy. Seeing one in the SSE stream is our cue to re-fetch the project so
// the schematic viewer picks up the latest content.
const STAGING_TOOLS = new Set([
  "add_subcircuit_from_datasheet",
  "add_subcircuit_from_netlist",
  "remove_components",
  "edit_property",
  "resolve_mpn",
  "commit_edit",
  "discard_edit",
]);

// Shown until the local backend answers /health — the bundled sidecar takes
// ~15s to cold-boot in packaged builds, so we wait instead of letting the first
// fetches fail with "Load failed".
function StartupSplash({
  state,
  onRetry,
}: {
  state: "probing" | "down";
  onRetry: () => void;
}) {
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "grid",
        placeItems: "center",
        background: "var(--panel)",
        color: "var(--ink)",
        font: "13px ui-monospace, monospace",
      }}
    >
      {state === "down" ? (
        <div style={{ textAlign: "center" }}>
          <div style={{ marginBottom: 12, opacity: 0.8 }}>
            Couldn’t reach the Pinflow backend.
          </div>
          <button
            onClick={onRetry}
            style={{
              padding: "6px 14px",
              border: "1px solid var(--line)",
              borderRadius: 6,
              background: "var(--bg)",
              color: "var(--ink)",
              cursor: "pointer",
            }}
          >
            Retry
          </button>
        </div>
      ) : (
        <div style={{ opacity: 0.6 }}>Starting Pinflow…</div>
      )}
    </div>
  );
}

function App() {
  const [theme, setTheme] = useState<Theme>(() => readTheme());
  const [config, setConfig] = useState<PinflowConfig | null>(() => getConfig());
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Bumped when a turn ends so the title-bar credits chip re-fetches the balance.
  const [creditsRefresh, setCreditsRefresh] = useState(0);
  // Live per-request LLM cost, driven by `cost` SSE events. The title-bar chip
  // shows the authoritative post-turn balance; this is the running in-flight
  // spend for the current message (and persists between turns as the session total).
  const [cost, setCost] = useState<CostInfo | null>(null);
  const [project, setProject] = useState<KicadProject | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<StagedAttachment[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  // True between a Stop click and the backend's wind-down `done`. The agent's
  // current step (LLM turn / tool) finishes, then it stops; we block new sends
  // until `done` so a fresh /chat can't race the still-draining drive.
  const [isStopping, setIsStopping] = useState(false);
  // Hard-abort watchdog: if the backend doesn't wind down (stuck in a long
  // tool/LLM call) we abort the fetch as a fallback. Cleared on done/error.
  const stopTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // True after a `suspended` event arrives — the agent is waiting on the
  // user's answer (either a QuestionsCard option click OR freeform text
  // through the chat input). Drives `onSend` to call `/resume` instead of
  // `/chat`, so the pending `ask_user` tool_use_id gets its `tool_result`.
  const [isAwaitingAnswer, setIsAwaitingAnswer] = useState(false);
  // Tracks the in-flight SSE stream so a new-session reset (or page unmount)
  // can abort it cleanly instead of letting it keep appending to a discarded
  // message log.
  const streamRef = useRef<StreamHandle | null>(null);
  // Gate the UI until the local backend answers. Packaged builds spawn the
  // FastAPI sidecar on launch and it takes ~15s to cold-boot; without this the
  // first fetches surface as WebKit "Load failed". probing → up → down.
  const [backend, setBackend] = useState<"probing" | "up" | "down">("probing");
  const [probeNonce, setProbeNonce] = useState(0);

  function onAttach(files: File[]) {
    setAttachments((prev) => [
      ...prev,
      ...files.map((file) => ({ key: attachmentKey(), file })),
    ]);
  }

  function onRemoveAttachment(key: string) {
    setAttachments((prev) => prev.filter((a) => a.key !== key));
  }

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    writeTheme(theme);
  }, [theme]);

  // Poll the local service's /health until it answers, then release the UI.
  // Retried on an interval because the bundled sidecar isn't up for the first
  // several seconds after launch (see `backend` state above).
  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const deadline = Date.now() + 60_000;
    async function probe() {
      if (cancelled) return;
      if (await api.health()) {
        if (!cancelled) setBackend("up");
      } else if (Date.now() > deadline) {
        if (!cancelled) setBackend("down");
      } else {
        timer = window.setTimeout(probe, 800);
      }
    }
    probe();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [probeNonce]);

  // Once a stream ends, mark the turn's live tool cards as done so they
  // collapse to a disclosure line. Old already-collapsed cards stay put;
  // future streams won't reanimate them.
  function collapseLiveTools() {
    setMessages((m) =>
      m.map((x) => (x.kind === "tool" && x.live ? { ...x, live: false } : x)),
    );
  }

  function applyEvent(e: ChatEvent) {
    if (e.kind === "meta") {
      if (typeof e.conversation_id === "string") setConversationId(e.conversation_id);
      return;
    }
    if (e.kind === "cost") {
      setCost({
        requestCredits: e.request_credits ?? 0,
        conversationCredits: e.conversation_credits ?? 0,
        estimated: !!e.estimated,
        balance: typeof e.balance === "number" ? e.balance : null,
        provider: typeof e.provider === "string" ? e.provider : "self",
        requestTokens: e.request_tokens ?? 0,
        requestInputTokens: e.request_input_tokens ?? 0,
        requestOutputTokens: e.request_output_tokens ?? 0,
        conversationTokens: e.conversation_tokens ?? 0,
      });
      return;
    }
    if (e.kind === "done") {
      setIsStreaming(false);
      setIsStopping(false);
      clearStopWatch();
      // Turn completed without suspending → server has no pending question.
      setIsAwaitingAnswer(false);
      streamRef.current = null;
      collapseLiveTools();
      // A cloud turn may have spent credits — refresh the balance chip.
      setCreditsRefresh((n) => n + 1);
      return;
    }
    // Tools that mutate disk or the staging working copy — refresh project
    // state so SchematicView picks up the new source (real or staged).
    if (e.kind === "tool" && STAGING_TOOLS.has(String(e.tool))) {
      api
        .detectActiveProject()
        .then((p) => setProject(p.detected ? p : null))
        .catch(() => {});
    }
    if (e.kind === "suspended") {
      // Lock the most recent ai message (its QuestionsCard is now awaiting
      // an answer; clicking an option fires chatResume).
      setMessages((m) => {
        for (let i = m.length - 1; i >= 0; i--) {
          if (m[i].kind === "ai") {
            const copy = m.slice();
            copy[i] = { ...m[i], locked: true } as Message;
            return copy;
          }
        }
        return m;
      });
      // Suspended = agent is waiting on the user, no longer actively working.
      setIsStreaming(false);
      setIsStopping(false);
      clearStopWatch();
      setIsAwaitingAnswer(true);
      streamRef.current = null;
      collapseLiveTools();
      return;
    }
    const msg = eventToMessage(e);
    if (!msg) return;
    setMessages((m) => [...m, msg]);
  }

  function onStreamError(err: unknown) {
    const text = (err as Error)?.message ?? String(err);
    setMessages((m) => [...m, { id: uid(), kind: "system", text: `error: ${text}` }]);
    setIsStreaming(false);
    setIsStopping(false);
    clearStopWatch();
    streamRef.current = null;
    collapseLiveTools();
  }

  function clearStopWatch() {
    if (stopTimerRef.current) {
      clearTimeout(stopTimerRef.current);
      stopTimerRef.current = null;
    }
  }

  // Stop an in-flight drive. Tell the backend to wind down cooperatively (its
  // current step finishes, then it emits `system` + `done` over the still-open
  // stream, which resets state through the `done` handler). Keep the stream
  // open so that graceful end arrives; a watchdog hard-aborts the fetch if the
  // backend is wedged in a long tool/LLM call and never reaches a checkpoint.
  function onStop() {
    if (!isStreaming || isStopping) return;
    const convId = conversationId;
    if (!convId) return;
    setIsStopping(true);
    api.cancelChat(convId).catch(() => {});
    clearStopWatch();
    stopTimerRef.current = setTimeout(() => {
      streamRef.current?.close();
      streamRef.current = null;
      setIsStreaming(false);
      setIsStopping(false);
      stopTimerRef.current = null;
      setMessages((m) => [...m, { id: uid(), kind: "system", text: "Stopped." }]);
      collapseLiveTools();
    }, 8000);
  }

  function onNewSession() {
    streamRef.current?.close();
    streamRef.current = null;
    clearStopWatch();
    setMessages([]);
    setDraft("");
    setAttachments([]);
    setConversationId(null);
    setIsStreaming(false);
    setIsStopping(false);
    setIsAwaitingAnswer(false);
    setCost(null);
  }

  async function onSend() {
    const text = draft.trim();
    const pending = attachments;
    if (!text && pending.length === 0) return;
    if (isStreaming || isStopping) return;

    // Snapshot then clear input + staged attachments so the user can't
    // double-fire while the upload is in flight.
    setDraft("");
    setAttachments([]);
    setIsStreaming(true);

    const bubble: Message = {
      id: uid(),
      kind: "user",
      text,
      attachments: pending.map((a) => ({
        filename: a.file.name,
        size: a.file.size,
        mime: a.file.type || "application/octet-stream",
      })),
    };
    setMessages((m) => [...m, bubble]);

    let convId = conversationId;
    let attachmentIds: string[] = [];
    if (pending.length > 0) {
      try {
        const result = await api.uploadAttachments(
          convId,
          pending.map((a) => a.file),
        );
        convId = result.conversation_id;
        if (convId !== conversationId) setConversationId(convId);
        attachmentIds = result.attachments.map((a) => a.attachment_id);
      } catch (err) {
        onStreamError(err);
        return;
      }
    }

    // Route through /resume when the agent is suspended waiting on an
    // ask_user answer. Otherwise the pending tool_use_id never gets a
    // tool_result and Anthropic rejects the next request. Don't clear
    // isAwaitingAnswer optimistically — the `done`/`suspended` events do it,
    // so a failed/interrupted resume (e.g. WebKit "Load failed") leaves the
    // flag true and the user can retry instead of getting routed to /chat
    // (which would 400 against the still-pending question).
    if (isAwaitingAnswer && convId) {
      streamRef.current = api.chatResume(
        convId,
        text,
        { onEvent: applyEvent, onError: onStreamError },
        attachmentIds,
      );
      return;
    }

    // Fresh user message → the backend resets its per-request meter; mirror that
    // so the line shows this request from 0 (keeping the session total). Resume
    // paths (above, and onAnswer) deliberately don't reset — the request continues.
    setCost((c) =>
      c ? { ...c, requestCredits: 0, estimated: false, requestTokens: 0, requestInputTokens: 0, requestOutputTokens: 0 } : c,
    );
    streamRef.current = api.chatStream(
      text,
      convId,
      { onEvent: applyEvent, onError: onStreamError },
      attachmentIds,
    );
  }

  async function onAnswer(msgId: string, qid: string, option: string) {
    if (!conversationId) return;
    if (isStreaming || isStopping) return;
    setIsStreaming(true);
    // isAwaitingAnswer is cleared by the resume stream's done/suspended event,
    // not optimistically — so a failed resume leaves it true for retry.
    setMessages((m) =>
      m.map((x) => {
        if (x.id !== msgId || x.kind !== "ai" || !x.questions) return x;
        return {
          ...x,
          questions: x.questions.map((q) =>
            q.id === qid ? { ...q, answer: option } : q,
          ),
        };
      }),
    );

    const pending = attachments;
    let attachmentIds: string[] = [];
    if (pending.length > 0) {
      setAttachments([]);
      // Render the attached files as a user bubble alongside the answer so
      // the conversation log shows what was attached.
      setMessages((m) => [
        ...m,
        {
          id: uid(),
          kind: "user",
          text: "",
          attachments: pending.map((a) => ({
            filename: a.file.name,
            size: a.file.size,
            mime: a.file.type || "application/octet-stream",
          })),
        },
      ]);
      try {
        const result = await api.uploadAttachments(
          conversationId,
          pending.map((a) => a.file),
        );
        attachmentIds = result.attachments.map((a) => a.attachment_id);
      } catch (err) {
        onStreamError(err);
        return;
      }
    }

    streamRef.current = api.chatResume(
      conversationId,
      option,
      { onEvent: applyEvent, onError: onStreamError },
      attachmentIds,
    );
  }

  // Sign in for parts mid-session WITHOUT switching the LLM to cloud — the server
  // holds the session token and uses it only for the gateway parts proxy; the BYOK
  // key still drives the LLM. Resolves true once signed in (so the card can react).
  async function signInForParts(): Promise<boolean> {
    let loginState: string;
    try {
      const started = await api.startCloudLogin();
      loginState = started.state;
    } catch {
      return false;
    }
    const deadline = Date.now() + 120_000;
    return new Promise<boolean>((resolve) => {
      const poll = window.setInterval(async () => {
        if (Date.now() > deadline) {
          window.clearInterval(poll);
          resolve(false);
          return;
        }
        try {
          const s = await api.cloudAuthStatus(loginState);
          if (s.signed_in) {
            window.clearInterval(poll);
            resolve(true);
          }
        } catch {
          /* keep polling */
        }
      }, 1500);
    });
  }

  if (backend !== "up") {
    return (
      <StartupSplash
        state={backend}
        onRetry={() => {
          setBackend("probing");
          setProbeNonce((n) => n + 1);
        }}
      />
    );
  }

  if (!config) {
    return <OnboardingScreen onComplete={(cfg) => setConfig(cfg)} />;
  }

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        background: "var(--panel)",
        overflow: "hidden",
        boxShadow: "inset 0 0 0 1px var(--line)",
      }}
    >
      <TitleBar
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        project={project}
        onProjectChange={setProject}
        onOpenSettings={() => setSettingsOpen(true)}
        showCredits={config?.mode === "cloud"}
        creditsRefresh={creditsRefresh}
      />

      <div style={{ flex: 1, display: "flex", minHeight: 0, background: "var(--bg)" }}>
        <div
          style={{
            width: "46%",
            minWidth: 380,
            borderRight: "1px solid var(--line)",
          }}
        >
          <ChatPanel
            messages={messages}
            draft={draft}
            onDraftChange={setDraft}
            onSend={onSend}
            onAnswer={onAnswer}
            onSignInForParts={signInForParts}
            project={project}
            attachments={attachments}
            onAttach={onAttach}
            onRemoveAttachment={onRemoveAttachment}
            isStreaming={isStreaming}
            isStopping={isStopping}
            onStop={onStop}
            onNewSession={onNewSession}
            cost={cost}
            cloudMode={config?.mode === "cloud"}
          />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <SchematicView
            project={project}
            source={project?.schematic_source ?? null}
          />
        </div>
      </div>

      {settingsOpen && (
        <SettingsModal
          onClose={() => setSettingsOpen(false)}
          onSaved={(cfg) => {
            setConfig(cfg);
            setSettingsOpen(false);
          }}
          onReset={() => {
            clearConfig();
            setConfig(null);
            setSettingsOpen(false);
          }}
        />
      )}
    </div>
  );
}

// Translate a backend ChatEvent into a chat Message. Returns null for
// control events (meta/done/suspended) handled separately in applyEvent.
function eventToMessage(e: ChatEvent): Message | null {
  if (e.kind === "ai") {
    return {
      id: e.id,
      kind: "ai",
      text: e.text ?? "",
      questions: e.questions,
      diff: e.diff,
      confirm: e.confirm,
      locked: e.locked,
      cost: e.cost ?? null,
    };
  }
  if (e.kind === "tool") {
    return {
      id: e.id,
      kind: "tool",
      tool: e.tool,
      title: e.title,
      meta: e.meta ?? [],
      live: true,
    };
  }
  if (e.kind === "thinking") {
    return { id: e.id, kind: "thinking", text: e.text ?? "", streaming: !!e.streaming };
  }
  if (e.kind === "action") {
    return { id: e.id, kind: "action", actKind: e.actKind, text: e.text ?? "" };
  }
  if (e.kind === "system") {
    return { id: e.id, kind: "system", text: e.text ?? "" };
  }
  if (e.kind === "block_diagram") {
    return {
      id: e.id,
      kind: "block_diagram",
      nodes: e.nodes ?? [],
      edges: e.edges ?? [],
    };
  }
  if (e.kind === "design_spec") {
    return { id: e.id, kind: "design_spec", spec: e.spec ?? {} };
  }
  if (e.kind === "signin_required") {
    return { id: e.id, kind: "signin_required", hint: e.hint ?? "" };
  }
  return null;
}

export default App;
