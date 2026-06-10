import { useState } from "react";

import type { DiffRow } from "../types";

type Props = {
  rows: DiffRow[];
  undoneIds?: Set<string>;
  confirmed?: boolean;
  onUndo?: (ref: string) => void;
};

export function DiffCard({ rows, undoneIds, confirmed, onUndo }: Props) {
  const [open, setOpen] = useState(true);
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
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 12px",
          cursor: "pointer",
          borderBottom: open ? "1px solid var(--line)" : "none",
          background: "var(--panel)",
        }}
      >
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            fontWeight: 600,
            color: "var(--ink-2)",
          }}
        >
          diff · {rows.length} changes
        </span>
        <span style={{ color: "var(--muted-2)" }}>·</span>
        <span
          style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--success)" }}
        >
          +{rows.filter((r) => r.sym === "+").length}
        </span>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--accent)" }}>
          ~{rows.filter((r) => r.sym === "~").length}
        </span>
        <div style={{ flex: 1 }} />
        {confirmed && (
          <span style={{ fontSize: 11, color: "var(--success)", fontWeight: 500 }}>
            ✓ committed
          </span>
        )}
        <svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          style={{
            transform: open ? "rotate(180deg)" : "none",
            transition: "transform 200ms",
            color: "var(--muted)",
          }}
        >
          <path
            d="M2 3.5l3 3 3-3"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>
      {open && (
        <div style={{ padding: "6px 4px" }}>
          {rows.map((r, i) => {
            const undone = undoneIds?.has(r.ref);
            return (
              <div
                key={i}
                style={{
                  display: "grid",
                  gridTemplateColumns: "18px 60px 1fr auto",
                  gap: 10,
                  alignItems: "center",
                  padding: "4px 12px",
                  opacity: undone ? 0.3 : 1,
                  textDecoration: undone ? "line-through" : "none",
                  fontFamily: "var(--font-mono)",
                  fontSize: 11.5,
                }}
              >
                <span
                  style={{
                    color:
                      r.sym === "+"
                        ? "var(--success)"
                        : r.sym === "-"
                          ? "var(--danger)"
                          : "var(--accent)",
                    fontWeight: 700,
                  }}
                >
                  {r.sym}
                </span>
                <span style={{ color: "var(--ink-2)", fontWeight: 600 }}>{r.ref}</span>
                <span style={{ color: "var(--ink)" }}>
                  {r.part}{" "}
                  <span style={{ color: "var(--muted)" }}>· {r.note}</span>
                </span>
                {!confirmed && !undone && r.sym !== "~" && (
                  <button
                    type="button"
                    onClick={() => onUndo?.(r.ref)}
                    style={{
                      background: "transparent",
                      border: "none",
                      color: "var(--muted)",
                      fontSize: 10,
                      cursor: "pointer",
                      fontFamily: "var(--font-mono)",
                    }}
                  >
                    undo
                  </button>
                )}
                {undone && (
                  <span style={{ color: "var(--muted-2)", fontSize: 10 }}>undone</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
