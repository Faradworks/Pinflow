// Pinflow Cloud sign-in widget. Drives the browser-mediated login on the local
// service: start → poll /auth/status → signed-in. The JWT stays in the local
// service; this only reflects status and reports it up so the parent can enable
// Continue/Save.

import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

import { api, type CloudAuthStatus } from "../../lib/api";

type Phase = "checking" | "idle" | "pending" | "signedin";

export function CloudSignIn({
  onSignedInChange,
}: {
  onSignedInChange: (signedIn: boolean) => void;
}) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [info, setInfo] = useState<CloudAuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);
  const deadlineRef = useRef<number>(0);

  function stopPoll() {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  function markSignedIn(s: CloudAuthStatus) {
    setInfo(s);
    setPhase("signedin");
    onSignedInChange(true);
  }

  useEffect(() => {
    let alive = true;
    api
      .cloudAuthStatus()
      .then((s) => {
        if (!alive) return;
        if (s.signed_in) markSignedIn(s);
        else {
          setPhase("idle");
          onSignedInChange(false);
        }
      })
      .catch(() => {
        if (!alive) return;
        setPhase("idle");
        onSignedInChange(false);
      });
    return () => {
      alive = false;
      stopPoll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function signIn() {
    setError(null);
    try {
      const { state, login_url } = await api.startCloudLogin();
      setLoginUrl(login_url);
      setPhase("pending");
      deadlineRef.current = Date.now() + 120_000;
      stopPoll();
      pollRef.current = window.setInterval(async () => {
        if (Date.now() > deadlineRef.current) {
          stopPoll();
          setPhase("idle");
          setError("Sign-in timed out — try again.");
          return;
        }
        try {
          const s = await api.cloudAuthStatus(state);
          if (s.signed_in) {
            stopPoll();
            markSignedIn(s);
          }
        } catch {
          /* keep polling */
        }
      }, 1500);
    } catch (e) {
      setError((e as Error)?.message ?? "Failed to start sign-in");
      setPhase("idle");
    }
  }

  async function signOut() {
    stopPoll();
    try {
      // Full sign-out (Clerk + Pinflow) so the next sign-in can pick a different
      // account — opens a browser tab that clears the Clerk session.
      await api.cloudSignout();
    } catch {
      /* ignore */
    }
    setInfo(null);
    setPhase("idle");
    onSignedInChange(false);
  }

  if (phase === "checking") {
    return <Box muted>Checking sign-in…</Box>;
  }

  if (phase === "signedin") {
    const who = info?.email || info?.name || info?.user_id || "your account";
    return (
      <Box>
        <span style={{ color: "var(--success)" }}>✓ Signed in</span> as{" "}
        <b style={{ fontFamily: "var(--font-mono)" }}>{who}</b>
        <button
          type="button"
          onClick={signOut}
          style={linkBtn}
          title="Signs out of Pinflow and your browser session, so you can switch accounts"
        >
          Sign out
        </button>
      </Box>
    );
  }

  if (phase === "pending") {
    return (
      <Box>
        Waiting for sign-in in your browser…
        {loginUrl && (
          <a href={loginUrl} style={{ color: "var(--accent)", marginLeft: 8 }}>
            reopen
          </a>
        )}
      </Box>
    );
  }

  // idle
  return (
    <div style={{ marginTop: 16 }}>
      <button type="button" onClick={signIn} style={signInBtn}>
        Sign in / register
      </button>
      <div style={{ fontSize: 11.5, color: "var(--muted)", marginTop: 8 }}>
        Opens your browser to sign in. You'll get free credits to start.
      </div>
      {error && (
        <div style={{ fontSize: 11.5, color: "var(--danger)", marginTop: 6 }}>{error}</div>
      )}
    </div>
  );
}

function Box({ children, muted }: { children: ReactNode; muted?: boolean }) {
  return (
    <div
      style={{
        marginTop: 16,
        padding: "10px 12px",
        fontSize: 12.5,
        lineHeight: 1.5,
        color: muted ? "var(--muted)" : "var(--ink-2)",
        background: "var(--bg-2)",
        border: "1px solid var(--line)",
        borderRadius: 8,
        display: "flex",
        alignItems: "center",
        gap: 6,
      }}
    >
      {children}
    </div>
  );
}

const signInBtn: CSSProperties = {
  height: 34,
  padding: "0 16px",
  fontSize: 13,
  fontWeight: 600,
  color: "#fff",
  background: "var(--accent)",
  border: "1px solid var(--accent)",
  borderRadius: 8,
  cursor: "pointer",
};

const linkBtn: CSSProperties = {
  marginLeft: "auto",
  background: "transparent",
  border: "1px solid var(--line)",
  borderRadius: 6,
  padding: "3px 9px",
  fontSize: 12,
  color: "var(--ink-2)",
  cursor: "pointer",
};
