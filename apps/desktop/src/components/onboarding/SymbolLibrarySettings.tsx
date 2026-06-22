// KiCad symbol-library directory control, shown in the Settings modal. Lets the
// user point Pinflow at their KiCad symbols when the platform default isn't
// where their install put them (common on Linux / custom prefixes / portable
// installs). Self-contained: loads its own status and talks to /kicad/symbol-library.

import { useEffect, useState } from "react";
import type { CSSProperties } from "react";

import { api, type SymbolLibraryStatus } from "../../lib/api";

// Tauri injects this when running inside the desktop shell. Pure-browser dev
// (vite on :1420) won't have it — we hide the native picker and fall back to
// the editable text input there.
const inTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

async function pickDirectory(defaultPath?: string): Promise<string | null> {
  // Dynamic import so the bundle still loads in a plain browser without the
  // Tauri runtime (the static import resolves fine, but keep it lazy/guarded).
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({
    directory: true,
    multiple: false,
    title: "Select KiCad symbol library directory",
    defaultPath: defaultPath || undefined,
  });
  return typeof picked === "string" ? picked : null;
}

export function SymbolLibrarySettings() {
  const [status, setStatus] = useState<SymbolLibraryStatus | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const s = await api.getSymbolLibrary();
      setStatus(s);
      setDraft(s.override ?? "");
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function save(dir: string | null) {
    setBusy(true);
    setError(null);
    try {
      const s = await api.setSymbolLibrary(dir);
      setStatus(s);
      setDraft(s.override ?? "");
    } catch (e) {
      // Surface the backend's "not a directory" / "no .kicad_sym files" message.
      const msg = String((e as Error)?.message ?? e);
      setError(msg.replace(/^\/kicad\/symbol-library → \d+\s*/, ""));
    } finally {
      setBusy(false);
    }
  }

  async function browse() {
    setError(null);
    try {
      const picked = await pickDirectory(draft.trim() || status?.dir);
      if (picked) await save(picked);
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    }
  }

  const missing = status != null && !status.exists;
  const dirty = status != null && draft.trim() !== (status.override ?? "");

  return (
    <div style={{ marginTop: 22, borderTop: "1px solid var(--line)", paddingTop: 18 }}>
      <h3 style={{ fontSize: 13, fontWeight: 650, color: "var(--ink)", margin: "0 0 4px" }}>
        KiCad symbol library
      </h3>
      <p style={{ fontSize: 12, color: "var(--muted)", lineHeight: 1.5, margin: "0 0 12px" }}>
        Where Pinflow looks for KiCad's bundled symbols. Override this if a part
        can't be found because your libraries live elsewhere.
      </p>

      {status && (
        <div
          style={{
            fontSize: 11.5,
            fontFamily: "var(--font-mono)",
            color: missing ? "var(--danger, #d9534f)" : "var(--muted)",
            marginBottom: 10,
          }}
        >
          {missing ? "✕ not found: " : "✓ "}
          {status.dir}
          {!missing && ` · ${status.symbol_count} libraries`}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={status?.defaults?.[0] ?? "/path/to/kicad/symbols"}
          spellCheck={false}
          style={inputStyle}
        />
        {inTauri && (
          <button
            type="button"
            onClick={browse}
            disabled={busy}
            title="Pick a folder"
            style={buttonStyle(!busy)}
          >
            Browse…
          </button>
        )}
        <button
          type="button"
          onClick={() => save(draft.trim() || null)}
          disabled={busy || !dirty}
          style={buttonStyle(!busy && dirty)}
        >
          {busy ? "Saving…" : "Save"}
        </button>
        {status?.override && (
          <button
            type="button"
            onClick={() => save(null)}
            disabled={busy}
            title="Revert to the platform default"
            style={buttonStyle(!busy)}
          >
            Reset
          </button>
        )}
      </div>

      {error && (
        <div style={{ marginTop: 8, fontSize: 11.5, color: "var(--danger, #d9534f)" }}>
          {error}
        </div>
      )}
    </div>
  );
}

const inputStyle: CSSProperties = {
  flex: 1,
  height: 34,
  padding: "0 10px",
  fontSize: 12,
  fontFamily: "var(--font-mono)",
  color: "var(--ink)",
  background: "var(--bg)",
  border: "1px solid var(--line)",
  borderRadius: 8,
};

function buttonStyle(enabled: boolean): CSSProperties {
  return {
    height: 34,
    padding: "0 14px",
    fontSize: 12.5,
    fontWeight: 500,
    color: enabled ? "var(--ink)" : "var(--muted-2)",
    background: "transparent",
    border: "1px solid var(--line)",
    borderRadius: 8,
    cursor: enabled ? "pointer" : "default",
    whiteSpace: "nowrap",
  };
}
