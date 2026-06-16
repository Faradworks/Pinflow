import type { CSSProperties } from "react";

import type { GateCost } from "../types";

type Props = {
  // The chosen option once answered ("Confirm" / "Discard"); undefined while
  // the prompt is still pending. NOT a boolean — a Discard answer is still an
  // answer, and must not render as a success.
  answer?: string;
  // Fuzzy "cost to finish from here" range, shown above the buttons while pending.
  estimate?: GateCost;
  onConfirm: () => void;
  onReject: () => void;
};

// Honest, approximate "cost to apply" line. The big upfront cost (e.g. the
// datasheet read) is already spent by this point, so this is correctly small —
// it tells the user finishing is cheap, not a false-precise total.
function CostHint({ estimate }: { estimate: GateCost }) {
  const { unit, lo, hi, balance } = estimate;
  const range =
    unit === "credits"
      ? `${lo.toFixed(2)}–${hi.toFixed(2)} cr to apply`
      : `$${lo.toFixed(3)}–$${hi.toFixed(3)} to apply`;
  const bal =
    unit === "credits" && balance != null ? ` · ${balance.toFixed(2)} cr left` : "";
  return (
    <div style={hintStyle} title="Approximate — the remaining steps are bounded; the upfront work is already done">
      <span style={{ color: "var(--accent)" }}>⚡</span>
      <span>≈ {range}</span>
      {bal && <span style={{ color: "var(--muted)" }}>{bal}</span>}
    </div>
  );
}

const hintStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: 11,
  fontFamily: "var(--font-mono)",
  color: "var(--muted)",
  padding: "0 2px",
};

export function ConfirmBar({ answer, estimate, onConfirm, onReject }: Props) {
  if (answer !== undefined) {
    const discarded = answer.toLowerCase() === "discard";
    // The UI only knows the user's *choice* — not whether a commit, stage, or
    // disk write followed. The agent's next messages report the real outcome.
    // So this bar states the choice and nothing more.
    const accent = discarded ? "var(--ink-2)" : "var(--success)";
    return (
      <div
        className="pf-fade-up"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          background: `color-mix(in oklab, ${accent} 12%, var(--panel))`,
          border: `1px solid color-mix(in oklab, ${accent} 35%, transparent)`,
          borderRadius: 10,
          fontSize: 12.5,
          color: "var(--ink)",
        }}
      >
        {discarded ? (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <circle cx="7" cy="7" r="6" stroke={accent} strokeWidth="1.2" />
            <path
              d="M5 5l4 4M9 5l-4 4"
              stroke={accent}
              strokeWidth="1.6"
              strokeLinecap="round"
            />
          </svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <circle cx="7" cy="7" r="6" stroke={accent} strokeWidth="1.2" />
            <path
              d="M4 7l2 2 4-4"
              stroke={accent}
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        )}
        {discarded ? "Discarded — no changes made." : "Confirmed."}
        <div style={{ flex: 1 }} />
      </div>
    );
  }
  return (
    <div className="pf-fade-up" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {estimate && <CostHint estimate={estimate} />}
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <button
        type="button"
        onClick={onConfirm}
        style={{
          flex: 1,
          padding: "9px 14px",
          background: "var(--ink)",
          color: "var(--bg)",
          border: "1px solid var(--ink)",
          borderRadius: 8,
          fontFamily: "var(--font-ui)",
          fontSize: 13,
          fontWeight: 500,
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 6,
        }}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path
            d="M2 6l3 3 5-6"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Confirm
      </button>
      <button
        type="button"
        onClick={onReject}
        style={{
          padding: "9px 14px",
          background: "transparent",
          color: "var(--ink-2)",
          border: "1px solid var(--line)",
          borderRadius: 8,
          fontFamily: "var(--font-ui)",
          fontSize: 13,
          fontWeight: 500,
          cursor: "pointer",
        }}
      >
        Discard
      </button>
      </div>
    </div>
  );
}
