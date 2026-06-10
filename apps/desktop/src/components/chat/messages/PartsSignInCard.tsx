// In-chat card shown when a parts tool reports the catalogue needs a (free)
// Pinflow sign-in. One-click sign-in that keeps the BYOK key for the LLM — the
// server uses the session token only for the gateway parts proxy. Non-blocking:
// the agent's text already offers the manual MPN/PDF path alongside.

import { useState } from "react";
import type { CSSProperties } from "react";

type Phase = "idle" | "pending" | "done" | "error";

export function PartsSignInCard({
  onSignIn,
}: {
  hint?: string;
  onSignIn?: () => Promise<boolean>;
}) {
  const [phase, setPhase] = useState<Phase>("idle");

  async function signIn() {
    if (!onSignIn) return;
    setPhase("pending");
    try {
      const ok = await onSignIn();
      setPhase(ok ? "done" : "error");
    } catch {
      setPhase("error");
    }
  }

  return (
    <div style={card}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span aria-hidden="true">🔑</span>
        <span style={{ fontWeight: 600, color: "var(--ink)" }}>
          Parts lookup needs a Pinflow sign-in
        </span>
      </div>
      <div style={{ fontSize: 12.5, color: "var(--muted)", lineHeight: 1.5 }}>
        Sign in (free, no card) to turn on automatic part search and datasheet
        lookup — or just paste the MPN / attach the datasheet and I'll continue.
        Your own API key still runs the model.
      </div>

      {phase === "done" ? (
        <div style={{ color: "var(--success)", fontSize: 12.5 }}>
          ✓ Signed in — ask me again and I'll look it up.
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <button
            type="button"
            onClick={signIn}
            disabled={phase === "pending" || !onSignIn}
            style={{ ...signInBtn, opacity: phase === "pending" ? 0.6 : 1 }}
          >
            {phase === "pending" ? "Waiting for browser…" : "Sign in — free"}
          </button>
          {phase === "error" && (
            <span style={{ fontSize: 11.5, color: "var(--danger)" }}>
              Sign-in didn't complete — try again.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

const card: CSSProperties = {
  marginTop: 4,
  padding: "12px 14px",
  display: "flex",
  flexDirection: "column",
  gap: 10,
  background: "var(--bg-2)",
  border: "1px solid var(--line)",
  borderRadius: 10,
};

const signInBtn: CSSProperties = {
  height: 32,
  padding: "0 14px",
  fontSize: 12.5,
  fontWeight: 600,
  color: "#fff",
  background: "var(--accent)",
  border: "1px solid var(--accent)",
  borderRadius: 8,
  cursor: "pointer",
};
