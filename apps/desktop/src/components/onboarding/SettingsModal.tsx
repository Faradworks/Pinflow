// Settings modal — reachable from the title-bar gear. Lets the user switch
// provider / edit the key or URL after onboarding, or reset (which clears the
// stored config and bounces back to the first-run screen).

import { useState } from "react";
import type { CSSProperties } from "react";

import { getConfig, saveConfig, type PinflowConfig } from "../../lib/config";
import { primaryButtonStyle } from "./OnboardingScreen";
import {
  ProviderForm,
  configToDraft,
  draftToConfig,
  draftBlockerReason,
  useServerLlmDisabled,
  type ProviderDraft,
} from "./ProviderForm";

export function SettingsModal({
  onClose,
  onSaved,
  onReset,
}: {
  onClose: () => void;
  onSaved: (cfg: PinflowConfig) => void;
  onReset: () => void;
}) {
  const [draft, setDraft] = useState<ProviderDraft>(() => configToDraft(getConfig()));
  const [cloudSignedIn, setCloudSignedIn] = useState(false);
  const [keyInvalid, setKeyInvalid] = useState(false);
  const serverLlmDisabled = useServerLlmDisabled(cloudSignedIn);
  const cfg = draftToConfig(draft, cloudSignedIn, keyInvalid, serverLlmDisabled);
  const blocker = draftBlockerReason(draft, cloudSignedIn, keyInvalid, serverLlmDisabled);

  function save() {
    if (!cfg) return;
    saveConfig(cfg);
    onSaved(cfg);
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: "absolute",
        inset: 0,
        zIndex: 60,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#0008",
        padding: 24,
        overflow: "auto",
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "min(680px, 100%)",
          background: "var(--panel)",
          border: "1px solid var(--line)",
          borderRadius: 14,
          padding: "22px 24px 20px",
          boxShadow: "0 16px 50px #0000003a",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 16,
          }}
        >
          <h2 style={{ fontSize: 16, fontWeight: 650, color: "var(--ink)", margin: 0 }}>
            Backend & AI provider
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            style={{
              width: 26,
              height: 26,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: "transparent",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink-2)",
              cursor: "pointer",
            }}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path
                d="M3 3l6 6M9 3l-6 6"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
              />
            </svg>
          </button>
        </div>

        <ProviderForm
          value={draft}
          onChange={setDraft}
          onCloudSignedInChange={setCloudSignedIn}
          onKeyInvalidChange={setKeyInvalid}
          serverLlmDisabled={serverLlmDisabled}
        />

        {blocker && (
          <div
            style={{
              marginTop: 18,
              fontSize: 12,
              color: "var(--muted)",
              lineHeight: 1.45,
              textAlign: "right",
            }}
          >
            {blocker}
          </div>
        )}

        <div
          style={{
            display: "flex",
            alignItems: "center",
            marginTop: blocker ? 10 : 24,
            gap: 10,
          }}
        >
          <button type="button" onClick={onReset} style={textButtonStyle}>
            Reset…
          </button>
          <div style={{ flex: 1 }} />
          <button type="button" onClick={onClose} style={textButtonStyle}>
            Cancel
          </button>
          <button type="button" onClick={save} disabled={!cfg} style={primaryButtonStyle(!!cfg)}>
            Save
          </button>
        </div>
      </div>
    </div>
  );
}

const textButtonStyle: CSSProperties = {
  height: 36,
  padding: "0 14px",
  fontSize: 13,
  fontWeight: 500,
  color: "var(--ink-2)",
  background: "transparent",
  border: "1px solid var(--line)",
  borderRadius: 8,
  cursor: "pointer",
};
