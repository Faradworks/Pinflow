// Full-window first-run chooser. Shown by App when no config is stored.
// Picking "Bring your own key" or "Custom backend" persists the config and
// drops the user into the app; "Pinflow Cloud" is shown but gated (Soon).

import { useState } from "react";
import type { CSSProperties } from "react";

import { getConfig, saveConfig, type PinflowConfig } from "../../lib/config";
import { PinflowLogo } from "../window-shell/PinflowLogo";
import {
  ProviderForm,
  configToDraft,
  draftToConfig,
  draftBlockerReason,
  useServerLlmDisabled,
  type ProviderDraft,
} from "./ProviderForm";

export function OnboardingScreen({
  onComplete,
}: {
  onComplete: (cfg: PinflowConfig) => void;
}) {
  const [draft, setDraft] = useState<ProviderDraft>(() => configToDraft(getConfig()));
  const [cloudSignedIn, setCloudSignedIn] = useState(false);
  const [keyInvalid, setKeyInvalid] = useState(false);
  const serverLlmDisabled = useServerLlmDisabled(cloudSignedIn);
  const cfg = draftToConfig(draft, cloudSignedIn, keyInvalid, serverLlmDisabled);
  const blocker = draftBlockerReason(draft, cloudSignedIn, keyInvalid, serverLlmDisabled);

  function commit() {
    if (!cfg) return;
    saveConfig(cfg);
    onComplete(cfg);
  }

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        zIndex: 50,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "var(--bg)",
        padding: 24,
        overflow: "auto",
      }}
    >
      {/* Draggable strip so the frameless window can be moved during first run
          (there's no TitleBar here). Traffic lights stay on top + clickable. */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 36,
          // @ts-expect-error — Tauri drag region; not in React's CSSProperties.
          WebkitAppRegion: "drag",
        }}
      />
      <div
        style={{
          width: "min(720px, 100%)",
          background: "var(--panel)",
          border: "1px solid var(--line)",
          borderRadius: 14,
          padding: "26px 28px 22px",
          boxShadow: "0 16px 50px #00000026",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 4 }}>
          <PinflowLogo />
          <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)" }}>
            Pinflow
          </span>
        </div>
        <h1 style={{ fontSize: 20, fontWeight: 650, color: "var(--ink)", margin: "10px 0 4px" }}>
          How do you want to run it?
        </h1>
        <p style={{ fontSize: 13, color: "var(--muted)", margin: "0 0 20px", lineHeight: 1.5 }}>
          Pinflow's KiCad integration always runs on your machine. Choose where the
          AI work is powered from — you can change this later in settings.
        </p>

        <ProviderForm
          value={draft}
          onChange={setDraft}
          onCloudSignedInChange={setCloudSignedIn}
          onKeyInvalidChange={setKeyInvalid}
          serverLlmDisabled={serverLlmDisabled}
        />

        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "flex-end",
            gap: 12,
            marginTop: 24,
          }}
        >
          {blocker && (
            <span style={{ fontSize: 12, color: "var(--muted)", textAlign: "right", lineHeight: 1.4 }}>
              {blocker}
            </span>
          )}
          <button
            type="button"
            onClick={commit}
            disabled={!cfg}
            style={primaryButtonStyle(!!cfg)}
          >
            Continue
          </button>
        </div>
      </div>
    </div>
  );
}

export function primaryButtonStyle(enabled: boolean): CSSProperties {
  return {
    height: 36,
    padding: "0 20px",
    fontSize: 13,
    fontWeight: 600,
    color: enabled ? "#fff" : "var(--muted)",
    background: enabled ? "var(--accent)" : "var(--bg-2)",
    border: `1px solid ${enabled ? "var(--accent)" : "var(--line)"}`,
    borderRadius: 8,
    cursor: enabled ? "pointer" : "not-allowed",
  };
}
