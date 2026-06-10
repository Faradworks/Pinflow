export type Theme = "light" | "dark";

const KEY = "pinflow.theme";

export function readTheme(): Theme {
  try {
    const stored = localStorage.getItem(KEY);
    if (stored === "dark" || stored === "light") return stored;
  } catch {}
  if (typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
    return "dark";
  }
  return "light";
}

export function writeTheme(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {}
}
