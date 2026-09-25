import { CircleAlert } from "lucide-react";
import type { ChatSubagentView } from "../../api/types";
import { subagentSummary } from "./useChatSubagents";

interface ChatSubagentAttentionProps {
  subagents: ChatSubagentView[];
  onReview: () => void;
}

/** One parent-owned entry point for child approvals; the pane keeps the full decision. */
export function ChatSubagentAttention({ subagents, onReview }: ChatSubagentAttentionProps) {
  const waiting = subagents.filter((item) => item.status === "waiting_approval");
  if (!waiting.length) return null;

  const first = waiting[0]!;
  const title = waiting.length === 1
    ? `${first.name} needs your approval`
    : `${waiting.length} subagents need your approval`;
  const detail = waiting.length === 1
    ? first.approval?.rationale?.trim() || `${first.name} is waiting for you to review a requested action.`
    : "Review each requested action before the waiting subagents continue.";

  return <section className="chat-subagent-attention" role="region" aria-label="Subagent request needing attention">
    <CircleAlert size={17} aria-hidden="true" />
    <div>
      <strong>{title}</strong>
      <p>{detail}</p>
      <small>{subagentSummary(subagents).map((item) => item.label).join(" · ")}</small>
    </div>
    <button type="button" className="button secondary" onClick={onReview}>
      {waiting.length === 1 ? "Review request" : "Review requests"}
    </button>
  </section>;
}
