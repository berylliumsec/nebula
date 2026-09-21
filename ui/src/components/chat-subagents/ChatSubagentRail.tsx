import { PanelRight, Split } from "lucide-react";
import type { ChatSubagentView } from "../../api/types";
import { compactTokens, subagentSummary } from "./useChatSubagents";

interface ChatSubagentRailProps {
  subagents: ChatSubagentView[];
  open: boolean;
  onToggle: () => void;
}

/**
 * The strip above the composer: how many children are working, what they have
 * spent, and the way into the pane. It stays out of the way when nothing is
 * delegated, and never blocks the composer while they run.
 */
export function ChatSubagentRail({ subagents, open, onToggle }: ChatSubagentRailProps) {
  if (subagents.length === 0) return null;
  const counts = subagentSummary(subagents);
  const tokens = subagents.reduce((total, item) => total + item.usage.totalTokens, 0);
  return <div className="chat-subagent-rail" role="status" aria-label="Subagents">
    <Split size={14} aria-hidden="true" />
    <strong>Subagents</strong>
    <span className="chat-subagent-counts">
      {counts.map((item) => <span key={item.tone} data-tone={item.tone}>
        <span className="chat-subagent-dot" aria-hidden="true" />{item.label}
      </span>)}
    </span>
    <small>{compactTokens(tokens)} tokens</small>
    <button
      type="button"
      className="icon-button subtle"
      aria-label={open ? "Hide subagents" : "Show subagents"}
      aria-expanded={open}
      title={open ? "Hide subagents" : "Show subagents"}
      onClick={onToggle}
    >
      <PanelRight size={16} aria-hidden="true" />
    </button>
  </div>;
}
