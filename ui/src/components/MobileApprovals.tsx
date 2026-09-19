import { useState } from "react";
import { Check, ShieldAlert, X } from "lucide-react";
import type { ApprovalDecision, ApprovalSummary } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";

function exactRequest(approval: ApprovalSummary): string {
  if (approval.command?.length) return approval.command.map((part) => /^[\w@%+=:,./-]+$/.test(part) ? part : JSON.stringify(part)).join(" ");
  return JSON.stringify(approval.arguments, null, 2);
}

/** Pending approvals, shown first on the phone Activity tab. Never collapsed. */
export function MobileApprovals() {
  const { approvals, previewMode, resolveApproval } = useWorkspace();
  const [busyId, setBusyId] = useState<string>();
  const [error, setError] = useState<string>();
  if (!approvals.length) return null;
  const decide = async (id: string, decision: ApprovalDecision) => {
    setBusyId(id);
    setError(undefined);
    try {
      await resolveApproval(id, { decision });
    } catch (caught) {
      void logCaughtDiagnostic("interface.mobile_approvals.decision_failed", "A handled interface operation failed.", caught, "mobile_approvals");
      setError(caught instanceof Error ? caught.message : "The approval decision could not be saved.");
    } finally {
      setBusyId(undefined);
    }
  };
  return <section className="mobile-approvals" aria-labelledby="mobile-approvals-title">
    <h2 id="mobile-approvals-title">Needs you</h2>
    {error && <DiagnosticErrorNotice error={error} fallback="The approval decision could not be saved." compact />}
    {approvals.map((approval) => <article className="mobile-approval-card" key={approval.id} aria-label={`Approval for ${approval.toolName}`}>
      <header>
        <span className="mobile-approval-icon"><ShieldAlert size={18} aria-hidden="true" /></span>
        <div><strong>{approval.toolName}</strong><small>{approval.agentName} · {approval.target}</small></div>
        <span className={`risk-badge ${approval.risk}`}>{approval.risk}</span>
      </header>
      <pre aria-label="Exact request">{exactRequest(approval)}</pre>
      {approval.expectedEffects && <p>{approval.expectedEffects}</p>}
      <footer>
        <button className="button secondary" type="button" disabled={previewMode || busyId === approval.id} onClick={() => void decide(approval.id, "reject")}><X size={16} aria-hidden="true" /> Reject</button>
        <button className="button primary" type="button" disabled={previewMode || busyId === approval.id} onClick={() => void decide(approval.id, "approve")}><Check size={16} aria-hidden="true" /> Approve once</button>
      </footer>
    </article>)}
  </section>;
}
