// Pinflow Cloud auth/balance chip for the title bar (cloud mode only). Signed
// in → the ⚡ balance chip + a top-up popover (opens Stripe Checkout via the
// local service). Signed out → a "Sign in" pill, so an expired/dropped session
// is visible instead of silent. The popover also has a "Sign out" (clears the
// local session) — handy for testing the signed-out state.

import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";

import { api, type CloudCredits } from "../../lib/api";

const AMOUNTS = [10, 25, 50, 100];

export function CreditsChip({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<CloudCredits | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [signingIn, setSigningIn] = useState(false);
  const pollRef = useRef<number | null>(null);

  function load() {
    api.cloudCredits().then(setData).catch(() => {});
  }

  useEffect(() => {
    load();
  }, [refreshKey]);

  useEffect(() => {
    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      if (pollRef.current !== null) clearInterval(pollRef.current);
    };
  }, []);

  // Kick off cloud sign-in (opens the hosted login in the browser), poll until
  // the local service reports a session, then refresh.
  async function signIn() {
    setSigningIn(true);
    try {
      const { state } = await api.startCloudLogin();
      const deadline = Date.now() + 120_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        try {
          if ((await api.cloudAuthStatus(state)).signed_in) break;
        } catch {
          /* keep polling */
        }
      }
    } catch {
      /* stays signed-out; the pill remains */
    }
    setSigningIn(false);
    load();
  }

  // Clears the local Pinflow session (not the Clerk browser session) — instant
  // way to land in the signed-out state for testing.
  async function signOut() {
    setOpen(false);
    try {
      await api.cloudLogout();
    } catch {
      /* ignore */
    }
    load();
  }

  if (!data || !data.configured) return null;

  if (!data.signed_in) {
    return (
      <button
        type="button"
        disabled={signingIn}
        onClick={signIn}
        title="Sign in to Pinflow Cloud"
        style={{
          ...chipStyle,
          borderColor: "var(--accent)",
          color: "var(--accent)",
          cursor: signingIn ? "default" : "pointer",
        }}
      >
        {signingIn ? "Signing in…" : "Sign in"}
      </button>
    );
  }

  const bal = typeof data.balance === "number" ? data.balance : null;

  async function topUp(amount: number) {
    setBusy(true);
    try {
      await api.cloudTopup(amount);
    } catch {
      /* ignore — surfaced as no balance change */
    }
    setBusy(false);
    setOpen(false);
    // Checkout completes in the browser; poll for the credited balance.
    if (pollRef.current !== null) clearInterval(pollRef.current);
    let n = 0;
    pollRef.current = window.setInterval(() => {
      load();
      if (++n >= 20 && pollRef.current !== null) clearInterval(pollRef.current);
    }, 3000);
  }

  const low = bal !== null && bal <= 1;

  return (
    <div style={{ position: "relative" }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title="Pinflow credits"
        style={{ ...chipStyle, borderColor: low ? "var(--pending)" : "var(--line)" }}
      >
        <span style={{ color: low ? "var(--pending)" : "var(--accent)" }}>⚡</span>
        {bal === null ? "—" : bal.toFixed(2)}
      </button>
      {open && (
        <div style={popoverStyle}>
          <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>
            Balance:{" "}
            <b style={{ color: "var(--ink)", fontFamily: "var(--font-mono)" }}>
              {bal === null ? "—" : bal.toFixed(2)}
            </b>{" "}
            credits
          </div>
          <div style={{ fontSize: 11.5, color: "var(--ink-2)", marginBottom: 6 }}>Top up</div>
          <div style={{ display: "flex", gap: 6 }}>
            {AMOUNTS.map((a) => (
              <button key={a} type="button" disabled={busy} onClick={() => topUp(a)} style={amtBtn}>
                ${a}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8 }}>
            Opens Stripe Checkout in your browser.
          </div>
          <button type="button" onClick={signOut} style={signOutBtn}>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

const chipStyle: CSSProperties = {
  height: 28,
  display: "flex",
  alignItems: "center",
  gap: 5,
  padding: "0 10px",
  fontSize: 12,
  fontFamily: "var(--font-mono)",
  color: "var(--ink-2)",
  background: "transparent",
  border: "1px solid var(--line)",
  borderRadius: 6,
  cursor: "pointer",
};

const popoverStyle: CSSProperties = {
  position: "absolute",
  top: 34,
  right: 0,
  zIndex: 80,
  width: 230,
  padding: "12px 14px",
  background: "var(--panel)",
  border: "1px solid var(--line)",
  borderRadius: 10,
  boxShadow: "0 10px 30px #00000033",
};

const amtBtn: CSSProperties = {
  flex: 1,
  height: 30,
  fontSize: 12.5,
  fontWeight: 600,
  color: "var(--ink)",
  background: "var(--panel-2)",
  border: "1px solid var(--line-2)",
  borderRadius: 7,
  cursor: "pointer",
};

const signOutBtn: CSSProperties = {
  width: "100%",
  marginTop: 12,
  height: 28,
  fontSize: 11.5,
  color: "var(--muted)",
  background: "transparent",
  border: "1px solid var(--line)",
  borderRadius: 6,
  cursor: "pointer",
};
