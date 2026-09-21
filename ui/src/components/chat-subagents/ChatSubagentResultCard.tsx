import { useState } from "react";
import { Check, CircleAlert, ExternalLink } from "lucide-react";
import type { ChatSubagentView } from "../../api/types";
import { compactTokens, elapsedLabel } from "./useChatSubagents";

/** Characters of a report shown before it has to be expanded. */
const PREVIEW_CHARS = 320;

interface ChatSubagentResultCardProps {
  subagent: ChatSubagentView;
  onOpenConversation: (childSessionId: string) => void;
}

/**
 * A finished child's report, in the parent transcript where it was posted.
 *
 * The report is the child's own words, so it is shown as text and never
 * summarised further here; the full conversation is one click away.
 */
export function ChatSubagentResultCard({ subagent, onOpenConversation }: ChatSubagentResultCardProps) {
  const [expanded, setExpanded] = useState(false);
  const failed = subagent.status !== "completed";
  const report = subagent.result || subagent.error || "This subagent finished without a report.";
  const clipped = !expanded && report.length > PREVIEW_CHARS;

  return <article className="chat-subagent-result" data-status={subagent.status}>
    <header>
      <span className="chat-subagent-result-icon" aria-hidden="true">
        {failed ? <CircleAlert size={15} /> : <Check size={15} />}
      </span>
      <strong>{failed ? "Subagent stopped" : "Subagent finished"}</strong>
      <span className="chat-subagent-result-name">{subagent.name}</span>
      <small>
        {subagent.parentBackend === "harness" && subagent.model
          ? <span className="chat-subagent-result-model">{subagent.model.split("/").pop()} · </span>
          : null}
        {elapsedLabel(subagent.elapsedSeconds)}
        {subagent.usage.totalTokens ? ` · ${compactTokens(subagent.usage.totalTokens)} tokens` : ""}
      </small>
    </header>
    <p>{clipped ? `${report.slice(0, PREVIEW_CHARS)}…` : report}</p>
    <div className="chat-subagent-result-actions">
      {report.length > PREVIEW_CHARS && <button type="button" className="button quiet" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        {expanded ? "Show less" : "Show full result"}
      </button>}
      <button type="button" className="button quiet" onClick={() => onOpenConversation(subagent.childSessionId)}>
        <ExternalLink size={14} aria-hidden="true" /> Open conversation
      </button>
    </div>
  </article>;
}
