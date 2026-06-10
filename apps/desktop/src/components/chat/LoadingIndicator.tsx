// Shown at the bottom of the message log while an SSE stream is in flight,
// so the user sees that the agent is working even between tool calls or
// during the gap before the first event arrives.
export function LoadingIndicator() {
  return (
    <div
      className="pf-fade-up"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        paddingLeft: 4,
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        color: "var(--muted)",
        textTransform: "uppercase",
        letterSpacing: "0.04em",
      }}
    >
      <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
        <span className="pf-typing-dot" style={{ animationDelay: "0ms" }} />
        <span className="pf-typing-dot" style={{ animationDelay: "150ms" }} />
        <span className="pf-typing-dot" style={{ animationDelay: "300ms" }} />
      </span>
      <span>working</span>
    </div>
  );
}
