import { useState } from "react";
import {
  Activity,
  Bot,
  Check,
  Clock3,
  FileCheck2,
  ShieldAlert,
  TerminalSquare,
  X,
} from "lucide-react";
import type { ApprovalDecisionRequest, RunEventKind } from "../api/types";
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
  const [decisionError, setDecisionError] = useState<string>();
  const [editingId, setEditingId] = useState<string>();
  const [editedArguments, setEditedArguments] = useState("");
  const { events, approvals, previewMode, resolveApproval, streamState } = useWorkspace();

  const decide = async (id: string, request: ApprovalDecisionRequest) => {
    setBusyId(id);
    setDecisionError(undefined);
    try {
      await resolveApproval(id, request);
      setEditingId(undefined);
    } catch (error) {
      setDecisionError(error instanceof Error ? error.message : "Could not record the approval decision.");
    } finally {
      setBusyId(undefined);
    }
  };

  const approveEdited = async (id: string) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(editedArguments);
    } catch {
      setDecisionError("Edited arguments must be valid JSON.");
      return;
    }
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      setDecisionError("Edited arguments must be one JSON object.");
      return;
    }
    await decide(id, {
      decision: "approve",
      editedArguments: parsed as Record<string, unknown>,
    });
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
                </li>
              );
            })}
            {events.length === 0 && (
              <li className="empty-row">Run events will appear here as work is persisted.</li>
            )}
          </ol>
        ) : (
          <div className="approval-list">
            {decisionError && <p className="activity-error" role="alert">{decisionError}</p>}
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
                  {approval.credentialClass && <div>
                    <dt>Credential</dt>
                    <dd>{approval.credentialClass}</dd>
                  </div>}
                </dl>
                <p>{approval.rationale}</p>
                <details>
                  <summary>Exact arguments and effects</summary>
                  <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
                  {approval.command && <><strong>Command argv</strong><pre>{JSON.stringify(approval.command, null, 2)}</pre></>}
                  {approval.image && <p><strong>Image:</strong> <code>{approval.image}</code></p>}
                  {approval.manifestDigest && <p><strong>Manifest:</strong> <code>{approval.manifestDigest}</code></p>}
                  <p>{approval.expectedEffects}</p>
                </details>
                {editingId === approval.id && <div className="approval-editor">
                  <label htmlFor={`approval-arguments-${approval.id}`}>Edited arguments</label>
                  <textarea
                    id={`approval-arguments-${approval.id}`}
                    rows={7}
                    spellCheck={false}
                    value={editedArguments}
                    onChange={(event) => setEditedArguments(event.target.value)}
                  />
                  <div className="approval-editor-actions">
                    <button className="button quiet" type="button" onClick={() => setEditingId(undefined)}>Cancel edit</button>
                    <button className="button primary" type="button" disabled={busyId === approval.id} onClick={() => void approveEdited(approval.id)}>Approve edited</button>
                  </div>
                </div>}
                <div className="approval-actions">
                  <button
                    className="button danger-quiet"
                    type="button"
                    disabled={previewMode || busyId === approval.id}
                    onClick={() => void decide(approval.id, { decision: "reject" })}
                  >
                    <X size={15} aria-hidden="true" /> Reject
                  </button>
                  <button
                    className="button quiet"
                    type="button"
                    disabled={previewMode || busyId === approval.id}
                    onClick={() => {
                      setDecisionError(undefined);
                      setEditingId(approval.id);
                      setEditedArguments(JSON.stringify(approval.arguments, null, 2));
                    }}
                  >
                    Edit
                  </button>
                  <button
                    className="button primary"
                    type="button"
                    disabled={previewMode || busyId === approval.id}
                    onClick={() => void decide(approval.id, { decision: "approve" })}
                  >
                    <Check size={15} aria-hidden="true" /> Approve once
                  </button>
                  <button
                    className="button danger-quiet"
                    type="button"
                    disabled={previewMode || busyId === approval.id}
                    onClick={() => void decide(approval.id, { decision: "stop" })}
                  >
                    Stop mission
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
