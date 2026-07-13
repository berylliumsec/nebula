import { useMemo, useState, type FormEvent } from "react";
import { Bug, CheckCircle2, GitCompareArrows, Link2, Plus, Search, ShieldAlert, X } from "lucide-react";
import type { FindingStatus, FindingSummary } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

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

export function FindingsPage() {
  const { assets, createFinding, engagement, findings, previewMode } = useWorkspace();
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
      setFormError("Select or create an engagement before recording a finding.");
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
      setFormError(error instanceof Error ? error.message : "Could not create the candidate finding.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page findings-page">
      <PageHeader title="Findings" description="Validate, remediate, and retest evidence-backed risk." actions={<><button className="button secondary" type="button" disabled title="Cross-scan comparison is release-gated"><GitCompareArrows size={16} /> Compare scan</button><button className="button primary" type="button" disabled={previewMode || !engagement} title={!engagement ? "Create an engagement first" : undefined} onClick={openCandidate}><Plus size={16} /> New finding</button></>} />
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
        <div className="table-scroll"><table className="data-table findings-table"><thead><tr><th scope="col">Severity</th><th scope="col">Finding</th><th scope="col">Status</th><th scope="col">Assets</th><th scope="col">Evidence</th><th scope="col">Updated</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead><tbody>{visibleFindings.map((finding) => <tr key={finding.id}><td><span className={`severity-label ${finding.severity}`}><span />{finding.severity}</span></td><td><div className="finding-title"><strong>{finding.title}</strong><small>{finding.cveIds.length ? finding.cveIds.join(", ") : finding.cweIds.join(", ") || "No advisory identifier"}</small></div></td><td><span className={`lifecycle-badge ${finding.status}`}>{finding.status.replaceAll("_", " ")}</span></td><td>{finding.affectedAssetCount}</td><td>{finding.evidenceCount}</td><td>{new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(finding.updatedAt))}</td><td><button className="text-link" type="button" onClick={() => setSelected(finding)}>Inspect</button></td></tr>)}{visibleFindings.length === 0 && <tr><td colSpan={7}>{query || activeFilters ? "No findings match the current search and filters." : "No findings have been recorded for this engagement."}</td></tr>}</tbody></table></div>
        <footer className="table-footer"><span>{visibleFindings.length} of {findings.length} findings</span></footer>
      </section>
      {adding && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog finding-create-dialog" role="dialog" aria-modal="true" aria-labelledby="finding-create-title" onSubmit={(event) => void submitCandidate(event)}><header><div><small>Manual analyst entry</small><h2 id="finding-create-title">Create candidate finding</h2></div><button className="icon-button subtle" type="button" aria-label="Close candidate finding dialog" onClick={() => setAdding(false)}><X size={17} /></button></header><p className="provider-dialog-note">This records an unverified candidate only. It will not be treated as validated or confirmed until supporting evidence and independent verification are recorded.</p><label>Title<input required autoFocus maxLength={300} value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Concise security observation" /></label><label>Description<textarea rows={4} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What was observed, where, and why it matters" /></label><div className="resource-form-grid"><label>Severity<select value={candidateSeverity} onChange={(event) => setCandidateSeverity(event.target.value as FindingSummary["severity"])}>{(["critical", "high", "medium", "low", "info"] as const).map((value) => <option value={value} key={value}>{value}</option>)}</select></label><label>Lifecycle status<input value="Candidate (unverified)" readOnly aria-readonly="true" /></label></div><label>Severity rationale<textarea rows={3} value={severityRationale} onChange={(event) => setSeverityRationale(event.target.value)} placeholder="Explain impact and likelihood" /></label><fieldset className="resource-checklist"><legend>Affected assets</legend>{assets.length ? assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={assetIds.includes(asset.id)} onChange={() => toggleAsset(asset.id)} /><span>{asset.displayName}</span></label>) : <p>No assets have been added to this engagement. You can create the candidate without one and link an asset later.</p>}</fieldset><div className="resource-form-grid"><label>CVE identifiers<input value={cveText} onChange={(event) => setCveText(event.target.value)} placeholder="CVE-2026-1234, …" autoCapitalize="characters" spellCheck={false} /></label><label>CWE identifiers<input value={cweText} onChange={(event) => setCweText(event.target.value)} placeholder="CWE-79, …" autoCapitalize="characters" spellCheck={false} /></label></div>{formError && <p className="form-error" role="alert">{formError}</p>}<footer><button className="button secondary" type="button" onClick={() => setAdding(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving || !title.trim()}>{saving ? "Creating…" : "Create candidate"}</button></footer></form></div>}
      {selected && <aside className="resource-inspector finding-dialog" role="complementary" aria-labelledby="finding-detail-title"><header><div><small>{selected.severity} · {selected.status.replaceAll("_", " ")}</small><h2 id="finding-detail-title">{selected.title}</h2></div><button className="icon-button subtle" type="button" aria-label="Close finding details" onClick={() => setSelected(undefined)}><X size={17} /></button></header><p className="resource-description">{selected.description || "No description has been recorded."}</p><dl className="resource-details"><div><dt>Affected assets</dt><dd>{selected.affectedAssetCount}</dd></div><div><dt>Evidence records</dt><dd>{selected.evidenceCount}</dd></div><div><dt>Verifier</dt><dd>{selected.verifierId || "Not independently verified"}</dd></div><div><dt>Verified</dt><dd>{selected.verifiedAt ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(selected.verifiedAt)) : "Not yet"}</dd></div></dl>{selected.severityRationale && <section><h3>Severity rationale</h3><p>{selected.severityRationale}</p></section>}<div className="scope-chip-list">{[...selected.cveIds, ...selected.cweIds].length ? [...selected.cveIds, ...selected.cweIds].map((id) => <span key={id}>{id}</span>) : <span>No CVE/CWE identifiers</span>}</div></aside>}
    </div>
  );
}
