import { useEffect, useRef } from "react";
import type { CSSProperties } from "react";

import type { CostInfo } from "../../lib/api";
import { useEasedNumber } from "../../lib/useEasedNumber";
import type { KicadProject } from "../window-shell/KicadStatusChip";
import { ChatInput, type StagedAttachment } from "./ChatInput";
import { EmptyState } from "./EmptyState";
import { LoadingIndicator } from "./LoadingIndicator";
import { MessageView } from "./MessageView";
import { Suggestion } from "./Suggestion";
import type { Message } from "./types";

type Props = {
  messages: Message[];
  draft: string;
  onDraftChange: (v: string) => void;
  onSend: () => void;
  onAnswer?: (msgId: string, qid: string, option: string) => void;
  onSignInForParts?: () => Promise<boolean>;
  project: KicadProject | null;
  attachments: StagedAttachment[];
  onAttach: (files: File[]) => void;
  onRemoveAttachment: (key: string) => void;
  isStreaming: boolean;
  isStopping?: boolean;
  onStop?: () => void;
  onNewSession: () => void;
  cost?: CostInfo | null;
  cloudMode?: boolean;
};

// For an ask_user tool message at index `i`, return whether the linked
// question (the next ai message's QuestionsCard) has been answered. The
// agent loop always emits the ai-with-questions right after the tool, so
// peeking at i+1 is sufficient. Returns undefined for non-ask_user tools
// and non-tool messages — ToolCard ignores it for those.
function askUserAnswered(messages: Message[], i: number): boolean | undefined {
  const m = messages[i];
  if (m.kind !== "tool" || m.tool !== "ask_user") return undefined;
  const next = messages[i + 1];
  if (!next || next.kind !== "ai" || !next.questions || next.questions.length === 0) {
    return false;
  }
  return next.questions.every((q) => q.answer !== undefined);
}

const SUGGESTIONS = [
  "Add a USB-C power input + 3.3V LDO",
  "Add I²C OLED display",
  "Add SD card slot (SPI)",
  "Add JTAG/SWD header",
];

// Live running-spend line above the composer. Cloud accounts read authoritative
// credits from the gateway (the remaining balance lives in the title-bar chip;
// this is the in-flight spend for the current request, incl. tool calls). BYO-key
// accounts have no credits — they pay Anthropic directly — so they get the exact
// token count only (no client-side $ estimate; see CostMeterLine).
function fmtTokens(n: number): string {
  const v = Math.round(n);
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

function CostMeterLine({ cost, cloudMode }: { cost: CostInfo; cloudMode: boolean }) {
  const showCredits = cloudMode && !cost.estimated;
  // Cosmetic easing: the meter only gets a new value per completed call (the
  // gateway is non-streaming), so ease the readout up to each new number instead
  // of snapping. Hooks must run unconditionally → they precede the early return.
  const tokens = useEasedNumber(cost.requestTokens);
  const credits = useEasedNumber(cost.requestCredits);
  const sessionCredits = useEasedNumber(cost.conversationCredits);
  if (cost.requestTokens <= 0 && cost.requestCredits <= 0) return null;
  // Cloud → authoritative credits + the exact token count. BYOK → token count
  // ONLY: no per-request $ figure. A client-side USD estimate would need a
  // hand-kept price table and structurally undercounts (it misses tokens a tool
  // spends internally, e.g. the datasheet read), so a quietly-low number is worse
  // than none — the exact token count is the honest figure BYOK users get.
  const credPrefix = showCredits ? `${credits.toFixed(2)} cr · ` : "";
  const session =
    showCredits && cost.conversationCredits > cost.requestCredits + 1e-9
      ? ` · ${sessionCredits.toFixed(2)} cr session`
      : "";
  return (
    <div
      style={meterStyle}
      title={`${cost.requestInputTokens.toLocaleString()} in + ${cost.requestOutputTokens.toLocaleString()} out${showCredits ? " · from Pinflow Cloud" : ""}`}
    >
      <span style={{ color: "var(--accent)" }}>⚡</span>
      <span>{credPrefix}{fmtTokens(tokens)} tok this request</span>
      {session && <span style={{ color: "var(--muted)" }}>{session}</span>}
    </div>
  );
}

const meterStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  marginBottom: 8,
  padding: "0 2px",
  fontSize: 11,
  fontFamily: "var(--font-mono)",
  color: "var(--muted)",
};

export function ChatPanel({
  messages,
  draft,
  onDraftChange,
  onSend,
  onAnswer,
  onSignInForParts,
  project,
  attachments,
  onAttach,
  onRemoveAttachment,
  isStreaming,
  isStopping,
  onStop,
  onNewSession,
  cost,
  cloudMode,
}: Props) {
  const logRef = useRef<HTMLDivElement>(null);
  const started = messages.length > 0;

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages.length, isStreaming]);

  const hint = project
    ? `paired · ${project.schematic || project.name}`
    : "no KiCad project · open one to pair";

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "var(--panel)",
        minWidth: 0,
      }}
    >
      <div
        style={{
          padding: "12px 18px 10px",
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderBottom: "1px solid var(--line)",
          flexShrink: 0,
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)" }}>New change</span>
        <span style={{ color: "var(--muted-2)" }}>·</span>
        <span
          style={{
            fontSize: 12,
            color: "var(--muted)",
            fontFamily: "var(--font-mono)",
          }}
        >
          pinflow · ui preview
        </span>
        <div style={{ flex: 1 }} />
        {started && (
          <button
            type="button"
            onClick={onNewSession}
            title="Start a new session"
            aria-label="Start a new session"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              height: 24,
              padding: "0 8px",
              background: "transparent",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink-2)",
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              cursor: "pointer",
            }}
          >
            <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <path
                d="M6 2v8M2 6h8"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
              />
            </svg>
            <span>new session</span>
          </button>
        )}
      </div>

      <div
        ref={logRef}
        className="pf-chat-log"
        style={{
          flex: 1,
          overflow: "auto",
          padding: "20px 22px 16px",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        {!started && <EmptyState schematicName={project?.schematic ?? null} />}
        {messages.map((m, i) => (
          <MessageView
            key={m.id}
            message={m}
            onAnswer={onAnswer}
            onSignInForParts={onSignInForParts}
            answered={askUserAnswered(messages, i)}
          />
        ))}
        {isStreaming && <LoadingIndicator />}
      </div>

      <div style={{ padding: "12px 18px 16px", flexShrink: 0 }}>
        {cost && <CostMeterLine cost={cost} cloudMode={!!cloudMode} />}
        <ChatInput
          value={draft}
          onChange={onDraftChange}
          onSend={onSend}
          disabled={isStreaming}
          isStreaming={isStreaming}
          isStopping={isStopping}
          onStop={onStop}
          hint={hint}
          attachments={attachments}
          onAttach={onAttach}
          onRemoveAttachment={onRemoveAttachment}
        />
        {!started && (
          <div style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
            {SUGGESTIONS.map((text) => (
              <Suggestion key={text} text={text} onClick={() => onDraftChange(text)} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
