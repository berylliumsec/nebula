import { ChevronDown } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import {
  activityLedgerStatusLabel,
  type ActivityLedgerEntry,
  type ActivityLedgerStatus,
  type ActivityLedgerViewModel,
} from "./activityLedgerModel";

function durationLabel(durationMs: number | undefined): string | undefined {
  if (!durationMs) return undefined;
  if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`;
  const minutes = Math.floor(durationMs / 60_000);
  const seconds = Math.round((durationMs % 60_000) / 1000);
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function timeLabel(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return undefined;
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(parsed);
}

function receiptParts(model: ActivityLedgerViewModel): string[] {
  const parts: string[] = [];
  if (model.totalTasks !== undefined) parts.push(`${model.completedTasks ?? 0}/${model.totalTasks} tasks`);
  if (model.actionCount) parts.push(`${model.actionCount} action${model.actionCount === 1 ? "" : "s"}`);
  if (model.attentionCount) parts.push(`${model.attentionCount} warning${model.attentionCount === 1 ? "" : "s"}`);
  if (model.artifactCount) parts.push(`${model.artifactCount} artifact${model.artifactCount === 1 ? "" : "s"}`);
  const duration = durationLabel(model.durationMs);
  if (duration) parts.push(duration);
  return parts;
}

function statusClass(status: ActivityLedgerStatus): string {
  return `status-${status}`;
}

function DefaultEntryDetails({ entry }: { entry: ActivityLedgerEntry }) {
  const hasPayload = Object.keys(entry.payload).length > 0;
  if (!entry.summary && !entry.outputs.length && !hasPayload && !entry.usageLabel) return null;
  return <div className="activity-ledger-entry-body">
    {entry.summary && entry.summary !== entry.label && <p>{entry.summary}</p>}
    {entry.outputs.map((output, index) => <div className="harness-output" key={`${output.label}-${index}`}><small>{output.label}</small><pre tabIndex={0}>{output.content}</pre></div>)}
    {hasPayload && <details className="activity-ledger-technical"><summary>Technical details</summary><pre tabIndex={0}>{JSON.stringify(entry.payload, null, 2)}</pre></details>}
    {entry.usageLabel && <small>{entry.usageLabel}</small>}
  </div>;
}

export function ActivityLedger({
  model,
  renderEntryDetails,
  renderEntryActions,
}: {
  model: ActivityLedgerViewModel;
  renderEntryDetails?: (entry: ActivityLedgerEntry) => ReactNode;
  renderEntryActions?: (entry: ActivityLedgerEntry) => ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const auditId = useId();
  const active = model.status === "active" || model.status === "queued" || model.status === "attention";
  const attentionEntries = model.entries.filter((entry) => entry.status === "attention" || entry.status === "failed");
  const receipt = receiptParts(model);
  return (
    <section className={`activity-ledger ${statusClass(model.status)}`} aria-label={model.title}>
      <header className="activity-ledger-header">
        <span className={`activity-ledger-state ${statusClass(model.status)}`} aria-hidden="true" />
        <strong>{model.title}</strong>
        <span>{activityLedgerStatusLabel(model.status)}{model.durationMs ? ` · ${durationLabel(model.durationMs)}` : ""}</span>
      </header>

      {active ? <div className="activity-ledger-live">
        {model.phases.length > 0 && <ol className="activity-ledger-phases" aria-label="Work phases">
          {model.phases.map((phase) => <li className={statusClass(phase.status)} key={phase.key}>
            <span className="activity-ledger-phase-marker" aria-hidden="true" />
            <span>{phase.label}</span>
            <small>{phase.total > 1 ? `${phase.completed} of ${phase.total}` : activityLedgerStatusLabel(phase.status)}</small>
          </li>)}
        </ol>}
        {model.currentAction && <p className="activity-ledger-current" aria-live="polite"><small>Now</small><span>{model.currentAction}</span></p>}
      </div> : <p className="activity-ledger-receipt">
        <strong>{activityLedgerStatusLabel(model.status)}</strong>
        {receipt.length > 0 && <span>{receipt.join(" · ")}</span>}
      </p>}

      {attentionEntries.length > 0 && <div className="activity-ledger-attention" aria-label="Activity requiring attention">
        {attentionEntries.map((entry) => <div className={statusClass(entry.status)} key={`attention:${entry.id}`}>
          <span className={`activity-ledger-phase-marker ${statusClass(entry.status)}`} aria-hidden="true" />
          <span><strong>{entry.label}</strong>{entry.brief && entry.brief !== entry.label && <small>{entry.brief}</small>}</span>
          <em>{entry.statusLabel ?? activityLedgerStatusLabel(entry.status)}</em>
        </div>)}
      </div>}

      <footer className="activity-ledger-footer">
        <span>{model.actionCount} action{model.actionCount === 1 ? "" : "s"}{model.attentionCount ? ` · ${model.attentionCount} need attention` : ""}</span>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={auditId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Hide activity" : "Show activity"}
          <ChevronDown size={14} aria-hidden="true" />
        </button>
      </footer>

      {expanded && <div className="activity-ledger-audit" id={auditId}>
        <header><strong>Activity</strong><small>Newest first</small></header>
        {model.entries.length > 0 ? <ol>
          {model.entries.map((entry) => {
            const details = renderEntryDetails?.(entry);
            const actions = renderEntryActions?.(entry);
            const hasDetails = Boolean(details || entry.summary || entry.outputs.length || Object.keys(entry.payload).length || entry.usageLabel || actions);
            return <li className={statusClass(entry.status)} key={entry.id}>
              <span className="activity-ledger-entry-time">{timeLabel(entry.occurredAt) ?? `#${entry.sequence}`}</span>
              <div className="activity-ledger-entry-content">
                {hasDetails ? <details>
                  <summary><strong>{entry.label}</strong><span>{entry.statusLabel ?? activityLedgerStatusLabel(entry.status)}</span></summary>
                  {details ?? <DefaultEntryDetails entry={entry} />}
                  {actions && <div className="activity-ledger-entry-actions">{actions}</div>}
                </details> : <div className="activity-ledger-entry-static"><strong>{entry.label}</strong><span>{entry.statusLabel ?? activityLedgerStatusLabel(entry.status)}</span></div>}
              </div>
            </li>;
          })}
        </ol> : <p>No activity has been recorded yet.</p>}
      </div>}
    </section>
  );
}
