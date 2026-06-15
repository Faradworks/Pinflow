// The two-card provider picker + per-mode inputs. Shared by the first-run
// OnboardingScreen and the SettingsModal so the two stay in lockstep.

import type { ReactNode } from "react";

import type { AgentModel, LlmMode, PinflowConfig } from "../../lib/config";
import { CloudSignIn } from "./CloudSignIn";
import { KeyField } from "./KeyField";

export interface ProviderDraft {
  mode: LlmMode | null;
  anthropicKey: string;
  model: AgentModel;
}

export function configToDraft(cfg: PinflowConfig | null): ProviderDraft {
  return {
    mode: cfg?.mode ?? null,
    anthropicKey: cfg?.anthropicKey ?? "",
    model: cfg?.model ?? "opus",
  };
}

/** Validate a draft into a persistable config, or null if incomplete/invalid.
 *  `cloud` is valid once the user has signed in (the token is held by the local
 *  service, so the config itself just records the mode). */
export function draftToConfig(
  d: ProviderDraft,
  cloudSignedIn = false,
  keyInvalid = false,
): PinflowConfig | null {
  if (d.mode === "cloud") {
    return cloudSignedIn ? { mode: "cloud", model: d.model } : null;
  }
  if (d.mode === "self") {
    const key = d.anthropicKey.trim();
    return key && !keyInvalid ? { mode: "self", anthropicKey: key, model: d.model } : null;
  }
  return null;
}

const CARDS: {
  mode: LlmMode;
  icon: ReactNode;
  title: string;
  blurb: string;
  tag?: string;
}[] = [
  {
    mode: "cloud",
    icon: <CloudIcon />,
    title: "Pinflow Cloud",
    blurb: "Free starter credits, then top up as you go. No API key to manage.",
  },
  {
    mode: "self",
    icon: <KeyIcon />,
    title: "Bring your own key",
    blurb: "Runs with your Anthropic key — Anthropic bills you for usage. No account.",
  },
];

export function ProviderForm({
  value,
  onChange,
  onCloudSignedInChange,
  onKeyInvalidChange,
}: {
  value: ProviderDraft;
  onChange: (d: ProviderDraft) => void;
  onCloudSignedInChange: (signedIn: boolean) => void;
  onKeyInvalidChange: (invalid: boolean) => void;
}) {
  const set = (patch: Partial<ProviderDraft>) => onChange({ ...value, ...patch });

  return (
    <div>
      <div style={{ display: "flex", gap: 10 }}>
        {CARDS.map((c) => (
          <CardButton
            key={c.mode}
            selected={value.mode === c.mode}
            onClick={() => set({ mode: c.mode })}
            icon={c.icon}
            title={c.title}
            blurb={c.blurb}
            tag={c.tag}
          />
        ))}
      </div>

      {value.mode === "self" && (
        <KeyField
          value={value.anthropicKey}
          onChange={(k) => set({ anthropicKey: k })}
          onInvalidChange={onKeyInvalidChange}
        />
      )}

      {value.mode === "cloud" && (
        <CloudSignIn onSignedInChange={onCloudSignedInChange} />
      )}

      <ModelSelect value={value.model} onChange={(m) => set({ model: m })} />
    </div>
  );
}

const MODELS: { id: AgentModel; title: string; blurb: string }[] = [
  { id: "opus", title: "Opus", blurb: "Strongest — best for complex schematics" },
  { id: "sonnet", title: "Sonnet", blurb: "Faster & cheaper — fewer tokens/credits" },
];

function ModelSelect({
  value,
  onChange,
}: {
  value: AgentModel;
  onChange: (m: AgentModel) => void;
}) {
  return (
    <div style={{ marginTop: 16 }}>
      <div
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: "var(--ink-2)",
          marginBottom: 8,
          display: "flex",
          gap: 6,
          alignItems: "baseline",
        }}
      >
        Model
        <span style={{ fontSize: 11, fontWeight: 400, color: "var(--muted)" }}>
          · used for the chat agent
        </span>
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        {MODELS.map((m) => {
          const selected = value === m.id;
          return (
            <button
              key={m.id}
              type="button"
              onClick={() => onChange(m.id)}
              style={{
                flex: 1,
                textAlign: "left",
                padding: "10px 12px",
                borderRadius: 9,
                cursor: "pointer",
                background: selected ? "var(--accent-soft)" : "var(--panel-2)",
                border: `1px solid ${selected ? "var(--accent)" : "var(--line)"}`,
                color: "var(--ink)",
                transition: "border-color 120ms, background 120ms",
              }}
            >
              <div style={{ fontSize: 12.5, fontWeight: 600, display: "flex", gap: 6 }}>
                {m.title}
                {m.id === "opus" && (
                  <span style={{ fontSize: 10, fontWeight: 500, color: "var(--muted)" }}>
                    default
                  </span>
                )}
              </div>
              <div style={{ fontSize: 11, lineHeight: 1.35, color: "var(--muted)", marginTop: 2 }}>
                {m.blurb}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function CardButton({
  selected,
  onClick,
  icon,
  title,
  blurb,
  tag,
}: {
  selected: boolean;
  onClick: () => void;
  icon: ReactNode;
  title: string;
  blurb: string;
  tag?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        position: "relative",
        flex: 1,
        textAlign: "left",
        padding: "14px 14px 16px",
        borderRadius: 10,
        cursor: "pointer",
        background: selected ? "var(--accent-soft)" : "var(--panel-2)",
        border: `1px solid ${selected ? "var(--accent)" : "var(--line)"}`,
        color: "var(--ink)",
        transition: "border-color 120ms, background 120ms",
      }}
    >
      {tag && (
        <span
          style={{
            position: "absolute",
            top: 10,
            right: 10,
            fontSize: 10,
            fontWeight: 600,
            letterSpacing: "0.02em",
            textTransform: "uppercase",
            color: "var(--pending)",
            background: "var(--pending-soft)",
            border: "1px solid var(--pending)",
            borderRadius: 5,
            padding: "1px 5px",
          }}
        >
          {tag}
        </span>
      )}
      <div
        style={{
          width: 30,
          height: 30,
          borderRadius: 8,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "var(--bg-2)",
          color: selected ? "var(--accent)" : "var(--ink-2)",
          marginBottom: 10,
        }}
      >
        {icon}
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>{title}</div>
      <div style={{ fontSize: 12, lineHeight: 1.4, color: "var(--muted)" }}>
        {blurb}
      </div>
    </button>
  );
}

function CloudIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 18 18" fill="none">
      <path
        d="M5 13.5h7.2a3 3 0 0 0 .4-5.97 4 4 0 0 0-7.74-1.06A3.25 3.25 0 0 0 5 13.5z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function KeyIcon() {
  return (
    <svg width="17" height="17" viewBox="0 0 18 18" fill="none">
      <circle cx="6" cy="6" r="3" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M8.1 8.1 13 13M11 11l1.4-1.4M12.4 12.4 14 11"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
