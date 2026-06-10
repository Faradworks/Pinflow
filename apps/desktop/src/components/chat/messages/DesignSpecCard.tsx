import type { ReactNode } from "react";
import type { DesignSpecData } from "../types";

type Props = { spec: DesignSpecData };

const HEAD = "1px solid var(--line)";

export function DesignSpecCard({ spec }: Props) {
  const computed = spec.components?.filter((c) => c.source === "computed") ?? [];
  const datasheet = spec.components?.filter((c) => c.source === "datasheet") ?? [];

  return (
    <div
      style={{
        border: HEAD,
        borderRadius: 10,
        background: "var(--panel-2)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: HEAD,
          background: "var(--panel)",
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
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
          design spec
        </span>
        <span style={{ fontSize: 11.5, color: "var(--ink-2)" }}>
          {spec.mpn}
          {spec.variant_code ? ` (${spec.variant_code})` : ""} ·{" "}
          {spec.topology?.replace("_", "-")} · {spec.vin} → {spec.vout}
          {spec.duty_cycle != null ? ` · D=${spec.duty_cycle}` : ""}
        </span>
      </div>

      <div style={{ padding: 12, display: "flex", flexDirection: "column", gap: 12 }}>
        {spec.blurb && (
          <div style={{ fontSize: 12.5, color: "var(--ink)" }}>{spec.blurb}</div>
        )}

        {spec.warnings?.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            {spec.warnings.map((w, i) => (
              <div
                key={i}
                style={{
                  fontSize: 11.5,
                  color: "var(--warn, #b9842b)",
                  fontFamily: "var(--font-mono)",
                }}
              >
                ⚠ {w}
              </div>
            ))}
          </div>
        )}

        <ComponentTable title="Computed" rows={computed} showEq />
        <ComponentTable title="From datasheet" rows={datasheet} />

        {spec.rail_map?.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <SectionLabel>pin → rail</SectionLabel>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {spec.rail_map.map((r) => (
                <span
                  key={r.pin_number}
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 11,
                    color: "var(--ink-2)",
                    padding: "1px 6px",
                    background: "var(--panel)",
                    border: HEAD,
                    borderRadius: 4,
                  }}
                >
                  {r.pin_number} {r.pin_name}
                  {r.rail ? ` → ${r.rail}` : ""}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <span
      style={{
        fontFamily: "var(--font-mono)",
        fontSize: 10.5,
        fontWeight: 600,
        color: "var(--ink-2)",
        letterSpacing: "0.04em",
        textTransform: "uppercase",
      }}
    >
      {children}
    </span>
  );
}

function ComponentTable({
  title,
  rows,
  showEq = false,
}: {
  title: string;
  rows: DesignSpecData["components"];
  showEq?: boolean;
}) {
  if (!rows || rows.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <SectionLabel>{title}</SectionLabel>
      {rows.map((c, i) => (
        <div
          key={`${c.refdes_hint}-${i}`}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 2,
            padding: "6px 8px",
            background: "var(--panel)",
            border: HEAD,
            borderRadius: 6,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 8,
              fontSize: 12.5,
            }}
          >
            <span
              style={{
                fontFamily: "var(--font-mono)",
                fontWeight: 600,
                color: "var(--ink)",
                minWidth: 28,
              }}
            >
              {c.refdes_hint}
            </span>
            <span
              style={{
                fontFamily: "var(--font-mono)",
                fontWeight: 600,
                color: "var(--ink)",
              }}
            >
              {c.value}
            </span>
            <span style={{ color: "var(--ink-2)", flex: 1 }}>{c.purpose}</span>
            {c.chip_pin_number ? (
              <span
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                  color: "var(--ink-2)",
                }}
              >
                pin {c.chip_pin_number}
              </span>
            ) : null}
          </div>
          {showEq && (c.equation || c.tolerance) ? (
            <div
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 10.5,
                color: "var(--ink-2)",
              }}
            >
              {c.equation}
              {c.equation && c.tolerance ? "  ·  " : ""}
              {c.tolerance}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
