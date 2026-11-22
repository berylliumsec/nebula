import type { NativeHookExecution } from "../api/types";

/** One row of a turn's lifecycle hook outcomes: identical successful runs share a row. */
export interface HookOutcomeRow {
  key: string;
  name: string;
  event: string;
  status: string;
  count: number;
  execution: NativeHookExecution;
}

const SETTLED = new Set<NativeHookExecution["status"]>(["complete", "reconciled"]);

/** "tool.before" reads "Before tool"; any other event reads as words, capitalized. */
export function hookEventLabel(eventName: string): string {
  const phased = /^([a-z_]+)\.(before|after)$/i.exec(eventName);
  const words = phased ? `${phased[2]} ${phased[1]}` : eventName;
  const text = words.replaceAll(".", " ").replaceAll("_", " ").trim();
  return text ? text[0].toLocaleUpperCase() + text.slice(1) : eventName;
}

/** A row per hook, event and outcome for runs that settled cleanly; every run that needs attention keeps its own row. */
export function groupHookOutcomes(executions: NativeHookExecution[], nameOf: (hookId: string) => string): HookOutcomeRow[] {
  const rows: HookOutcomeRow[] = [];
  const grouped = new Map<string, HookOutcomeRow>();
  for (const execution of executions) {
    const quiet = SETTLED.has(execution.status) && !execution.error && !(execution.reconciliation && typeof execution.reconciliation.detail === "string");
    const status = execution.status.replaceAll("_", " ");
    if (!quiet) {
      rows.push({ key: execution.id, name: nameOf(execution.hookId), event: hookEventLabel(execution.eventName), status, count: 1, execution });
      continue;
    }
    const key = [execution.hookId, execution.eventName, execution.status, execution.sideEffects].join("\u0000");
    const existing = grouped.get(key);
    if (existing) { existing.count += 1; continue; }
    const row = { key, name: nameOf(execution.hookId), event: hookEventLabel(execution.eventName), status, count: 1, execution };
    grouped.set(key, row);
    rows.push(row);
  }
  return rows;
}

interface HookOutcomesProps {
  executions: NativeHookExecution[];
  nameOf: (hookId: string) => string;
  open: boolean;
}

/** The turn's lifecycle hook outcomes: a collapsed summary over grouped rows. */
export function HookOutcomes({ executions, nameOf, open }: HookOutcomesProps) {
  const settled = executions.filter((item) => SETTLED.has(item.status)).length;
  return <details className="chat-hook-outcomes" data-guide="hook-outcomes" open={open}>
    <summary>Lifecycle hooks · {settled}/{executions.length} completed</summary>
    <div role="list" aria-label="Lifecycle hook outcomes">{groupHookOutcomes(executions, nameOf).map(({ key, name, event, status, count, execution }) =>
      <div role="listitem" key={key} data-status={execution.status}>
        <strong>{name}</strong>
        <small>{event} · {count > 1 ? `${count.toLocaleString()} ` : ""}{status}{execution.sideEffects !== "none" ? ` · ${execution.sideEffects} effects` : ""}</small>
        {execution.error && <span role="alert">{execution.error}</span>}
        {execution.status === "interrupted" && execution.lateOutcomeStatus && <small>Later process exit: {execution.lateOutcomeStatus}{execution.lateOutcomeExitCode !== undefined ? ` (code ${execution.lateOutcomeExitCode})` : ""}. {execution.lateOutcomeStatus === "complete" ? "Checking the saved result." : "Effects may be partial; verify before continuing."}</small>}
        {execution.reconciliation && typeof execution.reconciliation.detail === "string" && <small>{execution.reconciliation.detail}</small>}
      </div>)}
    </div>
  </details>;
}
