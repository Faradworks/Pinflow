import type { Theme } from "../../lib/theme";

type Props = {
  theme: Theme;
  onToggle: () => void;
};

export function ThemeToggle({ theme, onToggle }: Props) {
  return (
    <button
      type="button"
      onClick={onToggle}
      title="Toggle theme"
      aria-label="Toggle theme"
      style={{
        width: 28,
        height: 28,
        background: "transparent",
        border: "1px solid var(--line)",
        borderRadius: 6,
        color: "var(--ink-2)",
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {theme === "dark" ? (
        <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
          <path
            d="M11 8.5A4.5 4.5 0 0 1 5.5 3a4.5 4.5 0 1 0 5.5 5.5z"
            stroke="currentColor"
            strokeWidth="1.3"
          />
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
          <circle cx="7" cy="7" r="2.6" stroke="currentColor" strokeWidth="1.3" />
          <path
            d="M7 1.5v1.6M7 10.9v1.6M1.5 7h1.6M10.9 7h1.6M3.1 3.1l1.1 1.1M9.8 9.8l1.1 1.1M3.1 10.9l1.1-1.1M9.8 4.2l1.1-1.1"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinecap="round"
          />
        </svg>
      )}
    </button>
  );
}
