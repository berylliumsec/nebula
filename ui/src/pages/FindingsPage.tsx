import { useMemo, useState, type FormEvent } from "react";
import { Bug, CheckCircle2, FilePlus2, Link2, LoaderCircle, MessageSquareQuote, Paperclip, Plus, Save, Search, ShieldAlert, X } from "lucide-react";
import type { FindingStatus, FindingSummary } from "../api/types";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { useWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

function parseIdentifiers(
  value: string,
  pattern: RegExp,
  label: string,
  example: string,
): { values: string[]; error?: string } {
  const values = [...new Set(value.split(/[\s,;]+/).map((item) => item.trim().toUpperCase()).filter(Boolean))];
  const invalid = values.filter((item) => !pattern.test(item));
  return invalid.length
    ? { values: [], error: `${label} identifiers must look like ${example}. Check: ${invalid.join(", ")}.` }
    : { values };
}

interface FindingEditDraft {
  title: string;
  description: string;
  severity: FindingSummary["severity"];
  severityRationale: string;
  assetIds: string[];
  cveText: string;
  cweText: string;
  status: FindingStatus;
  evidenceIds: string[];
}

interface ValidatedFindingEdit {
  title: string;
  description: string;
  severity: FindingSummary["severity"];
  severityRationale: string;
  assetIds: string[];
  cveIds: string[];
  cweIds: string[];
  status: FindingStatus;
  evidenceIds: string[];
}

const editableFindingStatuses: FindingStatus[] = [
  "candidate",
  "validated",
  "accepted_risk",
  "false_positive",
  "remediated",
  "retest_passed",
  "retest_failed",
];

function findingEditDraft(finding: FindingSummary): FindingEditDraft {
  return {
    title: finding.title,
    description: finding.description,
    severity: finding.severity,
    severityRationale: finding.severityRationale,
    assetIds: finding.assetIds,
    cveText: finding.cveIds.join(", "),
    cweText: finding.cweIds.join(", "),
    status: finding.status,
    evidenceIds: finding.evidenceIds,
  };
}

function validateFindingEdit(draft: FindingEditDraft): { value?: ValidatedFindingEdit; error?: string } {
  const title = draft.title.trim();
  if (!title) return { error: "A finding title is required." };
  const cves = parseIdentifiers(draft.cveText, /^CVE-\d{4}-\d{4,}$/, "CVE", "CVE-2026-1234");
  const cwes = parseIdentifiers(draft.cweText, /^CWE-\d+$/, "CWE", "CWE-79");
  if (cves.error || cwes.error) return { error: cves.error ?? cwes.error };
  const evidenceIds = [...new Set(draft.evidenceIds)];
  if (draft.status === "confirmed" && evidenceIds.length === 0) {
    return { error: "Confirmed findings must retain at least one evidence record." };
  }
  return {
    value: {
      title,
      description: draft.description.trim(),
      severity: draft.severity,
      severityRationale: draft.severityRationale.trim(),
      assetIds: [...new Set(draft.assetIds)],
      cveIds: cves.values,
      cweIds: cwes.values,
      status: draft.status,
      evidenceIds,
    },
  };
}

function sameIdentifiers(left: string[], right: string[]): boolean {
  const normalizedLeft = [...new Set(left)].sort();
  const normalizedRight = [...new Set(right)].sort();
  return normalizedLeft.length === normalizedRight.length
    && normalizedLeft.every((value, index) => value === normalizedRight[index]);
}

function findingEditMatches(value: ValidatedFindingEdit, finding: FindingSummary): boolean {
  return value.title === finding.title
    && value.description === finding.description
    && value.severity === finding.severity
    && value.severityRationale === finding.severityRationale
    && value.status === finding.status
    && sameIdentifiers(value.assetIds, finding.assetIds)
    && sameIdentifiers(value.evidenceIds, finding.evidenceIds)
    && sameIdentifiers(value.cveIds, finding.cveIds)
    && sameIdentifiers(value.cweIds, finding.cweIds);
}

export function FindingsPage() {
  const confirm = useConfirmation();
  const { requestNebulaDraft } = useWorkbenchDrafts();
  const { assets, createFinding, engagement, evidence, findings, reports, updateFinding, updateReport } = useWorkspace();
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState<"all" | FindingSummary["severity"]>("all");
  const [status, setStatus] = useState<"all" | FindingStatus>("all");
  const [selected, setSelected] = useState<FindingSummary>();
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [candidateSeverity, setCandidateSeverity] = useState<FindingSummary["severity"]>("medium");
  const [severityRationale, setSeverityRationale] = useState("");
  const [assetIds, setAssetIds] = useState<string[]>([]);
  const [cveText, setCveText] = useState("");
  const [cweText, setCweText] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string>();
  const [findingActionSaving, setFindingActionSaving] = useState(false);
  const [findingActionError, setFindingActionError] = useState<string>();
  const [editDraft, setEditDraft] = useState<FindingEditDraft>();
  const [reportId, setReportId] = useState("");
  const visibleFindings = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return findings.filter((finding) => {
      if (severity !== "all" && finding.severity !== severity) return false;
      if (status !== "all" && finding.status !== status) return false;
      if (!needle) return true;
      return `${finding.title} ${finding.description} ${finding.cveIds.join(" ")} ${finding.cweIds.join(" ")}`.toLowerCase().includes(needle);
    });
  }, [findings, query, severity, status]);
  const attention = findings.filter((finding) => ["critical", "high"].includes(finding.severity)).length;
  const awaitingVerification = findings.filter((finding) => ["candidate", "validated"].includes(finding.status)).length;
  const remediated = findings.filter((finding) => ["remediated", "retest_passed"].includes(finding.status)).length;
  const advisoryLinked = findings.filter((finding) => finding.cveIds.length > 0).length;
  const activeFilters = Number(severity !== "all") + Number(status !== "all");
  const validatedEdit = useMemo(() => editDraft ? validateFindingEdit(editDraft) : {}, [editDraft]);
  const findingDirty = Boolean(selected && editDraft && (
    !validatedEdit.value || !findingEditMatches(validatedEdit.value, selected)
  ));
  const findingEditError = validatedEdit.error ?? findingActionError;

  const openCandidate = () => {
    setTitle("");
    setDescription("");
    setCandidateSeverity("medium");
    setSeverityRationale("");
    setAssetIds([]);
    setCveText("");
    setCweText("");
    setFormError(undefined);
    setAdding(true);
  };

  const toggleAsset = (id: string) => {
    setAssetIds((current) => current.includes(id)
      ? current.filter((assetId) => assetId !== id)
      : [...current, id]);
  };

  const submitCandidate = async (event: FormEvent) => {
    event.preventDefault();
    if (!engagement) {
      setFormError("Select or create a project before recording a finding.");
      return;
    }
    if (!title.trim()) {
      setFormError("A finding title is required.");
      return;
    }
    const cves = parseIdentifiers(cveText, /^CVE-\d{4}-\d{4,}$/, "CVE", "CVE-2026-1234");
    const cwes = parseIdentifiers(cweText, /^CWE-\d+$/, "CWE", "CWE-79");
    if (cves.error || cwes.error) {
      setFormError(cves.error ?? cwes.error);
      return;
    }
    setSaving(true);
    setFormError(undefined);
    try {
      await createFinding({
        engagementId: engagement.id,
        title: title.trim(),
        description: description.trim(),
        severity: candidateSeverity,
        severityRationale: severityRationale.trim(),
        assetIds,
        cveIds: cves.values,
        cweIds: cwes.values,
      });
      setAdding(false);
    } catch (error) {
      void logCaughtDiagnostic("interface.findings_page.caught_failure_01", "A handled interface operation failed.", error, "findings_page");
      setFormError(error instanceof Error ? error.message : "Could not create the candidate finding.");
    } finally {
      setSaving(false);
    }
  };

  const allowDiscardFinding = async () => !findingDirty || confirm({
    title: "Discard finding changes?",
    message: "Changes to this finding have not been saved and cannot be recovered.",
    confirmLabel: "Discard changes",
    tone: "danger",
  });

  const inspectFinding = async (finding: FindingSummary) => {
    if (selected?.id === finding.id) return;
    if (!await allowDiscardFinding()) return;
    setSelected(finding);
    setEditDraft(findingEditDraft(finding));
    setReportId(reports.find((report) => report.status !== "final")?.id ?? "");
    setFindingActionError(undefined);
  };

  const closeFinding = async () => {
    if (!await allowDiscardFinding()) return;
    setSelected(undefined);
    setEditDraft(undefined);
    setFindingActionError(undefined);
  };

  const updateEditDraft = <K extends keyof FindingEditDraft,>(field: K, value: FindingEditDraft[K]) => {
    setEditDraft((current) => current ? { ...current, [field]: value } : current);
    setFindingActionError(undefined);
  };

  const askNebulaAboutFinding = async () => {
    if (!selected || !await allowDiscardFinding()) return;
    requestNebulaDraft({
      text: `${selected.title}\n\n${selected.description || "No description recorded."}\n\nSeverity: ${selected.severity}\nStatus: ${selected.status.replaceAll("_", " ")}`,
      sourceKind: "finding",
      sourceId: selected.id,
      sourceLabel: selected.title,
    });
  };

  const saveFinding = async () => {
    if (!selected || !validatedEdit.value || !findingDirty) return;
    const changes = validatedEdit.value;
    setFindingActionSaving(true);
    setFindingActionError(undefined);
    try {
      const updated = await updateFinding(selected.id, {
        title: changes.title,
        description: changes.description,
        severity: changes.severity,
        severityRationale: changes.severityRationale,
        assetIds: changes.assetIds,
        cveIds: changes.cveIds,
        cweIds: changes.cweIds,
        status: changes.status,
        evidenceIds: changes.evidenceIds,
        expectedRevision: selected.revision,
      });
      setSelected(updated);
      setEditDraft(findingEditDraft(updated));
    } catch (error) {
      void logCaughtDiagnostic("interface.findings_page.caught_failure_02", "A handled interface operation failed.", error, "findings_page");
      setFindingActionError(error instanceof Error ? error.message : "Could not update the finding.");
    } finally {
      setFindingActionSaving(false);
    }
  };

  const addFindingToReport = async () => {
    if (!selected || !reportId) return;
    const report = reports.find((item) => item.id === reportId);
    if (!report || report.status === "final") return;
    setFindingActionSaving(true);
    setFindingActionError(undefined);
    try {
      await updateReport(report.id, {
        findingIds: [...new Set([...report.findingIds, selected.id])],
        expectedRevision: report.revision,
      });
    } catch (error) {
      void logCaughtDiagnostic("interface.findings_page.caught_failure_03", "A handled interface operation failed.", error, "findings_page");
      setFindingActionError(error instanceof Error ? error.message : "Could not add the finding to the report.");
    } finally {
      setFindingActionSaving(false);
    }
  };

  return (
    <div className="page findings-page">
      <PageHeader title="Findings" description="Validate, remediate, and retest evidence-backed risk." actions={<button className="button primary" type="button" disabled={!engagement} title={!engagement ? "Create a project first" : undefined} onClick={openCandidate}><Plus size={16} /> New finding</button>} />
      <section className="finding-summary-grid" aria-label="Finding lifecycle summary">
        <article><span className="summary-icon red"><ShieldAlert size={18} /></span><div><strong>{attention}</strong><small>Priority</small></div></article>
        <article><span className="summary-icon violet"><Bug size={18} /></span><div><strong>{awaitingVerification}</strong><small>To verify</small></div></article>
        <article><span className="summary-icon green"><CheckCircle2 size={18} /></span><div><strong>{remediated}</strong><small>Remediated</small></div></article>
        <article><span className="summary-icon orange"><Link2 size={18} /></span><div><strong>{advisoryLinked}</strong><small>CVE linked</small></div></article>
      </section>
      <section className="panel data-panel">
        <header className="data-toolbar">
          <label className="search-field"><Search size={16} /><span className="sr-only">Search findings</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search title, CVE, CWE…" /></label>
          <div className="toolbar-filters"><label><span>Severity</span><select aria-label="Filter findings by severity" value={severity} onChange={(event) => setSeverity(event.target.value as typeof severity)}><option value="all">All severities</option>{(["critical", "high", "medium", "low", "info"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label><label><span>Status</span><select aria-label="Filter findings by status" value={status} onChange={(event) => setStatus(event.target.value as typeof status)}><option value="all">All statuses</option>{(["candidate", "validated", "confirmed", "accepted_risk", "false_positive", "remediated", "retest_passed", "retest_failed"] as const).map((value) => <option value={value} key={value}>{value.replaceAll("_", " ")}</option>)}</select></label>{activeFilters > 0 && <button className="button quiet" type="button" onClick={() => { setSeverity("all"); setStatus("all"); }}>Clear {activeFilters}</button>}</div>
        </header>
        <div className="table-scroll"><table className="data-table findings-table"><thead><tr><th scope="col">Severity</th><th scope="col">Finding</th><th scope="col">Status</th><th scope="col">Assets</th><th scope="col">Evidence</th><th scope="col">Updated</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead><tbody>{visibleFindings.map((finding) => <tr key={finding.id}><td><span className={`severity-label ${finding.severity}`}><span />{finding.severity}</span></td><td><div className="finding-title"><strong>{finding.title}</strong><small>{finding.cveIds.length ? finding.cveIds.join(", ") : finding.cweIds.join(", ") || "No advisory identifier"}</small></div></td><td><span className={`lifecycle-badge ${finding.status}`}>{finding.status.replaceAll("_", " ")}</span></td><td>{finding.affectedAssetCount}</td><td>{finding.evidenceCount}</td><td>{new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(finding.updatedAt))}</td><td><button className="text-link" type="button" aria-label={`Edit ${finding.title}`} onClick={() => void inspectFinding(finding)}>Edit</button></td></tr>)}{visibleFindings.length === 0 && <tr><td colSpan={7}>{query || activeFilters ? "No findings match the current search and filters." : "No findings have been recorded for this project."}</td></tr>}</tbody></table></div>
        <footer className="table-footer"><span>{visibleFindings.length} of {findings.length} findings</span></footer>
      </section>
      {adding && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog finding-create-dialog" role="dialog" aria-modal="true" aria-labelledby="finding-create-title" onSubmit={(event) => void submitCandidate(event)}><header><div><small>Manual analyst entry</small><h2 id="finding-create-title">Create candidate finding</h2></div><button className="icon-button subtle" type="button" aria-label="Close candidate finding dialog" onClick={() => setAdding(false)}><X size={17} /></button></header><p className="provider-dialog-note">This records an unverified candidate only. It will not be treated as validated or confirmed until supporting evidence and independent verification are recorded.</p><label>Title<input required autoFocus maxLength={300} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Concise security observation" /></label><label>Description<textarea rows={4} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What was observed, where, and why it matters" /></label><div className="resource-form-grid"><label>Severity<select value={candidateSeverity} onChange={(event) => setCandidateSeverity(event.target.value as FindingSummary["severity"])}>{(["critical", "high", "medium", "low", "info"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label><label>Lifecycle status<input value="Candidate (unverified)" readOnly aria-readonly="true" /></label></div><label>Severity rationale<textarea rows={3} value={severityRationale} onChange={(event) => setSeverityRationale(event.target.value)} placeholder="Explain impact and likelihood" /></label><fieldset className="resource-checklist"><legend>Affected assets</legend>{assets.length ? assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={assetIds.includes(asset.id)} onChange={() => toggleAsset(asset.id)} /><span>{asset.displayName}</span></label>) : <p>No assets have been added to this project. You can create the candidate without one and link an asset later.</p>}</fieldset><div className="resource-form-grid"><label>CVE identifiers<input value={cveText} onChange={(event) => setCveText(event.target.value)} placeholder="CVE-2026-1234, …" autoCapitalize="characters" spellCheck={false} /></label><label>CWE identifiers<input value={cweText} onChange={(event) => setCweText(event.target.value)} placeholder="CWE-79, …" autoCapitalize="characters" spellCheck={false} /></label></div>{formError && <DiagnosticErrorNotice error={formError} fallback="The form could not be saved." compact />}<footer><button className="button secondary" type="button" onClick={() => setAdding(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || !title.trim()}>{saving ? "Creating…" : "Create candidate"}</button></footer></form></div>}
      {selected && editDraft && <aside className="resource-inspector finding-dialog" role="complementary" aria-labelledby="finding-detail-title">
        <header>
          <div><small>{selected.severity} · {selected.status.replaceAll("_", " ")} · revision {selected.revision}</small><h2 id="finding-detail-title">{selected.title}</h2></div>
          <button className="icon-button subtle" type="button" aria-label="Close finding details" disabled={findingActionSaving} onClick={() => void closeFinding()}><X size={17} /></button>
        </header>
        <div className="finding-inline-actions"><button className="button secondary" type="button" disabled={findingActionSaving} onClick={() => void askNebulaAboutFinding()}><MessageSquareQuote size={14} /> Ask Nebula</button></div>
        <dl className="resource-details"><div><dt>Affected assets</dt><dd>{editDraft.assetIds.length}</dd></div><div><dt>Evidence records</dt><dd>{editDraft.evidenceIds.length}</dd></div><div><dt>Verifier</dt><dd>{selected.verifierId || "Not independently verified"}</dd></div><div><dt>Verified</dt><dd>{selected.verifiedAt ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(selected.verifiedAt)) : "Not yet"}</dd></div></dl>
        <form className="finding-edit-form" onSubmit={(event) => { event.preventDefault(); void saveFinding(); }}>
          <fieldset className="finding-edit-fields" disabled={findingActionSaving}>
            <legend className="sr-only">Editable finding fields</legend>
            <label>Title<input required maxLength={300} value={editDraft.title} onChange={(event) => updateEditDraft("title", event.target.value)} /></label>
            <label>Description<textarea rows={4} value={editDraft.description} placeholder="What was observed, where, and why it matters" onChange={(event) => updateEditDraft("description", event.target.value)} /></label>
            <div className="resource-form-grid">
              <label>Severity<select value={editDraft.severity} onChange={(event) => updateEditDraft("severity", event.target.value as FindingSummary["severity"])}>{(["critical", "high", "medium", "low", "info"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label>
              <label>Lifecycle status<select aria-label="Finding lifecycle status" value={editDraft.status} onChange={(event) => updateEditDraft("status", event.target.value as FindingStatus)}>{editableFindingStatuses.map((value) => <option value={value} key={value}>{value.replaceAll("_", " ")}</option>)}{selected.status === "confirmed" && <option value="confirmed">confirmed</option>}</select></label>
            </div>
            <p className="finding-edit-note">Independent confirmation remains a verification workflow because it requires verifier attribution.</p>
            <label>Severity rationale<textarea rows={3} value={editDraft.severityRationale} placeholder="Explain impact and likelihood" onChange={(event) => updateEditDraft("severityRationale", event.target.value)} /></label>
            <fieldset className="resource-checklist finding-link-checklist"><legend>Affected assets</legend>{assets.length ? assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={editDraft.assetIds.includes(asset.id)} onChange={(event) => updateEditDraft("assetIds", event.target.checked ? [...new Set([...editDraft.assetIds, asset.id])] : editDraft.assetIds.filter((id) => id !== asset.id))} /><span>{asset.displayName}</span></label>) : <p>No assets have been added to this project.</p>}</fieldset>
            <div className="resource-form-grid finding-identifier-grid">
              <label>CVE identifiers<input value={editDraft.cveText} onChange={(event) => updateEditDraft("cveText", event.target.value)} placeholder="CVE-2026-1234, …" autoCapitalize="characters" spellCheck={false} /></label>
              <label>CWE identifiers<input value={editDraft.cweText} onChange={(event) => updateEditDraft("cweText", event.target.value)} placeholder="CWE-79, …" autoCapitalize="characters" spellCheck={false} /></label>
            </div>
            <fieldset className="resource-checklist finding-link-checklist"><legend><Paperclip size={13} /> Attached evidence</legend>{evidence.length ? evidence.map((item) => <label key={item.id}><input type="checkbox" checked={editDraft.evidenceIds.includes(item.id)} onChange={(event) => updateEditDraft("evidenceIds", event.target.checked ? [...new Set([...editDraft.evidenceIds, item.id])] : editDraft.evidenceIds.filter((id) => id !== item.id))} /><span>{item.title}</span></label>) : <p>No evidence is available yet. Preserve a file or terminal screenshot first.</p>}</fieldset>
          </fieldset>
          {findingEditError && <DiagnosticErrorNotice error={findingEditError} fallback="The finding could not be updated." compact />}
          <footer className="finding-edit-footer"><span aria-live="polite">{findingActionSaving ? "Saving…" : findingDirty ? "Unsaved changes" : `Saved · revision ${selected.revision}`}</span><button className="button primary" type="submit" disabled={findingActionSaving || !findingDirty || !validatedEdit.value}>{findingActionSaving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />} {findingActionSaving ? "Saving…" : "Save finding"}</button></footer>
        </form>
        <section><h3><FilePlus2 size={14} /> Add to report</h3>{reports.some((report) => report.status !== "final") ? <div className="finding-report-action"><select aria-label="Report for finding" value={reportId} onChange={(event) => setReportId(event.target.value)}>{reports.filter((report) => report.status !== "final").map((report) => <option value={report.id} key={report.id}>{report.title}</option>)}</select><button className="button secondary" type="button" disabled={!reportId || findingActionSaving || reports.find((report) => report.id === reportId)?.findingIds.includes(selected.id)} onClick={() => void addFindingToReport()}>Add</button></div> : <p>Create a draft report to include this finding.</p>}</section>
      </aside>}
    </div>
  );
}
