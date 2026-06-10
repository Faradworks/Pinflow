type Props = {
  text: string;
  onClick?: () => void;
};

export function Suggestion({ text, onClick }: Props) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        padding: "5px 10px",
        background: "var(--panel-2)",
        border: "1px solid var(--line)",
        borderRadius: 6,
        color: "var(--ink-2)",
        fontFamily: "var(--font-ui)",
        fontSize: 12,
        cursor: onClick ? "pointer" : "default",
      }}
    >
      {text}
    </button>
  );
}
