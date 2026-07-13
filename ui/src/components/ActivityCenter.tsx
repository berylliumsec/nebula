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
import { ModalSurface } from "./DialogSystem";

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
  const [selectedApprovalId, setSelectedApprovalId] = useState<string>();
  const { events, approvals, previewMode, resolveApproval, streamState } = useWorkspace();
  const selectedApproval = approvals.find((approval) => approval.id === selectedApprovalId);

  const decide = async (id: string, request: ApprovalDecisionRequest) => {
    setBusyId(id);
    setDecisionError(undefined);
    try {
      await resolveApproval(id, request);
      setEditingId(undefined);
      setSelectedApprovalId(undefined);
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
    <>
    <aside id="activity-center" className={`activity-center${open ? " open" : ""}`} aria-label="Activity inspector">
      <header className="activity-center-header">
        <div>
          <span className={`live-dot ${streamState}`} aria-hidden="true" />
          <strong>Activity</strong>
        </div>
        <button className="icon-button subtle" type="button" onClick={onClose} aria-label="Close activity center">
          <X size={17} aria-hidden="true" />
        </button>
      </header>

      <div className="segmented-control" role="tablist" aria-label="Activity inspector views">
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
                <div className="approval-actions">
                  <button
                    className="button secondary full"
                    type="button"
                    onClick={() => {
                      setDecisionError(undefined);
                      setEditingId(undefined);
                      setSelectedApprovalId(approval.id);
                    }}
                  >
                    Review exact request
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
    {selectedApproval && (
      <ModalSurface labelledBy="approval-review-title" className="approval-review-dialog" onClose={() => setSelectedApprovalId(undefined)}>
        <header className="approval-review-header">
          <div>
            <span className={`risk-badge ${selectedApproval.risk}`}>{selectedApproval.risk}</span>
            <h2 id="approval-review-title">Review {selectedApproval.toolName}</h2>
            <p>{selectedApproval.rationale}</p>
          </div>
          <button className="icon-button subtle" type="button" aria-label="Close approval review" onClick={() => setSelectedApprovalId(undefined)}><X size={17} /></button>
        </header>
        <div className="approval-review-body">
          {decisionError && <p className="activity-error" role="alert">{decisionError}</p>}
          <dl className="approval-review-facts">
            <div><dt>Agent</dt><dd>{selectedApproval.agentName}</dd></div>
            <div><dt>Target</dt><dd>{selectedApproval.target}</dd></div>
            {selectedApproval.credentialClass && <div><dt>Credential</dt><dd>{selectedApproval.credentialClass}</dd></div>}
            {selectedApproval.image && <div><dt>Image</dt><dd><code>{selectedApproval.image}</code></dd></div>}
            {selectedApproval.manifestDigest && <div><dt>Manifest</dt><dd><code>{selectedApproval.manifestDigest}</code></dd></div>}
          </dl>
          <section>
            <h3>Exact arguments</h3>
            {editingId === selectedApproval.id ? (
              <div className="approval-editor">
                <label htmlFor={`approval-arguments-${selectedApproval.id}`}>Edited arguments</label>
                <textarea id={`approval-arguments-${selectedApproval.id}`} rows={9} spellCheck={false} value={editedArguments} onChange={(event) => setEditedArguments(event.target.value)} />
                <div className="approval-editor-actions">
                  <button className="button quiet" type="button" onClick={() => setEditingId(undefined)}>Cancel edit</button>
                  <button className="button primary" type="button" disabled={busyId === selectedApproval.id || previewMode} onClick={() => void approveEdited(selectedApproval.id)}>Approve edited</button>
                </div>
              </div>
            ) : <pre>{JSON.stringify(selectedApproval.arguments, null, 2)}</pre>}
          </section>
          {selectedApproval.command && <section><h3>Command argv</h3><pre>{JSON.stringify(selectedApproval.command, null, 2)}</pre></section>}
          <section><h3>Expected effects</h3><p>{selectedApproval.expectedEffects}</p></section>
        </div>
        {editingId !== selectedApproval.id && <footer className="approval-review-actions">
          <button className="button danger-quiet" type="button" disabled={previewMode || busyId === selectedApproval.id} onClick={() => void decide(selectedApproval.id, { decision: "reject" })}><X size={15} /> Reject</button>
          <button className="button quiet" type="button" disabled={previewMode || busyId === selectedApproval.id} onClick={() => { setDecisionError(undefined); setEditingId(selectedApproval.id); setEditedArguments(JSON.stringify(selectedApproval.arguments, null, 2)); }}>Edit request</button>
          <span />
          <button className="button danger-quiet" type="button" disabled={previewMode || busyId === selectedApproval.id} onClick={() => void decide(selectedApproval.id, { decision: "stop" })}>Stop mission</button>
          <button className="button primary" type="button" disabled={previewMode || busyId === selectedApproval.id} onClick={() => void decide(selectedApproval.id, { decision: "approve" })}><Check size={15} /> Approve once</button>
        </footer>}
      </ModalSurface>
    )}
    </>
  );
}
