import type { UserAttachment } from "../types";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function AttachmentChip({ a }: { a: UserAttachment }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 8px",
        background: "color-mix(in oklab, var(--accent) 14%, transparent)",
        border: "1px solid color-mix(in oklab, var(--accent) 28%, transparent)",
        borderRadius: 6,
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        color: "var(--ink)",
        maxWidth: "100%",
      }}
      title={`${a.filename} · ${formatSize(a.size)}`}
    >
      <span aria-hidden="true">📎</span>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {a.filename}
      </span>
      <span style={{ color: "var(--muted)" }}>{formatSize(a.size)}</span>
    </span>
  );
}

export function UserBubble({
  text,
  attachments,
}: {
  text: string;
  attachments?: UserAttachment[];
}) {
  return (
    <div
      className="pf-fade-up"
      style={{
        alignSelf: "flex-end",
        maxWidth: "78%",
        background: "var(--accent-soft)",
        border: "1px solid color-mix(in oklab, var(--accent) 22%, transparent)",
        color: "var(--ink)",
        padding: "8px 12px",
        borderRadius: "12px 12px 4px 12px",
        fontSize: 13.5,
        lineHeight: 1.5,
        whiteSpace: "pre-wrap",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      {attachments && attachments.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {attachments.map((a, i) => (
            <AttachmentChip key={i} a={a} />
          ))}
        </div>
      )}
      {text && <span>{text}</span>}
    </div>
  );
}
