import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Archive, BadgeCheck, Download, FileText, LoaderCircle, Plus, RotateCcw, Save, ShieldCheck, Sparkles, X } from "lucide-react";
import type { AIWritingProvenance, ReportNoteTransform } from "../api/types";
import { AIWritingDialog } from "../components/AIWritingDialog";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

function safeFilename(value: string): string {
  return value.trim().replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "nebula-report";
}

function downloadBlob(filename: string, content: Blob): void {
  const url = URL.createObjectURL(content);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function ReportsPage() {
  const confirm = useConfirmation();
  const {
    activeOperator,
    api,
    createOperatorProfile,
    createReport,
    engagement,
    findings,
    observations,
    providers,
    reports,
    signOffReport,
    updateReport,
  } = useWorkspace();
  const [selectedId, setSelectedId] = useState("");
  const selected = reports.find((report) => report.id === selectedId);
  const [title, setTitle] = useState("");
  const [status, setStatus] = useState("draft");
  const [summary, setSummary] = useState("");
  const [findingIds, setFindingIds] = useState<string[]>([]);
  const [observationIds, setObservationIds] = useState<string[]>([]);
  const [noteTransforms, setNoteTransforms] = useState<ReportNoteTransform[]>([]);
  const [summaryProvenance, setSummaryProvenance] = useState<AIWritingProvenance>();
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [createSaving, setCreateSaving] = useState(false);
  const [pdfState, setPdfState] = useState<"idle" | "queued" | "rendering" | "downloading">("idle");
  const [bundleSaving, setBundleSaving] = useState(false);
  const [signoffOpen, setSignoffOpen] = useState(false);
  const [signoffName, setSignoffName] = useState("");
  const [attestation, setAttestation] = useState("I reviewed this report and approve it as the final record.");
  const [signing, setSigning] = useState(false);
  const [writingTarget, setWritingTarget] = useState<{ kind: "summary" } | { kind: "note"; observationId: string }>();
  const readOnly = selected?.status === "final";

  useEffect(() => {
    if (selectedId && reports.some((report) => report.id === selectedId)) return;
    setSelectedId(reports[0]?.id ?? "");
  }, [reports, selectedId]);

  useEffect(() => {
    if (!selected) {
      setTitle("");
      setSummary("");
      setFindingIds([]);
      setObservationIds([]);
      setNoteTransforms([]);
      setSummaryProvenance(undefined);
      setDirty(false);
      return;
    }
    setTitle(selected.title);
    setStatus(selected.status);
    setSummary(selected.executiveSummary);
    setFindingIds(selected.findingIds);
    setObservationIds(selected.observationIds);
    setNoteTransforms(selected.noteTransforms);
    setSummaryProvenance(selected.executiveSummaryProvenance);
    setDirty(false);
    setError(undefined);
  }, [selected]);

  const linkedFindings = useMemo(() => findings.filter((finding) => findingIds.includes(finding.id)), [findingIds, findings]);
  const linkedObservations = useMemo(() => observations.filter((observation) => observationIds.includes(observation.id)), [observationIds, observations]);
  const writingObservation = writingTarget?.kind === "note"
    ? observations.find((observation) => observation.id === writingTarget.observationId)
    : undefined;
  const reportWritingSource = useMemo(() => {
    const noteSections = linkedObservations.map((observation) => {
      const transform = noteTransforms.find((item) => item.observationId === observation.id);
      return {
        title: transform?.title ?? observation.title,
        body: transform?.body ?? observation.body,
        source: observation.observationType,
      };
    });
    return JSON.stringify({
      engagement: engagement?.name,
      report_title: title,
      existing_executive_summary: summary,
      selected_findings: linkedFindings.map((finding) => ({
        title: finding.title,
        severity: finding.severity,
        status: finding.status,
        description: finding.description,
        evidence_count: finding.evidenceIds.length,
      })),
      selected_note_sections: noteSections,
    }, null, 2).slice(0, 100_000);
  }, [engagement?.name, linkedFindings, linkedObservations, noteTransforms, summary, title]);
  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!engagement) return;
    setCreateSaving(true);
    setError(undefined);
    try {
      const report = await createReport({
        engagementId: engagement.id,
        title: newTitle.trim(),
        findingIds: findings.filter((finding) => ["validated", "confirmed"].includes(finding.status)).map((finding) => finding.id),
      });
      setSelectedId(report.id);
      setNewTitle("");
      setCreating(false);
    } catch (createError) {
      void logCaughtDiagnostic("interface.reports_page.caught_failure_01", "A handled interface operation failed.", createError, "reports_page");
      setError(createError instanceof Error ? createError.message : "Could not create the report.");
    } finally {
      setCreateSaving(false);
    }
  };

  const save = async () => {
    if (!selected || selected.status === "final" || !title.trim()) return;
    setSaving(true);
    setError(undefined);
    try {
      await updateReport(selected.id, {
        title: title.trim(),
        status,
        executiveSummary: summary,
        findingIds,
        observationIds,
        noteTransforms,
        executiveSummaryProvenance: summaryProvenance ?? null,
        expectedRevision: selected.revision,
      });
      setDirty(false);
    } catch (saveError) {
      void logCaughtDiagnostic("interface.reports_page.caught_failure_02", "A handled interface operation failed.", saveError, "reports_page");
      setError(saveError instanceof Error ? saveError.message : "Could not save the report.");
    } finally {
      setSaving(false);
    }
  };

  const setField = <T,>(setter: (value: T) => void, value: T) => {
    if (readOnly) return;
    setter(value);
    setDirty(true);
  };

  const setIncludedObservation = (observationId: string, included: boolean) => {
    if (readOnly) return;
    setObservationIds(included
      ? [...observationIds, observationId]
      : observationIds.filter((id) => id !== observationId));
    if (!included) {
      setNoteTransforms((current) => current.filter((item) => item.observationId !== observationId));
    }
    setDirty(true);
  };

  const allowDiscard = async () => !dirty || confirm({
    title: "Discard unsaved changes?",
    message: "Changes to this report have not been persisted and cannot be recovered.",
    confirmLabel: "Discard changes",
    tone: "danger",
  });
  const selectReport = async (id: string) => {
    if (id !== selectedId && await allowDiscard()) setSelectedId(id);
  };
  const openCreate = async () => {
    if (!await allowDiscard()) return;
    setError(undefined);
    setCreating(true);
  };

  const exportPdf = async () => {
    if (!api || !selected || dirty) return;
    setError(undefined);
    setPdfState("queued");
    try {
      let render = await api.renderReport(selected.id, selected.revision);
      for (let attempt = 0; attempt < 150 && (render.status === "queued" || render.status === "rendering"); attempt += 1) {
        setPdfState(render.status === "queued" ? "queued" : "rendering");
        await new Promise((resolve) => setTimeout(resolve, 200));
        render = await api.getReportRender(render.id);
      }
      if (render.status !== "completed") {
        throw new Error(render.errorDetail ?? `PDF render ended with status ${render.status}.`);
      }
      setPdfState("downloading");
      downloadBlob(`${safeFilename(selected.title)}.pdf`, await api.downloadReportPdf(render.id));
      if (render.warnings.length) setError(render.warnings.join(" "));
    } catch (pdfError) {
      void logCaughtDiagnostic("interface.reports_page.caught_failure_03", "A handled interface operation failed.", pdfError, "reports_page");
      setError(pdfError instanceof Error ? pdfError.message : "Could not export the PDF.");
    } finally {
      setPdfState("idle");
    }
  };

  const exportBundle = async () => {
    if (!api || !engagement) return;
    const approved = await confirm({
      title: "Export sensitive engagement bundle?",
      message: "The bundle contains unredacted evidence, raw execution output, retained results from selected terminal security tools, metadata-only terminal records, PDFs, and audit records. Store and share it as sensitive data. Scratch workspace files are excluded unless promoted.",
      confirmLabel: "Export bundle",
      tone: "danger",
    });
    if (!approved) return;
    setBundleSaving(true);
    setError(undefined);
    try {
      const bundle = await api.exportEngagementBundle(engagement.id);
      downloadBlob(`${safeFilename(engagement.name)}.nebula.zip`, bundle);
    } catch (bundleError) {
      void logCaughtDiagnostic("interface.reports_page.caught_failure_04", "A handled interface operation failed.", bundleError, "reports_page");
      setError(bundleError instanceof Error ? bundleError.message : "Could not export the engagement bundle.");
    } finally {
      setBundleSaving(false);
    }
  };

  const openSignoff = () => {
    if (!selected || selected.status !== "review" || dirty) return;
    setSignoffName(activeOperator?.displayName ?? "");
    setAttestation("I reviewed this report and approve it as the final record.");
    setError(undefined);
    setSignoffOpen(true);
  };

  const completeSignoff = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected || selected.status !== "review") return;
    setSigning(true);
    setError(undefined);
    try {
      const operator = activeOperator ?? await createOperatorProfile({ displayName: signoffName.trim() });
      await signOffReport(selected.id, selected.revision, operator.id, attestation.trim());
      setSignoffOpen(false);
    } catch (signoffError) {
      void logCaughtDiagnostic("interface.reports_page.caught_failure_05", "A handled interface operation failed.", signoffError, "reports_page");
      setError(signoffError instanceof Error ? signoffError.message : "Could not sign off the report.");
    } finally {
      setSigning(false);
    }
  };

  return (
    <div className="page reports-page">
      <PageHeader title="Reports" description="Build reports from verified findings and evidence." actions={<button className="button primary" type="button" disabled={!engagement} onClick={() => void openCreate()}><Plus size={16} /> New report</button>} />
      {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." />}
      {!selected ? <section className="panel empty-state report-empty-state"><FileText size={28} /><strong>Create your first report</strong><p>Turn verified findings and evidence into a reviewable deliverable.</p><button className="button primary" type="button" disabled={!engagement} onClick={() => void openCreate()}><Plus size={15} /> New report</button></section> : <div className="report-layout">
        <aside className="panel report-outline">
          <header><div><span>{reports.length} report{reports.length === 1 ? "" : "s"}</span><strong>{engagement?.name}</strong></div></header>
          <nav aria-label="Reports">{reports.map((report) => <button className={report.id === selected.id ? "active" : undefined} type="button" title={report.title} key={report.id} onClick={() => void selectReport(report.id)}><FileText size={15} /><span className="report-list-label">{report.title}<small>{report.status} · revision {report.revision}</small></span></button>)}</nav>
          <footer><span>Persisted by Core</span><strong>{reports.length}</strong></footer>
        </aside>
        <section className="panel report-editor" aria-readonly={readOnly || undefined}>
          <header className="report-editor-toolbar"><div><label>Status<select value={status} disabled={readOnly} onChange={(event) => setField(setStatus, event.target.value)}><option value="draft">Draft</option><option value="review">In review</option><option value="final" disabled>Final · signed</option></select></label></div><span>{readOnly ? `Final · read-only · revision ${selected.revision}` : saving ? "Saving…" : dirty ? "Unsaved changes" : `Saved · revision ${selected.revision}`}</span></header>
          <div className="report-form">
            {readOnly && <p className="provider-dialog-note" role="status">This final report is an immutable signed record. Export remains available; create a new draft to make changes.</p>}
            <label>Report title<input value={title} readOnly={readOnly} onChange={(event) => setField(setTitle, event.target.value)} /></label>
            <label>
              <span className="report-field-heading"><span>Executive summary</span>{!readOnly && <button className="button quiet" type="button" disabled={!api || !providers.some((provider) => provider.enabled && provider.models.length)} onClick={() => setWritingTarget({ kind: "summary" })}><Sparkles size={14} /> Draft with AI</button>}</span>
              <textarea rows={14} value={summary} readOnly={readOnly} placeholder="Summarize scope, posture, material findings, and recommended next actions…" onChange={(event) => {
                if (readOnly) return;
                setSummary(event.target.value);
                setSummaryProvenance(undefined);
                setDirty(true);
              }} />
              {summaryProvenance && <small>AI-assisted draft · {summaryProvenance.model} · operator editable</small>}
            </label>
            <fieldset disabled={readOnly}><legend>Included findings</legend>{findings.length ? findings.map((finding) => <label key={finding.id}><input type="checkbox" checked={findingIds.includes(finding.id)} onChange={(event) => setField(setFindingIds, event.target.checked ? [...findingIds, finding.id] : findingIds.filter((id) => id !== finding.id))} /><span><strong>{finding.title}</strong><small>{finding.severity} · {finding.status.replaceAll("_", " ")}</small></span></label>) : <p>No findings are available.</p>}</fieldset>
            <fieldset disabled={readOnly}><legend>Note sections</legend>{observations.length ? observations.map((observation) => {
              const included = observationIds.includes(observation.id);
              const transform = noteTransforms.find((item) => item.observationId === observation.id);
              return <div key={observation.id}>
                <label className="report-note-option"><input type="checkbox" checked={included} onChange={(event) => setIncludedObservation(observation.id, event.target.checked)} /><span><strong>{observation.title}</strong><small>{observation.observationType.replaceAll("_", " ")} · {observation.evidenceIds.length} evidence link{observation.evidenceIds.length === 1 ? "" : "s"}</small></span>{included && !readOnly && <button className="button quiet" type="button" onClick={(event) => { event.preventDefault(); setWritingTarget({ kind: "note", observationId: observation.id }); }}><Sparkles size={13} /> {transform ? "Transform again" : "Transform with AI"}</button>}</label>
                {included && transform && <section className="report-note-transform"><header><span>AI-assisted section · source revision {transform.sourceRevision}{transform.sourceRevision !== observation.revision ? " · source note changed" : ""}</span>{!readOnly && <button className="button quiet" type="button" onClick={() => { setNoteTransforms((current) => current.filter((item) => item.observationId !== observation.id)); setDirty(true); }}><RotateCcw size={13} /> Use original note</button>}</header><textarea aria-label={`Report section for ${observation.title}`} readOnly={readOnly} value={transform.body} onChange={(event) => { setNoteTransforms((current) => current.map((item) => item.observationId === observation.id ? { ...item, body: event.target.value } : item)); setDirty(true); }} /></section>}
              </div>;
            }) : <p>No project notes are available. Capture notes from selected text or create one in Workbench.</p>}</fieldset>
            <footer><span>{linkedFindings.length} finding{linkedFindings.length === 1 ? "" : "s"} · {linkedObservations.length} note section{linkedObservations.length === 1 ? "" : "s"}</span><button className="button primary" type="button" disabled={readOnly || !dirty || saving || !title.trim()} onClick={() => void save()}><Save size={15} /> {readOnly ? "Final report" : saving ? "Saving…" : "Save report"}</button></footer>
          </div>
        </section>
        <aside className="panel report-review">
          <header><h2>{selected.status === "review" ? "Sign off & export" : "Export"}</h2><p>{dirty ? "Save this revision before exporting. PDF output is always rendered from the persisted record." : `PDF output uses saved revision ${selected.revision}.`}</p></header>
          <ul><li className={summary.trim() ? "pass" : "warning"}><ShieldCheck size={16} /><span><strong>Executive summary</strong><small>{summary.trim() ? "Present" : "Needs content"}</small></span></li><li className={linkedFindings.length ? "pass" : "warning"}><ShieldCheck size={16} /><span><strong>Finding coverage</strong><small>{linkedFindings.length} included</small></span></li></ul>
          <div className="export-actions">{selected.status === "review" && <button className="button primary full" type="button" disabled={dirty || signing} title={dirty ? "Save the report before sign-off" : undefined} onClick={openSignoff}><BadgeCheck size={15} /> Sign off final report</button>}<button className={`button ${selected.status === "review" ? "secondary" : "primary"} full`} type="button" disabled={dirty || !api || pdfState !== "idle"} title={dirty ? "Save the report before exporting" : undefined} onClick={() => void exportPdf()}>{pdfState === "idle" ? <Download size={15} /> : <LoaderCircle className="spin" size={15} />} {pdfState === "idle" ? "Export PDF" : pdfState === "downloading" ? "Downloading…" : "Rendering PDF…"}</button><button className="button secondary full" type="button" disabled={!api || !engagement || bundleSaving} onClick={() => void exportBundle()}>{bundleSaving ? <LoaderCircle className="spin" size={15} /> : <Archive size={15} />} {bundleSaving ? "Exporting…" : "Export engagement bundle"}</button><p className="provider-dialog-note">The .nebula.zip bundle is a portable sensitive export, not a backup. It includes unredacted evidence, retained selected-tool terminal results, and metadata-only terminal records; scratch workspace files are excluded.</p></div>
        </aside>
      </div>}
      {creating && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="report-dialog-title" onSubmit={(event) => void create(event)}><header><div><small>Persisted deliverable</small><h2 id="report-dialog-title">New report</h2></div><button className="icon-button subtle" type="button" aria-label="Close report dialog" onClick={() => setCreating(false)}><X size={17} /></button></header><label>Title<input required autoFocus value={newTitle} placeholder={`${engagement?.name ?? "Engagement"} assessment`} onChange={(event) => setNewTitle(event.target.value)} /></label><p className="provider-dialog-note">Validated and confirmed findings are included initially and can be changed in the editor.</p>{error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}<footer><button className="button secondary" type="button" onClick={() => setCreating(false)}>Cancel</button><button className="button primary" type="submit" disabled={createSaving || !newTitle.trim()}>{createSaving ? "Creating…" : "Create report"}</button></footer></form></div>}
      {signoffOpen && selected && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="report-signoff-title" onSubmit={(event) => void completeSignoff(event)}><header><div><small>Revision {selected.revision} · permanent attribution</small><h2 id="report-signoff-title">Sign off final report</h2></div><button className="icon-button subtle" type="button" aria-label="Close report sign-off" disabled={signing} onClick={() => setSignoffOpen(false)}><X size={17} /></button></header><p className="provider-dialog-note">Sign-off finalizes this saved revision and makes it read-only. Included findings must already be validated.</p>{!activeOperator && <label>Your display name<input required autoFocus maxLength={200} value={signoffName} placeholder="Name shown in report attribution" onChange={(event) => setSignoffName(event.target.value)} /></label>}{activeOperator && <label>Signing as<input value={activeOperator.displayName} readOnly aria-readonly="true" /></label>}<label>Attestation<textarea required rows={4} maxLength={2000} value={attestation} onChange={(event) => setAttestation(event.target.value)} /></label>{error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}<footer><button className="button secondary" type="button" disabled={signing} onClick={() => setSignoffOpen(false)}>Cancel</button><button className="button primary" type="submit" disabled={signing || !attestation.trim() || (!activeOperator && !signoffName.trim())}>{signing ? <><LoaderCircle className="spin" size={15} /> Signing…</> : <><BadgeCheck size={15} /> Sign off report</>}</button></footer></form></div>}
      {writingTarget && api && engagement && (writingTarget.kind === "summary" || writingObservation) && <AIWritingDialog
        api={api}
        engagementId={engagement.id}
        providers={providers}
        purpose={writingTarget.kind === "summary" ? "report_summary" : "report_section"}
        title={writingTarget.kind === "summary" ? "Draft executive summary with AI" : "Transform note into a report section"}
        description={writingTarget.kind === "summary"
          ? "Nebula will draft from the report's selected findings and note sections. Review and edit the result before saving the report."
          : "Tell Nebula how this project note should read in the report. The original note remains unchanged."}
        sourceLabel={writingTarget.kind === "summary" ? `${selected?.title ?? title} report context` : writingObservation?.title ?? "Project note"}
        sourceText={writingTarget.kind === "summary" ? reportWritingSource : (writingObservation?.body ?? "").slice(0, 100_000)}
        initialInstruction={writingTarget.kind === "summary"
          ? "Draft a concise executive summary covering scope, overall posture, material verified findings, and prioritized next actions. Do not present working notes as confirmed findings."
          : "Rewrite this note as a concise report section for a technical stakeholder. Preserve concrete facts and clearly label uncertainty."}
        onClose={() => setWritingTarget(undefined)}
        onApply={(result) => {
          if (writingTarget.kind === "summary") {
            setSummary(result.content);
            setSummaryProvenance(result.provenance);
          } else if (writingObservation) {
            const next: ReportNoteTransform = {
              observationId: writingObservation.id,
              sourceRevision: writingObservation.revision,
              title: writingObservation.title,
              body: result.content,
              provenance: result.provenance,
            };
            setNoteTransforms((current) => [next, ...current.filter((item) => item.observationId !== writingObservation.id)]);
          }
          setDirty(true);
          setWritingTarget(undefined);
        }}
      />}
    </div>
  );
}
