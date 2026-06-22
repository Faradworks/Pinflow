import { useEffect, useState } from "react";

import { api } from "../../lib/api";

export type KicadProject = {
  name: string;
  schematic: string;
  path: string | null;
  schematic_path: string | null;
  schematic_source: string | null;
  staged: boolean;
  stage_stale: boolean;
  highlighted: boolean;
};

type Props = {
  onProjectChange?: (p: KicadProject | null) => void;
};

export function KicadStatusChip({ onProjectChange }: Props) {
  const [project, setProject] = useState<KicadProject | null>(null);
  const [loading, setLoading] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      const r = await api.detectActiveProject();
      const next: KicadProject | null = r.detected
        ? {
            name: r.name,
            schematic: r.schematic,
            path: r.path,
            schematic_path: r.schematic_path,
            schematic_source: r.schematic_source,
            staged: r.staged,
            stage_stale: r.stage_stale,
            highlighted: r.highlighted,
          }
        : null;
      setProject(next);
      onProjectChange?.(next);
    } catch (e) {
      console.error("detectActiveProject failed:", e);
      setProject(null);
      onProjectChange?.(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const detected = !!project;
  const label = loading
    ? "detecting…"
    : detected
      ? `KiCad · ${project.name}`
      : "KiCad · not detected";
  const tooltip = project?.path ?? project?.schematic ?? "Click to refresh";

  return (
    <div style={{ position: "relative", display: "flex", alignItems: "center", gap: 4 }}>
      <button
        type="button"
        onClick={refresh}
        disabled={loading}
        title={tooltip}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 9px",
          border: "1px solid var(--line)",
          borderRadius: 6,
          background: "transparent",
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--muted)",
          cursor: loading ? "wait" : "pointer",
          opacity: loading ? 0.6 : 1,
          whiteSpace: "nowrap",
        }}
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: detected ? "var(--success)" : "var(--muted-2)",
            boxShadow: detected ? "0 0 8px var(--success)" : "none",
          }}
        />
        {label}
        {detected && !project.path && (
          <span
            style={{ color: "var(--pending)", marginLeft: 2 }}
            title="lsof could not resolve the project path — library writes unavailable"
          >
            (path?)
          </span>
        )}
      </button>

      {/* When KiCad isn't detected, offer a "why?" affordance: the usual cause
          is the IPC API being off in KiCad's preferences. */}
      {!detected && !loading && (
        <button
          type="button"
          onClick={() => setShowHelp((v) => !v)}
          aria-label="Why isn't KiCad detected?"
          title="Why isn't KiCad detected?"
          style={{
            width: 18,
            height: 18,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            border: "1px solid var(--line)",
            borderRadius: "50%",
            background: "transparent",
            color: "var(--muted)",
            fontSize: 11,
            fontWeight: 600,
            cursor: "pointer",
            lineHeight: 1,
          }}
        >
          ?
        </button>
      )}

      {showHelp && !detected && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            right: 0,
            zIndex: 70,
            width: 300,
            padding: "12px 14px",
            background: "var(--panel)",
            border: "1px solid var(--line)",
            borderRadius: 8,
            boxShadow: "0 10px 30px #00000033",
            fontFamily: "var(--font-sans, inherit)",
            fontSize: 12,
            lineHeight: 1.55,
            color: "var(--ink-2)",
            whiteSpace: "normal",
            textAlign: "left",
          }}
        >
          <div style={{ fontWeight: 650, color: "var(--ink)", marginBottom: 6 }}>
            KiCad not detected
          </div>
          Pinflow talks to KiCad over its IPC API. If KiCad is open but still not
          showing here, enable the API:
          <ol style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            <li>
              In KiCad: <strong>Preferences → Plugins</strong>
            </li>
            <li>
              Enable <strong>“Enable IPC API server”</strong>
            </li>
            <li>Restart KiCad, then click the chip to re-check.</li>
          </ol>
        </div>
      )}
    </div>
  );
}
