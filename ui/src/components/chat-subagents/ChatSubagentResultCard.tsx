import { useState } from "react";
import { Check, CircleAlert, ExternalLink } from "lucide-react";
import type { ChatSubagentView } from "../../api/types";
import { compactTokens, elapsedLabel } from "./useChatSubagents";

/** Characters of a report shown before it has to be expanded. */
const PREVIEW_CHARS = 320;
const FAILURE_PREVIEW_CHARS = 160;

function postedReport(content: string): string {
  const reportStart = content.indexOf("\n\n");
  return reportStart >= 0 ? content.slice(reportStart + 2) : content;
}

/** Keep a saved result compact while its child record loads or after a later round supersedes it. */
export function ChatSubagentPostedResult({ content }: { content: string }) {
  const heading = content.split("\n", 1)[0];
  return <details className="chat-subagent-result chat-subagent-posted">
    <summary>{heading}</summary>
    <p>{postedReport(content)}</p>
  </details>;
}

interface ChatSubagentResultCardProps {
  subagent: ChatSubagentView;
  postedContent?: string;
  onOpenConversation: (childSessionId: string) => void;
}

/**
 * A finished child's report, in the parent transcript where it was posted.
 *
 * The report is the child's own words, so it is shown as text and never
 * summarised further here; the full conversation is one click away.
 */
export function ChatSubagentResultCard({ subagent, postedContent, onOpenConversation }: ChatSubagentResultCardProps) {
  const [expanded, setExpanded] = useState(false);
  const failed = subagent.status !== "completed";
  // Core's posted message also contains failure steps that are absent from the
  // subagent list response. Keep those details available behind the disclosure.
  const report = (postedContent ? postedReport(postedContent) : undefined)
    || subagent.result || subagent.error || "This subagent finished without a report.";
  const previewLength = failed ? FAILURE_PREVIEW_CHARS : PREVIEW_CHARS;
  const preview = failed ? report.split("\n", 1)[0].split(/(?=: [/~])/)[0] : report;
  const clipped = !expanded && (report.length > previewLength || preview.length < report.length);

  return <article className="chat-subagent-result" data-status={subagent.status}>
    <header>
      <span className="chat-subagent-result-icon" aria-hidden="true">
        {failed ? <CircleAlert size={15} /> : <Check size={15} />}
      </span>
      <strong>{subagent.status === "failed" ? "Subagent failed" : failed ? "Subagent stopped" : "Subagent finished"}</strong>
      <span className="chat-subagent-result-name">{subagent.name}</span>
      <small>
        {subagent.parentBackend === "harness" && subagent.model
          ? <span className="chat-subagent-result-model">{subagent.model.split("/").pop()} · </span>
          : null}
        {elapsedLabel(subagent.elapsedSeconds)}
        {subagent.usage.totalTokens ? ` · ${compactTokens(subagent.usage.totalTokens)} tokens` : ""}
      </small>
    </header>
    <p>{clipped ? `${preview.slice(0, previewLength).trimEnd()}…` : report}</p>
    <div className="chat-subagent-result-actions">
      {(report.length > previewLength || preview.length < report.length) && <button type="button" className="button quiet" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        {expanded ? "Show less" : "Show full result"}
      </button>}
      <button type="button" className="button quiet" onClick={() => onOpenConversation(subagent.childSessionId)}>
        <ExternalLink size={14} aria-hidden="true" /> Open conversation
      </button>
    </div>
  </article>;
}
