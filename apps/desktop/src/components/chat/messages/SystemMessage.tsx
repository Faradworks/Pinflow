export function SystemMessage({ text }: { text: string }) {
  return (
    <div
      className="pf-fade-up"
      style={{
        fontSize: 12,
        color: "var(--muted)",
        fontFamily: "var(--font-mono)",
        padding: "4px 10px",
        alignSelf: "center",
        border: "1px dashed var(--line)",
        borderRadius: 6,
      }}
    >
      {text}
    </div>
  );
}
