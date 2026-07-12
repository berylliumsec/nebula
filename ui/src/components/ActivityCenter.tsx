import { useState } from "react";
import {
  Activity,
  Bot,
  Check,
  ChevronRight,
  Clock3,
  FileCheck2,
  ShieldAlert,
  TerminalSquare,
  X,
} from "lucide-react";
import type { ApprovalDecision, RunEventKind } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";

type CenterTab = "activity" | "approvals";

const eventIcons: Partial<Record<RunEventKind, typeof Activity>> = {
  "approval.requested": ShieldAlert,
  "finding.updated": FileCheck2,
  "tool.completed": TerminalSquare,
  "tool.failed": X,
  "agent.message": Bot,
};

function shortTime(value: string): string {
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

interface ActivityCenterProps {
  open: boolean;
  onClose: () => void;
}

export function ActivityCenter({ open, onClose }: ActivityCenterProps) {
  const [tab, setTab] = useState<CenterTab>("activity");
  const [busyId, setBusyId] = useState<string>();
  const { events, approvals, previewMode, resolveApproval, streamState } = useWorkspace();

  const decide = async (id: string, decision: ApprovalDecision) => {
    setBusyId(id);
    try {
      await resolveApproval(id, decision);
    } finally {
      setBusyId(undefined);
    }
  };

  return (
    <aside id="activity-center" className={`activity-center${open ? " open" : ""}`} aria-label="Activity center">
      <header className="activity-center-header">
        <div>
          <span className={`live-dot ${streamState}`} aria-hidden="true" />
          <strong>Activity center</strong>
        </div>
        <button className="icon-button subtle" type="button" onClick={onClose} aria-label="Close activity center">
          <X size={17} aria-hidden="true" />
        </button>
      </header>

      <div className="segmented-control" role="tablist" aria-label="Activity center views">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "activity"}
          onClick={() => setTab("activity")}
        >
          Activity
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "approvals"}
          onClick={() => setTab("approvals")}
        >
          Approvals
          {approvals.length > 0 && <span className="count-badge">{approvals.length}</span>}
        </button>
      </div>

      {previewMode && (
        <p className="preview-notice">Preview data is shown while Nebula Core is unavailable.</p>
      )}

      <div className="activity-scroll" role="tabpanel">
        {tab === "activity" ? (
          <ol className="event-list">
            {events.map((event) => {
              const Icon = eventIcons[event.kind] ?? Activity;
              return (
                <li key={event.id}>
                  <span className={`event-icon ${event.kind.split(".")[0]}`}>
                    <Icon size={15} aria-hidden="true" />
                  </span>
                  <div>
                    <p>{event.summary}</p>
                    <small>
                      {event.actor ?? "Nebula"} · {shortTime(event.occurredAt)} · #{event.sequence}
                    </small>
                  </div>
                  <ChevronRight size={14} aria-hidden="true" />
                </li>
              );
            })}
            {events.length === 0 && (
              <li className="empty-row">Run events will appear here as work is persisted.</li>
            )}
          </ol>
        ) : (
          <div className="approval-list">
            {approvals.map((approval) => (
              <article className="approval-card" key={approval.id}>
                <div className="approval-card-heading">
                  <span className={`risk-badge ${approval.risk}`}>{approval.risk}</span>
                  <span className="approval-time">
                    <Clock3 size={13} aria-hidden="true" /> waiting
                  </span>
                </div>
                <h3>{approval.toolName}</h3>
                <dl className="approval-facts">
                  <div>
                    <dt>Agent</dt>
                    <dd>{approval.agentName}</dd>
                  </div>
                  <div>
                    <dt>Target</dt>
                    <dd>{approval.target}</dd>
                  </div>
                </dl>
                <p>{approval.rationale}</p>
                <details>
                  <summary>Exact arguments and effects</summary>
                  <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
                  <p>{approval.expectedEffects}</p>
                </details>
                <div className="approval-actions">
                  <button
                    className="button danger-quiet"
                    type="button"
                    disabled={busyId === approval.id}
                    onClick={() => void decide(approval.id, "reject")}
                  >
                    <X size={15} aria-hidden="true" /> Reject
                  </button>
                  <button
                    className="button primary"
                    type="button"
                    disabled={busyId === approval.id}
                    onClick={() => void decide(approval.id, "approve")}
                  >
                    <Check size={15} aria-hidden="true" /> Approve once
                  </button>
                </div>
              </article>
            ))}
            {approvals.length === 0 && (
              <div className="empty-state compact">
                <Check size={22} aria-hidden="true" />
                <strong>No pending approvals</strong>
                <p>Blocked mission steps will appear here with their exact scope and effects.</p>
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
