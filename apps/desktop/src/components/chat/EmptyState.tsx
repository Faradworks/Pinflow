type Props = {
  schematicName?: string | null;
};

export function EmptyState({ schematicName }: Props) {
  return (
    <div
      className="pf-fade-up"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        padding: "16px 0",
      }}
    >
      <div
        style={{
          fontSize: 20,
          fontWeight: 600,
          color: "var(--ink)",
          letterSpacing: "-0.015em",
        }}
      >
        What should we add?
      </div>
      <div
        style={{
          fontSize: 13.5,
          color: "var(--muted)",
          lineHeight: 1.55,
          maxWidth: 480,
        }}
      >
        {schematicName ? (
          <>
            Pinflow is paired with{" "}
            <span style={{ fontFamily: "var(--font-mono)", color: "var(--ink-2)" }}>
              {schematicName}
            </span>
            . Tell me what to add, swap, or fix — I'll draft it before anything touches your board.
          </>
        ) : (
          <>
            Tell me what to add, swap, or fix. I'll draft it before anything touches your board.
            Open a KiCad project to pair Pinflow with a schematic.
          </>
        )}
      </div>
    </div>
  );
}
