type Props = {
  kind: "place" | "route";
  text: string;
};

export function SchemaActionLine({ kind, text }: Props) {
  const icon =
    kind === "place" ? (
      <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
        <rect x="1.5" y="1.5" width="9" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
        <circle cx="6" cy="6" r="1.2" fill="currentColor" />
      </svg>
    ) : (
      <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
        <path
          d="M2 4h3l2 4h3"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  return (
    <div
      className="pf-fade-up"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        paddingLeft: 4,
        fontFamily: "var(--font-mono)",
        fontSize: 11.5,
        color: "var(--muted)",
      }}
    >
      <span style={{ color: "var(--pending)" }}>{icon}</span>
      <span style={{ color: "var(--ink-2)" }}>{text}</span>
    </div>
  );
}
