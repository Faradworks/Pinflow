import type { KicadProject } from "../window-shell/KicadStatusChip";
import { PinflowLogo } from "../window-shell/PinflowLogo";

type Props = {
  project: KicadProject | null;
  source: string | null;
};

export function SchematicView({ project, source }: Props) {
  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "var(--sch-bg)",
      }}
    >
      <div
        style={{
          height: 40,
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "0 16px",
          borderBottom: "1px solid var(--line)",
          background: "var(--panel)",
          fontFamily: "var(--font-mono)",
          fontSize: 11,
          color: "var(--muted)",
        }}
      >
        <span style={{ color: "var(--ink-2)", fontWeight: 600 }}>schematic</span>
        <span style={{ color: "var(--muted-2)" }}>·</span>
        <span>{project?.schematic ?? "no schematic paired"}</span>
        {project?.highlighted && (
          <span
            style={{
              marginLeft: "auto",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              color: "rgb(46,160,67)",
              fontWeight: 600,
            }}
          >
            <span
              style={{
                width: 18,
                borderTop: "2px dashed rgb(46,160,67)",
                display: "inline-block",
              }}
            />
            staged changes · not yet committed
          </span>
        )}
      </div>

      <div style={{ flex: 1, minHeight: 0, position: "relative" }}>
        {source ? <KicanvasViewer source={source} /> : <PlaceholderBody project={project} />}
      </div>
    </div>
  );
}

function KicanvasViewer({ source }: { source: string }) {
  // Theme is set globally via localStorage in index.html — the embed's `theme`
  // attribute is not forwarded to the child schematic-app, so setting it here is a no-op.
  //
  // `controls="full"` (not "basic") is what builds KiCanvas's activity side-bar,
  // which hosts the Properties panel. Under "basic" the side-bar is null, so
  // clicking a component has nowhere to surface its properties. The embed
  // hardcodes `sidebarcollapsed`, so the panel stays hidden until the schematic
  // app's on_viewer_select fires change_activity("properties") on a click.
  return (
    <kicanvas-embed key={fingerprint(source)} controls="full">
      <kicanvas-source type="schematic">{source}</kicanvas-source>
    </kicanvas-embed>
  );
}

function PlaceholderBody({ project }: { project: KicadProject | null }) {
  const detected = !!project;
  const heading = detected
    ? "No schematic open"
    : "Open a KiCad project";
  const body = detected
    ? "Pinflow is paired, but KiCad doesn't have a schematic open yet. Open a .kicad_sch in KiCad and it'll render here."
    : "Open a KiCad project and Pinflow will render its schematic here — and live-preview every edit before it touches your board.";
  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 14,
        padding: 24,
        backgroundImage:
          "radial-gradient(var(--sch-grid) 1px, transparent 1px)",
        backgroundSize: "16px 16px",
      }}
    >
      <div
        style={{
          width: 96,
          height: 96,
          borderRadius: 22,
          border: "1.5px solid var(--line-2)",
          background: "var(--panel)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--muted-2)",
        }}
      >
        <PinflowLogo size={48} />
      </div>
      <div
        style={{
          fontSize: 14,
          fontWeight: 600,
          color: "var(--ink-2)",
          letterSpacing: "-0.005em",
        }}
      >
        {heading}
      </div>
      <div
        style={{
          fontSize: 12.5,
          color: "var(--muted)",
          lineHeight: 1.5,
          maxWidth: 320,
          textAlign: "center",
        }}
      >
        {body}
      </div>
    </div>
  );
}

// React key only; <kicanvas-source> is read once on mount, so we remount on source change.
function fingerprint(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return `${s.length}:${h}`;
}
