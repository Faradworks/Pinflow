import { useEffect, useRef, useState } from "react";

export type StagedAttachment = {
  // Local-only key (not the server attachment_id). The id is assigned when
  // we upload right before sending the message.
  key: string;
  file: File;
};

type Props = {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  disabled?: boolean;
  // When the agent is running, the send button becomes a Stop button.
  isStreaming?: boolean;
  isStopping?: boolean;
  onStop?: () => void;
  hint?: string;
  attachments: StagedAttachment[];
  onAttach: (files: File[]) => void;
  onRemoveAttachment: (key: string) => void;
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function PaperclipIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M10.4 3.6 5.2 8.8a2.2 2.2 0 0 0 3.1 3.1l5.6-5.6a3.6 3.6 0 0 0-5.1-5.1L3.2 6.8a5 5 0 0 0 7.1 7.1l4.5-4.5"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function AttachmentChip({
  file,
  onRemove,
}: {
  file: File;
  onRemove: () => void;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "3px 4px 3px 8px",
        background: "var(--panel)",
        border: "1px solid var(--line)",
        borderRadius: 6,
        fontFamily: "var(--font-mono)",
        fontSize: 11,
        color: "var(--ink)",
        maxWidth: 240,
      }}
      title={`${file.name} · ${formatSize(file.size)}`}
    >
      <span aria-hidden="true">📎</span>
      <span
        style={{
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          flex: 1,
        }}
      >
        {file.name}
      </span>
      <span style={{ color: "var(--muted)" }}>{formatSize(file.size)}</span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${file.name}`}
        style={{
          width: 16,
          height: 16,
          padding: 0,
          background: "transparent",
          border: "none",
          color: "var(--muted)",
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          borderRadius: 4,
        }}
      >
        <svg width="9" height="9" viewBox="0 0 9 9" fill="none">
          <path d="M1 1l7 7M8 1L1 8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
        </svg>
      </button>
    </span>
  );
}

export function ChatInput({
  value,
  onChange,
  onSend,
  disabled,
  isStreaming,
  isStopping,
  onStop,
  hint,
  attachments,
  onAttach,
  onRemoveAttachment,
}: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 140) + "px";
  }, [value]);

  const hasAttachments = attachments.length > 0;
  const canSend = (!!value.trim() || hasAttachments) && !disabled;

  function handleFilesChosen(list: FileList | null) {
    if (!list || list.length === 0) return;
    onAttach(Array.from(list));
  }

  return (
    <div
      onDragEnter={(e) => {
        if (e.dataTransfer?.types?.includes("Files")) {
          e.preventDefault();
          setDragging(true);
        }
      }}
      onDragOver={(e) => {
        if (e.dataTransfer?.types?.includes("Files")) {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }
      }}
      onDragLeave={(e) => {
        // Only drop the dragging flag when leaving the container itself, not
        // any internal child element.
        if (e.currentTarget === e.target) setDragging(false);
      }}
      onDrop={(e) => {
        if (!e.dataTransfer?.files?.length) return;
        e.preventDefault();
        setDragging(false);
        handleFilesChosen(e.dataTransfer.files);
      }}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: "10px 12px",
        border: `1px solid ${dragging ? "var(--accent)" : "var(--line)"}`,
        borderRadius: 14,
        background: "var(--panel-2)",
        transition: "border-color 120ms",
      }}
    >
      {dragging && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background:
              "color-mix(in oklab, var(--accent) 18%, var(--panel-2))",
            border: "1px dashed var(--accent)",
            borderRadius: 14,
            pointerEvents: "none",
            color: "var(--ink)",
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            zIndex: 2,
          }}
        >
          Drop datasheet PDF to attach
        </div>
      )}

      {hasAttachments && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {attachments.map((a) => (
            <AttachmentChip
              key={a.key}
              file={a.file}
              onRemove={() => onRemoveAttachment(a.key)}
            />
          ))}
        </div>
      )}

      <input
        ref={fileRef}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        style={{ display: "none" }}
        onChange={(e) => {
          handleFilesChosen(e.target.files);
          // reset so re-selecting the same file fires onChange again
          e.target.value = "";
        }}
      />

      <textarea
        ref={ref}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            if (canSend) onSend();
          }
        }}
        placeholder={
          hasAttachments
            ? "Add a note (optional)…"
            : "Describe a change to your schematic…"
        }
        rows={1}
        style={{
          width: "100%",
          background: "transparent",
          border: "none",
          outline: "none",
          resize: "none",
          color: "var(--ink)",
          fontFamily: "var(--font-ui)",
          fontSize: 13.5,
          lineHeight: 1.5,
          padding: 0,
        }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={disabled}
          aria-label="Attach PDF"
          title="Attach datasheet PDF"
          style={{
            width: 26,
            height: 26,
            background: "transparent",
            border: "1px solid var(--line)",
            color: "var(--muted)",
            borderRadius: 6,
            cursor: disabled ? "default" : "pointer",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <PaperclipIcon />
        </button>
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 10,
            color: "var(--muted)",
          }}
        >
          {hint ?? "ui preview · agent wiring coming soon"}
        </span>
        <div style={{ flex: 1 }} />
        {isStreaming ? (
          <button
            type="button"
            onClick={() => onStop?.()}
            disabled={isStopping}
            aria-label="Stop"
            title={isStopping ? "Stopping…" : "Stop the agent"}
            style={{
              width: 28,
              height: 28,
              background: isStopping ? "var(--line)" : "var(--ink)",
              color: isStopping ? "var(--muted)" : "var(--bg)",
              border: "none",
              borderRadius: 7,
              cursor: isStopping ? "default" : "pointer",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <svg width="10" height="10" viewBox="0 0 10 10" fill="currentColor">
              <rect x="0.5" y="0.5" width="9" height="9" rx="1.5" />
            </svg>
          </button>
        ) : (
          <button
            type="button"
            onClick={() => canSend && onSend()}
            disabled={!canSend}
            aria-label="Send"
            style={{
              width: 28,
              height: 28,
              background: canSend ? "var(--ink)" : "var(--line)",
              color: canSend ? "var(--bg)" : "var(--muted)",
              border: "none",
              borderRadius: 7,
              cursor: canSend ? "pointer" : "default",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <path
                d="M6 10V2M3 5l3-3 3 3"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
