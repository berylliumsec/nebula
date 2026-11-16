import { ExternalLink } from "lucide-react";
import type { ChatSubagentView } from "../api/types";
import { ACTIVE_STATUSES, statusLabel } from "./chat-subagents";
import styles from "./NebulaControlAssistant.module.css";

/**
 * The subagents this Butler panel has seen running since the operator's last
 * request: running ones first, finished ones until the next request.
 */
export function butlerActivityRows(subagents: ChatSubagentView[], shownIds: ReadonlySet<string>): ChatSubagentView[] {
  const rows = subagents.filter((item) => shownIds.has(item.id) || ACTIVE_STATUSES.has(item.status));
  const active = rows.filter((item) => ACTIVE_STATUSES.has(item.status));
  return [...active, ...rows.filter((item) => !ACTIVE_STATUSES.has(item.status))];
}

/** A report Core posted after the panel last read Butler's transcript. */
export function hasUnreadReport(subagents: ChatSubagentView[], messageIds: ReadonlySet<string>): boolean {
  return subagents.some((item) => item.resultMessageId && !messageIds.has(item.resultMessageId));
}

function rowState(item: ChatSubagentView): { label: string; tone: "working" | "done" | "attention" } {
  if (item.status === "completed") return { label: "Finished", tone: "done" };
  if (item.status === "waiting_approval") return { label: "Needs approval", tone: "attention" };
  if (ACTIVE_STATUSES.has(item.status)) {
    const steps = item.stepCount ? ` · ${item.stepCount} ${item.stepCount === 1 ? "step" : "steps"}` : "";
    return { label: `${statusLabel(item.status)}${steps}`, tone: "working" };
  }
  return { label: statusLabel(item.status), tone: "attention" };
}

export function ButlerSubagentActivity({ rows, subagentsHref, onOpen }: {
  rows: ChatSubagentView[];
  /** Butler's conversation with its Subagents drawer open. */
  subagentsHref: string;
  onOpen: (href: string) => void;
}) {
  if (!rows.length) return null;
  const working = rows.filter((item) => ACTIVE_STATUSES.has(item.status)).length;
  const heading = working
    ? `${working} ${working === 1 ? "subagent" : "subagents"} working`
    : `${rows.length === 1 ? "Subagent" : "Subagents"} finished`;
  return <section className={styles.subagentActivity} aria-label="Butler subagents" data-state={working ? "working" : "done"}>
    <div className={styles.subagentActivityHeading}>
      <span className={styles.turnStatusDot} aria-hidden="true" />
      <strong role="status">{heading}</strong>
      <a href={subagentsHref} onClick={(event) => { event.preventDefault(); onOpen(subagentsHref); }}>Subagents<ExternalLink size={12} aria-hidden="true" /></a>
    </div>
    <ul>
      {rows.map((item) => {
        const state = rowState(item);
        return <li key={item.id}><span>{item.name}</span><small data-tone={state.tone}>{state.label}</small></li>;
      })}
    </ul>
  </section>;
}
