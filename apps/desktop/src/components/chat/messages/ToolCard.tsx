import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";

import type { ToolMetaRow } from "../types";

type Props = {
  tool: string;
  title: string;
  meta: ToolMetaRow[];
  // `live` reflects the stream state: true while the agent is still working
  // on the turn that emitted this tool, false once the turn ended. Default
  // (undefined) = treat as live (back-compat).
  live?: boolean;
  // Only meaningful for `tool === "ask_user"`: true once the user has
  // selected an option for every question on the linked ai message. Drives
  // the "awaiting" → "done" badge transition.
  answered?: boolean;
};

const TOOL_ICONS: Record<string, ReactNode> = {
  query_lcsc: (
    <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
      <circle cx="6" cy="6" r="4.2" stroke="currentColor" strokeWidth="1.4" />
      <path d="M9.2 9.2L12 12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  ),
  fetch_datasheet: (
    <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
      <path
        d="M7 2v7m0 0l-2.4-2.4M7 9l2.4-2.4"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M2.5 11.5h9" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  ),
};

const DEFAULT_ICON: ReactNode = (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
    <rect x="2" y="2" width="10" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.4" />
  </svg>
);

export function ToolCard({ tool, title, meta, live, answered }: Props) {
  // Track whether the user has manually toggled, so a later live→collapsed
  // transition doesn't override a manual expand (or vice versa).
  const [userToggled, setUserToggled] = useState(false);
  const [expanded, setExpanded] = useState<boolean>(live ?? true);
  const prevLive = useRef(live);

  useEffect(() => {
    if (prevLive.current === live) return;
    prevLive.current = live;
    if (userToggled) return;
    setExpanded(live ?? true);
  }, [live, userToggled]);

  function toggle() {
    setUserToggled(true);
    setExpanded((v) => !v);
  }

  if (!expanded)
    return <CompactView tool={tool} title={title} answered={answered} onClick={toggle} />;
  return (
    <ExpandedView
      tool={tool}
      title={title}
      meta={meta}
      answered={answered}
      onCollapse={toggle}
    />
  );
}

// ask_user emits a tool event right before the agent suspends to wait for
// the user. Calling that "done" while the user is still deciding is wrong,
// so we show "awaiting" in pending color until they pick an option, then
// flip to "done" in success color.
function statusFor(tool: string, answered?: boolean): { label: string; color: string } {
  if (tool === "ask_user" && !answered)
    return { label: "awaiting", color: "var(--pending)" };
  return { label: "done", color: "var(--success)" };
}

function CompactView({
  tool,
  title,
  answered,
  onClick,
}: {
  tool: string;
  title: string;
  answered?: boolean;
  onClick: () => void;
}) {
  const showTitle = title && title !== tool;
  const status = statusFor(tool, answered);
  return (
    <button
      type="button"
      onClick={onClick}
      title="Show tool details"
      style={{
        display: "inline-flex",
        alignSelf: "flex-start",
        alignItems: "center",
        gap: 7,
        padding: "2px 8px 2px 4px",
        background: "transparent",
        border: "none",
        cursor: "pointer",
        color: "var(--muted)",
        fontFamily: "var(--font-mono)",
        fontSize: 11,
      }}
    >
      <svg width="8" height="8" viewBox="0 0 8 8" fill="none" aria-hidden="true">
        <path
          d="M2 1.5l3 2.5-3 2.5"
          stroke="currentColor"
          strokeWidth="1.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span style={{ color: "var(--ink-2)" }}>{tool}</span>
      {showTitle && (
        <>
          <span style={{ color: "var(--muted-2)" }}>·</span>
          <span>{title}</span>
        </>
      )}
      <span style={{ color: "var(--muted-2)" }}>·</span>
      <span style={{ color: status.color }}>{status.label}</span>
    </button>
  );
}

function ExpandedView({
  tool,
  title,
  meta,
  answered,
  onCollapse,
}: {
  tool: string;
  title: string;
  meta: ToolMetaRow[];
  answered?: boolean;
  onCollapse: () => void;
}) {
  const icon = TOOL_ICONS[tool] ?? DEFAULT_ICON;
  const status = statusFor(tool, answered);
  const showCheck = status.label === "done";
  return (
    <div
      className="pf-fade-up"
      style={{
        display: "flex",
        flexDirection: "column",
        border: "1px solid var(--line)",
        borderRadius: 10,
        background: "var(--panel-2)",
        overflow: "hidden",
        maxWidth: 460,
      }}
    >
      <button
        type="button"
        onClick={onCollapse}
        title="Hide tool details"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "7px 11px",
          borderBottom: "1px solid var(--line)",
          background: "var(--panel)",
          cursor: "pointer",
          textAlign: "left",
          width: "100%",
          color: "inherit",
        }}
      >
        <div
          style={{
            width: 18,
            height: 18,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--accent)",
          }}
        >
          {icon}
        </div>
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 11,
            fontWeight: 600,
            color: "var(--ink-2)",
          }}
        >
          {tool}
        </span>
        <span style={{ color: "var(--muted-2)" }}>·</span>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--ink)" }}>
          {title}
        </span>
        <div style={{ flex: 1 }} />
        <span
          style={{
            fontSize: 11,
            color: status.color,
            display: "flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          {showCheck && (
            <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
              <path
                d="M2 5l2 2 4-4"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          )}
          {status.label}
        </span>
      </button>
      {meta.length > 0 && (
        <div
          style={{
            padding: "8px 11px",
            display: "grid",
            gridTemplateColumns: "auto 1fr",
            columnGap: 10,
            rowGap: 4,
            fontFamily: "var(--font-mono)",
            fontSize: 11,
          }}
        >
          {meta.map((row, i) => (
            <Fragment key={i}>
              <span style={{ color: "var(--muted)" }}>{row.k}</span>
              <span style={{ color: "var(--ink-2)" }}>{row.v}</span>
            </Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
