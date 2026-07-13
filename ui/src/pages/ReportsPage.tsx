import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Archive, Download, FileText, LoaderCircle, Plus, Save, ShieldCheck, X } from "lucide-react";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

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
  const { api, createReport, engagement, findings, observations, previewMode, reports, updateReport } = useWorkspace();
  const [selectedId, setSelectedId] = useState("");
  const selected = reports.find((report) => report.id === selectedId);
  const [title, setTitle] = useState("");
  const [status, setStatus] = useState("draft");
  const [summary, setSummary] = useState("");
  const [findingIds, setFindingIds] = useState<string[]>([]);
  const [observationIds, setObservationIds] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [createSaving, setCreateSaving] = useState(false);
  const [pdfState, setPdfState] = useState<"idle" | "queued" | "rendering" | "downloading">("idle");
  const [bundleSaving, setBundleSaving] = useState(false);
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
      setDirty(false);
      return;
    }
    setTitle(selected.title);
    setStatus(selected.status);
    setSummary(selected.executiveSummary);
    setFindingIds(selected.findingIds);
    setObservationIds(selected.observationIds);
    setDirty(false);
    setError(undefined);
  }, [selected]);

  const linkedFindings = useMemo(() => findings.filter((finding) => findingIds.includes(finding.id)), [findingIds, findings]);
  const linkedObservations = useMemo(() => observations.filter((observation) => observationIds.includes(observation.id)), [observationIds, observations]);
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
        expectedRevision: selected.revision,
      });
      setDirty(false);
    } catch (saveError) {
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
      setError(pdfError instanceof Error ? pdfError.message : "Could not export the PDF.");
    } finally {
      setPdfState("idle");
    }
  };

  const exportBundle = async () => {
    if (!api || !engagement) return;
    const approved = await confirm({
      title: "Export sensitive engagement bundle?",
      message: "The bundle contains unredacted evidence, raw execution output, PDFs, and audit records. Store and share it as sensitive data. Scratch workspace files are excluded unless promoted.",
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
      setError(bundleError instanceof Error ? bundleError.message : "Could not export the engagement bundle.");
    } finally {
      setBundleSaving(false);
    }
  };

  return (
    <div className="page reports-page">
      <PageHeader eyebrow="Defensible deliverables" title="Reports" description="Compose executive narratives from persisted report data and selected findings." actions={<button className="button primary" type="button" disabled={previewMode || !engagement} onClick={() => void openCreate()}><Plus size={16} /> New report</button>} />
      {error && <div className="knowledge-status error" role="alert">{error}</div>}
      {!selected ? <section className="panel empty-state"><FileText size={28} /><strong>{previewMode ? "Core unavailable" : "No reports yet"}</strong><p>{previewMode ? "Connect Nebula Core to create and edit persisted reports." : "Create a draft report to begin composing an executive summary."}</p></section> : <div className="report-layout">
        <aside className="panel report-outline">
          <header><div><span>{reports.length} report{reports.length === 1 ? "" : "s"}</span><strong>{engagement?.name}</strong></div></header>
          <nav aria-label="Reports">{reports.map((report) => <button className={report.id === selected.id ? "active" : undefined} type="button" title={report.title} key={report.id} onClick={() => void selectReport(report.id)}><FileText size={15} /><span className="report-list-label">{report.title}<small>{report.status} · revision {report.revision}</small></span></button>)}</nav>
          <footer><span>Persisted by Core</span><strong>{reports.length}</strong></footer>
        </aside>
        <section className="panel report-editor" aria-readonly={readOnly || undefined}>
          <header className="report-editor-toolbar"><div><label>Status<select value={status} disabled={readOnly} onChange={(event) => setField(setStatus, event.target.value)}><option value="draft">Draft</option><option value="review">In review</option><option value="final" disabled>Final · signed</option></select></label></div><span>{readOnly ? `Final · read-only · revision ${selected.revision}` : saving ? "Saving…" : dirty ? "Unsaved changes" : `Saved · revision ${selected.revision}`}</span></header>
          <div className="report-form">{readOnly && <p className="provider-dialog-note" role="status">This final report is an immutable signed record. Export remains available; create a new draft to make changes.</p>}<label>Report title<input value={title} readOnly={readOnly} onChange={(event) => setField(setTitle, event.target.value)} /></label><label>Executive summary<textarea rows={14} value={summary} readOnly={readOnly} placeholder="Summarize scope, posture, material findings, and recommended next actions…" onChange={(event) => setField(setSummary, event.target.value)} /></label><fieldset disabled={readOnly}><legend>Included findings</legend>{findings.length ? findings.map((finding) => <label key={finding.id}><input type="checkbox" checked={findingIds.includes(finding.id)} onChange={(event) => setField(setFindingIds, event.target.checked ? [...findingIds, finding.id] : findingIds.filter((id) => id !== finding.id))} /><span><strong>{finding.title}</strong><small>{finding.severity} · {finding.status.replaceAll("_", " ")}</small></span></label>) : <p>No findings are available.</p>}</fieldset><fieldset disabled={readOnly}><legend>Selected notes</legend>{observations.length ? observations.map((observation) => <label key={observation.id}><input type="checkbox" checked={observationIds.includes(observation.id)} onChange={(event) => setField(setObservationIds, event.target.checked ? [...observationIds, observation.id] : observationIds.filter((id) => id !== observation.id))} /><span><strong>{observation.title}</strong><small>{observation.observationType.replaceAll("_", " ")} · {observation.evidenceIds.length} evidence link{observation.evidenceIds.length === 1 ? "" : "s"}</small></span></label>) : <p>No observations are available.</p>}</fieldset><footer><span>{linkedFindings.length} finding{linkedFindings.length === 1 ? "" : "s"} · {linkedObservations.length} note{linkedObservations.length === 1 ? "" : "s"}</span><button className="button primary" type="button" disabled={readOnly || !dirty || saving || !title.trim()} onClick={() => void save()}><Save size={15} /> {readOnly ? "Final report" : saving ? "Saving…" : "Save report"}</button></footer></div>
        </section>
        <aside className="panel report-review">
          <header><h2>Export</h2><p>{dirty ? "Save this revision before exporting. PDF output is always rendered from the persisted record." : `PDF output uses saved revision ${selected.revision}.`}</p></header>
          <ul><li className={summary.trim() ? "pass" : "warning"}><ShieldCheck size={16} /><span><strong>Executive summary</strong><small>{summary.trim() ? "Present" : "Needs content"}</small></span></li><li className={linkedFindings.length ? "pass" : "warning"}><ShieldCheck size={16} /><span><strong>Finding coverage</strong><small>{linkedFindings.length} included</small></span></li></ul>
          <div className="export-actions"><button className="button primary full" type="button" disabled={dirty || !api || pdfState !== "idle"} title={dirty ? "Save the report before exporting" : undefined} onClick={() => void exportPdf()}>{pdfState === "idle" ? <Download size={15} /> : <LoaderCircle className="spin" size={15} />} {pdfState === "idle" ? "Export PDF" : pdfState === "downloading" ? "Downloading…" : "Rendering PDF…"}</button><button className="button secondary full" type="button" disabled={!api || !engagement || bundleSaving} onClick={() => void exportBundle()}>{bundleSaving ? <LoaderCircle className="spin" size={15} /> : <Archive size={15} />} {bundleSaving ? "Exporting…" : "Export engagement bundle"}</button><p className="provider-dialog-note">The .nebula.zip bundle is a portable export, not a backup. It includes unredacted evidence and excludes scratch workspace files.</p></div>
        </aside>
      </div>}
      {creating && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="report-dialog-title" onSubmit={(event) => void create(event)}><header><div><small>Persisted deliverable</small><h2 id="report-dialog-title">New report</h2></div><button className="icon-button subtle" type="button" aria-label="Close report dialog" onClick={() => setCreating(false)}><X size={17} /></button></header><label>Title<input required autoFocus value={newTitle} placeholder={`${engagement?.name ?? "Engagement"} assessment`} onChange={(event) => setNewTitle(event.target.value)} /></label><p className="provider-dialog-note">Validated and confirmed findings are included initially and can be changed in the editor.</p>{error && <p className="form-error" role="alert">{error}</p>}<footer><button className="button secondary" type="button" onClick={() => setCreating(false)}>Cancel</button><button className="button primary" type="submit" disabled={createSaving || !newTitle.trim()}>{createSaving ? "Creating…" : "Create report"}</button></footer></form></div>}
    </div>
  );
}
