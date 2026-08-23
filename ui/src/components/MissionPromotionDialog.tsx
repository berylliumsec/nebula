import { useEffect, useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { FilePlus2, ShieldAlert, X } from "lucide-react";
import type { AgentRunSummary, FindingSummary } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";
import { ModalSurface } from "./DialogSystem";

export function MissionPromotionDialog({ run, summary }: { run: AgentRunSummary; summary: string }) {
  const { createFinding, createReport, engagement } = useWorkspace();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"finding" | "report">("finding");
  const [title, setTitle] = useState(run.title);
  const [body, setBody] = useState(summary);
  const [severity, setSeverity] = useState<FindingSummary["severity"]>("info");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!open) return;
    setTitle(run.title);
    setBody(summary);
    setError(undefined);
  }, [open, run.title, summary]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!engagement || !title.trim() || !body.trim()) return;
    setSaving(true);
    setError(undefined);
    try {
      if (kind === "finding") {
        await createFinding({
          engagementId: engagement.id,
          title: title.trim(),
          description: body.trim(),
          severity,
          severityRationale: "Operator-created candidate from a reviewed Mission result.",
          sourceRunId: run.id,
        });
      } else {
        await createReport({
          engagementId: engagement.id,
          title: title.trim(),
          status: "draft",
          executiveSummary: body.trim(),
          sourceRunId: run.id,
        });
      }
      setOpen(false);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.mission_promotion.failed", "A reviewed Mission result could not be promoted.", caughtError, "missions");
      setError(caughtError instanceof Error ? caughtError.message : "Could not create the draft.");
    } finally {
      setSaving(false);
    }
  };

  return <>
    <button className="button secondary" type="button" onClick={() => setOpen(true)}><FilePlus2 size={15} /> Create reviewed draft</button>
    {open && createPortal(<ModalSurface as="form" noValidate className="provider-dialog resource-dialog mission-promotion-dialog" labelledBy="mission-promotion-title" onClose={() => { if (!saving) setOpen(false); }} onSubmit={(event) => void submit(event)}>
      <header><div><small>Operator-reviewed promotion</small><h2 id="mission-promotion-title">Create from Mission result</h2></div><button className="icon-button subtle" type="button" aria-label="Close promotion dialog" onClick={() => setOpen(false)}><X size={17} /></button></header>
      <div className="segmented-control" role="group" aria-label="Draft type"><button type="button" aria-pressed={kind === "finding"} onClick={() => setKind("finding")}><ShieldAlert size={14} /> Finding candidate</button><button type="button" aria-pressed={kind === "report"} onClick={() => setKind("report")}><FilePlus2 size={14} /> Report draft</button></div>
      <p className="provider-dialog-note">Review and edit the Mission output. Nebula will preserve the source Mission ID; it will not mark a finding confirmed or sign off a report.</p>
      <label>Title<input required autoFocus maxLength={300} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      {kind === "finding" && <label>Initial severity<select value={severity} onChange={(event) => setSeverity(event.target.value as FindingSummary["severity"])}><option value="info">Informational</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="critical">Critical</option></select></label>}
      <label>{kind === "finding" ? "Candidate description" : "Executive summary draft"}<textarea required rows={12} value={body} onChange={(event) => setBody(event.target.value)} /></label>
      {error && <DiagnosticErrorNotice error={error} fallback="The draft could not be created." compact />}
      <footer><button className="button secondary" type="button" onClick={() => setOpen(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || !title.trim() || !body.trim()}>{saving ? "Creating…" : kind === "finding" ? "Create candidate" : "Create report draft"}</button></footer>
    </ModalSurface>, document.body)}
  </>;
}
