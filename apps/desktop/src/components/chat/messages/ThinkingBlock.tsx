type Props = {
  text: string;
  streaming: boolean;
};

export function ThinkingBlock({ text, streaming }: Props) {
  return (
    <div
      className="pf-fade-up"
      style={{ display: "flex", gap: 10, alignItems: "flex-start", maxWidth: "94%" }}
    >
      <div
        style={{
          width: 14,
          flexShrink: 0,
          marginTop: 4,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 4,
        }}
      >
        <div
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: streaming ? "var(--pending)" : "var(--muted-2)",
            boxShadow: streaming ? "0 0 0 0 var(--pending)" : "none",
            animation: streaming ? "pf-pulse-ring 1.4s infinite" : "none",
          }}
        />
        <div style={{ flex: 1, width: 1, background: "var(--line)", minHeight: 10 }} />
      </div>
      <div style={{ flex: 1, paddingTop: 1 }}>
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: "var(--muted)",
            fontFamily: "var(--font-mono)",
            textTransform: "uppercase",
            letterSpacing: "0.04em",
            marginBottom: 4,
          }}
        >
          {streaming ? "thinking" : "thought"}{" "}
          <span style={{ color: "var(--muted-2)", fontWeight: 500 }}>· stream</span>
        </div>
        <div
          className={streaming ? "pf-caret" : ""}
          style={{
            fontSize: 13,
            lineHeight: 1.55,
            color: "var(--muted)",
            fontStyle: "italic",
            whiteSpace: "pre-wrap",
          }}
        >
          {text}
        </div>
      </div>
    </div>
  );
}
