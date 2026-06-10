// Anthropic API key input with inline validation. Debounces, then checks the
// key against Anthropic's free GET /v1/models (via the local service) so the
// user gets instant ✓/✗ instead of a cryptic failure on their first message.

import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";

import { api } from "../../lib/api";

type Status = "empty" | "checking" | "valid" | "invalid" | "unknown";

export function KeyField({
  value,
  onChange,
  onInvalidChange,
}: {
  value: string;
  onChange: (v: string) => void;
  onInvalidChange: (invalid: boolean) => void;
}) {
  const [status, setStatus] = useState<Status>(value.trim() ? "checking" : "empty");
  const timer = useRef<number | null>(null);
  const seq = useRef(0);

  // Only a confirmed 401 gates Save — checking/unknown/network errors don't.
  useEffect(() => {
    onInvalidChange(status === "invalid");
  }, [status, onInvalidChange]);

  useEffect(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    const key = value.trim();
    if (!key) {
      setStatus("empty");
      return;
    }
    setStatus("checking");
    const mySeq = ++seq.current;
    timer.current = window.setTimeout(() => {
      api
        .validateAnthropicKey(key)
        .then((res) => {
          if (mySeq !== seq.current) return; // a newer keystroke superseded this
          setStatus(res.valid ? "valid" : res.unknown ? "unknown" : "invalid");
        })
        .catch(() => {
          if (mySeq === seq.current) setStatus("unknown");
        });
    }, 600);
    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, [value]);

  return (
    <div style={{ marginTop: 16 }}>
      <label style={labelStyle}>Anthropic API key</label>
      <div style={{ position: "relative" }}>
        <input
          type="password"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="sk-ant-…"
          spellCheck={false}
          autoComplete="off"
          style={{ ...inputStyle, borderColor: borderColor(status) }}
        />
        <span style={{ ...glyphStyle, color: glyphColor(status) }}>{glyph(status)}</span>
      </div>
      <div style={{ fontSize: 11.5, marginTop: 6, color: msgColor(status) }}>
        {message(status)}
      </div>
    </div>
  );
}

function glyph(s: Status): string {
  return s === "valid" ? "✓" : s === "invalid" ? "✗" : s === "checking" ? "…" : "";
}
function glyphColor(s: Status): string {
  return s === "valid" ? "var(--success)" : s === "invalid" ? "var(--danger)" : "var(--muted)";
}
function borderColor(s: Status): string {
  return s === "valid" ? "var(--success)" : s === "invalid" ? "var(--danger)" : "var(--line-2)";
}
function msgColor(s: Status): string {
  return s === "invalid" ? "var(--danger)" : "var(--muted)";
}
function message(s: Status): string {
  switch (s) {
    case "valid":
      return "Key verified with Anthropic. Stored locally; sent only with chat requests.";
    case "invalid":
      return "Anthropic rejected this key (401). Check it and try again.";
    case "checking":
      return "Checking key…";
    case "unknown":
      return "Couldn't verify right now — it may still work. Stored locally on this machine.";
    default:
      return "Stored locally on this machine; sent only with chat requests.";
  }
}

const labelStyle: CSSProperties = {
  display: "block",
  fontSize: 12,
  fontWeight: 600,
  color: "var(--ink-2)",
  marginBottom: 6,
};

const inputStyle: CSSProperties = {
  width: "100%",
  height: 34,
  padding: "0 30px 0 10px",
  fontSize: 13,
  fontFamily: "var(--font-mono)",
  color: "var(--ink)",
  background: "var(--panel)",
  border: "1px solid var(--line-2)",
  borderRadius: 7,
  outline: "none",
};

const glyphStyle: CSSProperties = {
  position: "absolute",
  right: 11,
  top: 0,
  height: 34,
  display: "flex",
  alignItems: "center",
  fontSize: 14,
  fontWeight: 700,
};
