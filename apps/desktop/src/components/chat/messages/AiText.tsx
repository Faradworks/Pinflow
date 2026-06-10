import type { ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Props = {
  text: string;
  children?: ReactNode;
};

export function AiText({ text, children }: Props) {
  return (
    <div
      className="pf-fade-up"
      style={{
        maxWidth: "92%",
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      {text && (
        <div
          className="pf-md"
          style={{
            fontSize: 14,
            lineHeight: 1.6,
            color: "var(--ink)",
          }}
        >
          {/* The agent emits GitHub-flavored markdown — remark-gfm adds the
              table / strikethrough / autolink support it relies on. */}
          <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
        </div>
      )}
      {children}
    </div>
  );
}
