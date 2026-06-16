import type { Message, Question } from "./types";
import { AiText } from "./messages/AiText";
import { BlockDiagramCard } from "./messages/BlockDiagramCard";
import { ConfirmBar } from "./messages/ConfirmBar";
import { DesignSpecCard } from "./messages/DesignSpecCard";
import { DiffCard } from "./messages/DiffCard";
import { PartsSignInCard } from "./messages/PartsSignInCard";
import { QuestionsCard } from "./messages/QuestionsCard";
import { SchemaActionLine } from "./messages/SchemaActionLine";
import { SystemMessage } from "./messages/SystemMessage";
import { ThinkingBlock } from "./messages/ThinkingBlock";
import { ToolCard } from "./messages/ToolCard";
import { UserBubble } from "./messages/UserBubble";

type Props = {
  message: Message;
  onAnswer?: (msgId: string, qid: string, option: string) => void;
  onConfirm?: (msgId: string) => void;
  onReject?: (msgId: string) => void;
  onSignInForParts?: () => Promise<boolean>;
  // For ask_user tool messages: whether the linked question has been answered.
  // Computed by the parent (ChatPanel) by peeking at the next message.
  answered?: boolean;
};

// A QuestionsCard whose options are exactly ["Confirm","Discard"] is treated
// as a staging-confirmation prompt and rendered as ConfirmBar instead — the
// system prompt instructs the agent to use this exact shape after staging
// any edit.
function isConfirmQuestion(q: Question): boolean {
  return (
    q.options.length === 2 &&
    q.options[0]?.toLowerCase() === "confirm" &&
    q.options[1]?.toLowerCase() === "discard"
  );
}

export function MessageView({ message: m, onAnswer, onConfirm, onReject, onSignInForParts, answered }: Props) {
  if (m.kind === "user") return <UserBubble text={m.text} attachments={m.attachments} />;
  if (m.kind === "thinking") return <ThinkingBlock text={m.text} streaming={m.streaming} />;
  if (m.kind === "tool")
    return (
      <ToolCard
        tool={m.tool}
        title={m.title}
        meta={m.meta}
        live={m.live}
        answered={answered}
      />
    );
  if (m.kind === "action") return <SchemaActionLine kind={m.actKind} text={m.text} />;
  if (m.kind === "system") return <SystemMessage text={m.text} />;
  if (m.kind === "block_diagram") {
    return <BlockDiagramCard nodes={m.nodes} edges={m.edges} />;
  }
  if (m.kind === "design_spec") {
    return <DesignSpecCard spec={m.spec} />;
  }
  if (m.kind === "signin_required") {
    return <PartsSignInCard hint={m.hint} onSignIn={onSignInForParts} />;
  }
  if (m.kind === "ai") {
    const confirmQ = m.questions?.find(isConfirmQuestion);
    const otherQs = m.questions?.filter((q) => !isConfirmQuestion(q));
    return (
      <AiText text={m.text}>
        {otherQs && otherQs.length > 0 && (
          <QuestionsCard
            questions={otherQs}
            locked={!!m.locked}
            onAnswer={(qid, opt) => onAnswer?.(m.id, qid, opt)}
          />
        )}
        {m.diff && <DiffCard rows={m.diff} confirmed={!!m.confirm} />}
        {confirmQ && (
          <ConfirmBar
            answer={confirmQ.answer}
            estimate={m.cost ?? undefined}
            onConfirm={() => onAnswer?.(m.id, confirmQ.id, "Confirm")}
            onReject={() => onAnswer?.(m.id, confirmQ.id, "Discard")}
          />
        )}
        {!confirmQ && m.confirm !== undefined && (
          <ConfirmBar
            answer={m.confirm ? "Confirm" : undefined}
            onConfirm={() => onConfirm?.(m.id)}
            onReject={() => onReject?.(m.id)}
          />
        )}
      </AiText>
    );
  }
  return null;
}
