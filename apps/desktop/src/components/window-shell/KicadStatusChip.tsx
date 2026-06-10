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
  );
}
