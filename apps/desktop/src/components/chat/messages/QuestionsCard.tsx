import type { Question } from "../types";

type Props = {
  // `locked` is accepted for backwards compatibility but no longer drives
  // disable/visual state — the per-question `answer` field does. A card
  // stays clickable until the user picks an option; after the click, the
  // answered question highlights its choice and fades the alternatives.
  questions: Question[];
  locked?: boolean;
  onAnswer?: (qid: string, option: string) => void;
};

export function QuestionsCard({ questions, onAnswer }: Props) {
  // The agent loop currently emits one question per ask_user, and its text
  // is identical to the parent ai message — so rendering it again here is
  // pure duplication. Drop the number + restated text for single-question
  // cards; only show them when there's >1 question (the numbered list is
  // load-bearing then).
  const showLabel = questions.length > 1;
  // Freeform-only ask_user (no `options`) — the chat input below IS the
  // answer mechanism, so render a small hint instead of an empty bordered
  // panel.
  const allFreeform = questions.every((q) => q.options.length === 0);
  if (allFreeform) {
    return (
      <div
        style={{
          fontSize: 11,
          color: "var(--muted)",
          fontFamily: "var(--font-mono)",
          paddingLeft: 4,
        }}
      >
        ↓ type your answer below
      </div>
    );
  }
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        padding: "10px 12px",
        background: "var(--panel-2)",
        border: "1px solid var(--line)",
        borderRadius: 10,
      }}
    >
      {questions.map((q, i) => (
        <div key={q.id} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {showLabel && (
            <div
              style={{
                fontSize: 12.5,
                color: "var(--ink-2)",
                lineHeight: 1.45,
                display: "flex",
                gap: 6,
              }}
            >
              <span
                style={{
                  color: "var(--muted-2)",
                  fontFamily: "var(--font-mono)",
                  fontSize: 10,
                  marginTop: 2,
                }}
              >
                {String(i + 1).padStart(2, "0")}
              </span>
              <span>{q.q}</span>
            </div>
          )}
          <div
            style={{
              display: "flex",
              gap: 6,
              flexWrap: "wrap",
              paddingLeft: showLabel ? 18 : 0,
            }}
          >
            {q.options.map((opt) => {
              const answered = q.answer !== undefined;
              const selected = answered && q.answer === opt;
              return (
                <button
                  key={opt}
                  type="button"
                  disabled={answered}
                  onClick={() => onAnswer?.(q.id, opt)}
                  style={{
                    fontFamily: "var(--font-ui)",
                    fontSize: 11.5,
                    fontWeight: 500,
                    padding: "4px 9px",
                    background: selected ? "var(--accent-soft)" : "var(--panel)",
                    color: selected ? "var(--accent)" : "var(--ink-2)",
                    border:
                      "1px solid " +
                      (selected
                        ? "color-mix(in oklab, var(--accent) 35%, transparent)"
                        : "var(--line)"),
                    borderRadius: 6,
                    cursor: answered ? "default" : "pointer",
                    opacity: answered && !selected ? 0.4 : 1,
                  }}
                >
                  {opt}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
