import type { BlockDiagramEdge, BlockDiagramNode } from "../types";

type Props = {
  nodes: BlockDiagramNode[];
  edges: BlockDiagramEdge[];
};

export function BlockDiagramCard({ nodes, edges }: Props) {
  return (
    <div
      style={{
        border: "1px solid var(--line)",
        borderRadius: 10,
        background: "var(--panel-2)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: "1px solid var(--line)",
          background: "var(--panel)",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            fontWeight: 600,
            color: "var(--ink-2)",
            letterSpacing: "0.04em",
            textTransform: "uppercase",
          }}
        >
          block diagram
        </span>
        <span style={{ fontSize: 11.5, color: "var(--ink-2)" }}>
          {nodes.length} {nodes.length === 1 ? "block" : "blocks"} ·{" "}
          {edges.length} {edges.length === 1 ? "edge" : "edges"}
        </span>
      </div>

      <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 12 }}>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 8,
          }}
        >
          {nodes.map((n) => (
            <div
              key={n.id}
              style={{
                padding: "8px 12px",
                background: "var(--panel)",
                border: "1px solid var(--line)",
                borderRadius: 8,
                fontSize: 12.5,
                minWidth: 110,
              }}
            >
              <div style={{ fontWeight: 600, color: "var(--ink)" }}>{n.role}</div>
              <div
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                  color: "var(--ink-2)",
                  marginTop: 2,
                }}
              >
                {n.mpn ?? n.id}
              </div>
            </div>
          ))}
        </div>

        {edges.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {edges.map((e, i) => (
              <div
                key={`${e.from}-${e.to}-${e.interface}-${i}`}
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 11.5,
                  color: "var(--ink-2)",
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <span>{e.from}</span>
                <span style={{ color: "var(--ink-3, var(--ink-2))" }}>→</span>
                <span>{e.to}</span>
                <span
                  style={{
                    padding: "1px 6px",
                    background: "var(--panel)",
                    border: "1px solid var(--line)",
                    borderRadius: 4,
                    color: "var(--ink)",
                    fontWeight: 500,
                  }}
                >
                  {e.interface}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
