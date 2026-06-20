// The two-card provider picker + per-mode inputs. Shared by the first-run
// OnboardingScreen and the SettingsModal so the two stay in lockstep.

import { useEffect, useState, type ReactNode } from "react";

import { api } from "../../lib/api";
import {
  isServerLlmDisabled,
  setServerLlmDisabled,
  type AgentModel,
  type LlmMode,
  type PinflowConfig,
} from "../../lib/config";
import { CloudSignIn } from "./CloudSignIn";
import { KeyField } from "./KeyField";

/** Tracks BYOK-required mode (server-side key disabled). The flag is
 *  gateway-served via /cloud/credits and only fully known once signed in, so
 *  this re-fetches whenever `signedIn` flips. Mirrors the value into config so
 *  getLlmHeaders() routes the next chat send correctly. */
export function useServerLlmDisabled(signedIn: boolean): boolean {
  const [disabled, setDisabled] = useState<boolean>(isServerLlmDisabled());
  useEffect(() => {
    let alive = true;
    api
      .cloudCredits()
      .then((d) => {
        if (!alive) return;
        const v = !!d.byok_required;
        setServerLlmDisabled(v);
        setDisabled(v);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [signedIn]);
  return disabled;
}

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
  serverLlmDisabled = false,
): PinflowConfig | null {
  if (d.mode === "cloud") {
    if (!cloudSignedIn) return null;
    // BYOK-required mode: cloud sign-in is for parts/credits, but the agent runs
    // on the user's own key — so a valid key is also required to finish.
    if (serverLlmDisabled) {
      const key = d.anthropicKey.trim();
      return key && !keyInvalid ? { mode: "cloud", anthropicKey: key, model: d.model } : null;
    }
    return { mode: "cloud", model: d.model };
  }
  if (d.mode === "self") {
    const key = d.anthropicKey.trim();
    return key && !keyInvalid ? { mode: "self", anthropicKey: key, model: d.model } : null;
  }
  return null;
}

/** Human-readable reason the current draft can't be saved yet, or null when it's
 *  valid. Mirrors `draftToConfig`'s gates so the UI can explain a disabled
 *  Save/Continue button instead of leaving the user guessing. */
export function draftBlockerReason(
  d: ProviderDraft,
  cloudSignedIn = false,
  keyInvalid = false,
  serverLlmDisabled = false,
): string | null {
  if (!d.mode) return "Choose how to run Pinflow to continue.";
  const key = d.anthropicKey.trim();
  if (d.mode === "cloud") {
    if (!cloudSignedIn) return "Sign in to Pinflow Cloud to continue.";
    if (serverLlmDisabled) {
      if (!key) return "Pinflow Cloud is currently unavailable for the agent — add your Anthropic key to continue.";
      if (keyInvalid) return "That Anthropic key isn't valid yet — check it and try again.";
    }
    return null;
  }
  // self
  if (!key) return "Add your Anthropic key to continue.";
  if (keyInvalid) return "That Anthropic key isn't valid yet — check it and try again.";
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
    blurb:
      "Free starter credits, plus part search across millions of orderable " +
      "JLCPCB/LCSC components. No API key to manage.",
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
  serverLlmDisabled = false,
}: {
  value: ProviderDraft;
  onChange: (d: ProviderDraft) => void;
  onCloudSignedInChange: (signedIn: boolean) => void;
  onKeyInvalidChange: (invalid: boolean) => void;
  serverLlmDisabled?: boolean;
}) {
  const set = (patch: Partial<ProviderDraft>) => onChange({ ...value, ...patch });

  // When cloud LLM is unavailable, the card's "no API key to manage" promise no
  // longer holds — sign-in is for parts/credits, the agent runs on the user's key.
  const cards = CARDS.map((c) =>
    c.mode === "cloud" && serverLlmDisabled
      ? {
          ...c,
          tag: "key needed",
          blurb:
            "Currently unavailable for running the agent. Sign in for part " +
            "search — you'll run the agent on your own Anthropic key.",
        }
      : c,
  );

  return (
    <div>
      <div style={{ display: "flex", gap: 10 }}>
        {cards.map((c) => (
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
        <>
          {serverLlmDisabled && <ByokCallout />}
          <CloudSignIn onSignedInChange={onCloudSignedInChange} />
          {serverLlmDisabled && (
            <KeyField
              value={value.anthropicKey}
              onChange={(k) => set({ anthropicKey: k })}
              onInvalidChange={onKeyInvalidChange}
            />
          )}
        </>
      )}

      <ModelSelect value={value.model} onChange={(m) => set({ model: m })} />
    </div>
  );
}

/** Explains, in friendly/generic terms, why a key is needed even though Pinflow
 *  Cloud is selected — shown above sign-in so the requirement is clear before the
 *  user hunts for a Save. Deliberately doesn't expose the operational flag. */
function ByokCallout() {
  return (
    <div
      style={{
        marginTop: 14,
        padding: "10px 12px",
        fontSize: 12,
        lineHeight: 1.5,
        color: "var(--ink-2)",
        background: "var(--pending-soft)",
        border: "1px solid var(--pending)",
        borderRadius: 8,
      }}
    >
      <b style={{ color: "var(--ink)" }}>Pinflow Cloud is currently unavailable
      for running the agent.</b> You can still sign in to use part search — to keep
      chatting, just add your own Anthropic key below.
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
