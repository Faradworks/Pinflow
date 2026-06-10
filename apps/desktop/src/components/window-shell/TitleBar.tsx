import type { Theme } from "../../lib/theme";
import { CreditsChip } from "./CreditsChip";
import { KicadStatusChip, type KicadProject } from "./KicadStatusChip";
import { PinflowLogo } from "./PinflowLogo";
import { ThemeToggle } from "./ThemeToggle";

type Props = {
  theme: Theme;
  onToggleTheme: () => void;
  project: KicadProject | null;
  onProjectChange: (p: KicadProject | null) => void;
  onOpenSettings: () => void;
  showCredits: boolean;
  creditsRefresh: number;
};

export function TitleBar({
  theme,
  onToggleTheme,
  project,
  onProjectChange,
  onOpenSettings,
  showCredits,
  creditsRefresh,
}: Props) {
  return (
    <div
      style={{
        height: 44,
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        gap: 10,
        // Leave room for the real macOS traffic lights on the left.
        padding: "0 14px 0 78px",
        borderBottom: "1px solid var(--line)",
        background: "var(--panel)",
        // @ts-expect-error — Tauri-specific drag region; not in React's CSSProperties.
        WebkitAppRegion: "drag",
      }}
    >
      {/* Brand + breadcrumb */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <PinflowLogo />
        <span
          style={{
            fontSize: 13,
            fontWeight: 600,
            letterSpacing: "-0.005em",
            color: "var(--ink)",
          }}
        >
          Pinflow
        </span>
        {project && (
          <>
            <span style={{ color: "var(--muted-2)", fontSize: 12 }}>›</span>
            <span
              style={{
                fontSize: 12,
                color: "var(--muted)",
                fontFamily: "var(--font-mono)",
              }}
              title={project.path ?? project.schematic}
            >
              {project.name}
            </span>
            {project.schematic && (
              <>
                <span style={{ color: "var(--muted-2)", fontSize: 12 }}>›</span>
                <span
                  style={{
                    fontSize: 12,
                    color: "var(--ink-2)",
                    fontFamily: "var(--font-mono)",
                  }}
                >
                  {project.schematic}
                </span>
              </>
            )}
          </>
        )}
      </div>

      <div style={{ flex: 1 }} />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          // @ts-expect-error — opt interactive controls out of drag region.
          WebkitAppRegion: "no-drag",
        }}
      >
        <KicadStatusChip onProjectChange={onProjectChange} />
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
        {showCredits && <CreditsChip refreshKey={creditsRefresh} />}
        <button
          type="button"
          onClick={onOpenSettings}
          title="Backend & AI provider"
          aria-label="Backend & AI provider"
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
          <svg width="14" height="14" viewBox="0 0 18 18" fill="none">
            <path
              d="M3 6h6M12.5 6H15M3 12h2.5M9 12h6"
              stroke="currentColor"
              strokeWidth="1.3"
              strokeLinecap="round"
            />
            <circle cx="10.5" cy="6" r="1.9" stroke="currentColor" strokeWidth="1.3" />
            <circle cx="7" cy="12" r="1.9" stroke="currentColor" strokeWidth="1.3" />
          </svg>
        </button>
      </div>
    </div>
  );
}
