type Props = {
  // The chosen option once answered ("Confirm" / "Discard"); undefined while
  // the prompt is still pending. NOT a boolean — a Discard answer is still an
  // answer, and must not render as a success.
  answer?: string;
  onConfirm: () => void;
  onReject: () => void;
};

export function ConfirmBar({ answer, onConfirm, onReject }: Props) {
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
    <div className="pf-fade-up" style={{ display: "flex", gap: 8, alignItems: "center" }}>
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
  );
}
