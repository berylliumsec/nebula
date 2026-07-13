import { useMemo, useState, type FormEvent } from "react";
import { Camera, FileCode2, FileSearch, Image, LoaderCircle, LockKeyhole, Search, Upload, X } from "lucide-react";
import type { EvidenceSummary } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

const MAX_EVIDENCE_BYTES = 25 * 1024 * 1024;

const previewEvidence: EvidenceSummary[] = [
  { id: "preview-1", engagementId: "preview", evidenceType: "scanner_output", title: "gateway-service-detection.xml", description: "Preview Nmap XML artifact", assetIds: [], sha256: "7f392ad861", capturedAt: "2026-07-12T18:58:00Z", capturedBy: "Network analyst", createdAt: "2026-07-12T18:58:00Z", updatedAt: "2026-07-12T18:58:00Z", metadata: { filename: "gateway-service-detection.xml", mediaType: "application/xml", size: 86_016, source: "preview" } },
  { id: "preview-2", engagementId: "preview", evidenceType: "http_exchange", title: "jwt-algorithm-response.har", description: "Preview HTTP archive", assetIds: [], sha256: "a821d41c09", capturedAt: "2026-07-12T19:02:00Z", capturedBy: "Web analyst", createdAt: "2026-07-12T19:02:00Z", updatedAt: "2026-07-12T19:02:00Z", metadata: { filename: "jwt-algorithm-response.har", mediaType: "application/json", size: 223_232, source: "preview" } },
];

function encodeBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  return btoa(binary);
}

function formatSize(value?: number): string {
  if (value === undefined) return "Unknown size";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function evidenceIcon(item: EvidenceSummary) {
  const type = `${item.evidenceType} ${item.metadata.mediaType ?? ""}`.toLowerCase();
  if (type.includes("image")) return Image;
  if (type.includes("http") || type.includes("har")) return FileSearch;
  return FileCode2;
}

export function EvidencePage() {
  const { activeOperator, api, assets, engagement, evidence, findings, operatorProfiles, previewMode, uploadEvidence } = useWorkspace();
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [file, setFile] = useState<File>();
  const [title, setTitle] = useState("");
  const [evidenceType, setEvidenceType] = useState("operator_upload");
  const [description, setDescription] = useState("");
  const [findingId, setFindingId] = useState("");
  const [assetIds, setAssetIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [message, setMessage] = useState<string>();
  const [busyId, setBusyId] = useState<string>();
  const [selected, setSelected] = useState<EvidenceSummary>();
  const items = previewMode ? previewEvidence : evidence;
  const operatorLabel = (value?: string) => value
    ? operatorProfiles.find((profile) => profile.id === value)?.displayName ?? value
    : "Unattributed";
  const visibleItems = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? items.filter((item) => `${item.title} ${item.description} ${item.evidenceType} ${item.sha256 ?? ""} ${item.metadata.filename ?? ""} ${item.metadata.source ?? ""} ${operatorLabel(item.capturedBy)}`.toLowerCase().includes(needle)) : items;
  }, [items, operatorProfiles, query]);

  const closeUpload = () => {
    setAdding(false);
    setFile(undefined);
    setTitle("");
    setEvidenceType("operator_upload");
    setDescription("");
    setFindingId("");
    setAssetIds([]);
    setError(undefined);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!file || !engagement) return;
    if (file.size > MAX_EVIDENCE_BYTES) {
      setError(`${file.name} is larger than the 25 MB evidence limit.`);
      return;
    }
    setSaving(true);
    setError(undefined);
    setMessage(`Reading ${file.name}…`);
    try {
      const contentBase64 = encodeBase64(await file.arrayBuffer());
      setMessage(`Hashing and storing ${file.name}…`);
      await uploadEvidence({ engagementId: engagement.id, filename: file.name, title, evidenceType, contentBase64, mediaType: file.type || undefined, description, source: "operator_upload", findingId: findingId || undefined, assetIds, capturedBy: activeOperator?.id });
      setMessage(`${file.name} was stored and verified.`);
      closeUpload();
    } catch (uploadError) {
      setMessage(undefined);
      setError(uploadError instanceof Error ? uploadError.message : "Could not upload the evidence artifact.");
    } finally {
      setSaving(false);
    }
  };

  const download = async (item: EvidenceSummary) => {
    if (!api || !item.artifactId) return;
    setBusyId(item.id);
    setError(undefined);
    try {
      const blob = await api.getArtifactContent(item.artifactId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = item.metadata.filename || item.title;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : "Could not download the evidence artifact.");
    } finally {
      setBusyId(undefined);
    }
  };

  return (
    <div className="page evidence-page">
      <PageHeader eyebrow="Immutable provenance" title="Evidence" description="Content-addressed artifacts preserve source, timestamps, hashes, and finding links." actions={<><button className="button secondary" type="button" disabled title="Desktop screenshot capture is release-gated"><Camera size={16} /> Capture unavailable</button><button className="button primary" type="button" disabled={previewMode || !engagement} onClick={() => { setError(undefined); setAdding(true); }}><Upload size={16} /> Add evidence</button></>} />
      <div className="evidence-callout callout"><LockKeyhole size={18} /><div><strong>Originals are immutable</strong><p>Uploaded bytes are content-addressed and downloaded as attachments; active content is never rendered inline.</p></div><span>SHA-256</span></div>
      {message && <div className="knowledge-status" role="status">{saving && <LoaderCircle className="spin" size={15} />}{message}</div>}
      {error && <div className="knowledge-status error" role="alert">{error}</div>}
      <section className="panel data-panel evidence-panel">
        <header className="data-toolbar"><label className="search-field"><Search size={16} /><span className="sr-only">Search evidence</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search artifact, hash, source…" /></label><span className="toolbar-summary">{visibleItems.length} artifact{visibleItems.length === 1 ? "" : "s"}</span></header>
        <div className="artifact-grid">{visibleItems.map((item) => { const Icon = evidenceIcon(item); return <article className="artifact-card" key={item.id}><div className="artifact-preview"><Icon size={31} strokeWidth={1.4} /><span>{item.evidenceType.replaceAll("_", " ")}</span></div><div className="artifact-body"><h3 title={item.title}>{item.title}</h3><p>{formatSize(item.metadata.size)} · <code>{item.sha256 ? `${item.sha256.slice(0, 10)}…` : "hash unavailable"}</code></p><dl><div><dt>Captured by</dt><dd>{operatorLabel(item.capturedBy)}</dd></div><div><dt>Assets</dt><dd>{item.assetIds.length || "Not linked"}</dd></div></dl></div><footer><span><LockKeyhole size={13} /> {item.sha256 ? "Hash recorded" : "Metadata only"}</span><span className="artifact-actions"><button className="text-link" type="button" onClick={() => setSelected(item)}>Inspect</button><button className="text-link" type="button" disabled={previewMode || !item.artifactId || busyId === item.id} onClick={() => void download(item)}>{busyId === item.id ? "Downloading…" : "Download"}</button></span></footer></article>; })}{visibleItems.length === 0 && <div className="empty-state compact"><LockKeyhole size={23} /><strong>{query ? "No matching evidence" : "No evidence loaded"}</strong><p>{query ? "Try another artifact name, source, or hash." : "Upload an artifact to create an immutable evidence record."}</p></div>}</div>
      </section>
      {selected && <aside className="resource-inspector" role="complementary" aria-labelledby="evidence-detail-title"><header><div><small>{selected.evidenceType.replaceAll("_", " ")}</small><h2 id="evidence-detail-title">{selected.title}</h2></div><button className="icon-button subtle" type="button" aria-label="Close evidence details" onClick={() => setSelected(undefined)}><X size={17} /></button></header><p className="resource-description">{selected.description || "No description recorded."}</p><dl className="resource-details"><div><dt>SHA-256</dt><dd><code>{selected.sha256 || "Not recorded"}</code></dd></div><div><dt>Captured by</dt><dd>{operatorLabel(selected.capturedBy)}</dd></div><div><dt>Captured</dt><dd>{new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(selected.capturedAt))}</dd></div><div><dt>Size</dt><dd>{formatSize(selected.metadata.size)}</dd></div><div><dt>Assets</dt><dd>{selected.assetIds.length || "Not linked"}</dd></div></dl>{!previewMode && selected.artifactId && <div className="inspector-actions"><button className="button primary full" type="button" disabled={busyId === selected.id} onClick={() => void download(selected)}>{busyId === selected.id ? "Downloading…" : "Download original"}</button></div>}</aside>}
      {adding && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="evidence-dialog-title" onSubmit={(event) => void submit(event)}><header><div><small>Immutable artifact</small><h2 id="evidence-dialog-title">Add evidence</h2></div><button className="icon-button subtle" type="button" aria-label="Close evidence dialog" onClick={closeUpload}><X size={17} /></button></header><label>File<input required type="file" onChange={(event) => { const selected = event.target.files?.[0]; setFile(selected); if (selected && !title) setTitle(selected.name); }} /></label><label>Title<input required value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>Evidence type<select value={evidenceType} onChange={(event) => setEvidenceType(event.target.value)}><option value="operator_upload">Operator upload</option><option value="scanner_output">Scanner output</option><option value="http_exchange">HTTP exchange</option><option value="certificate">Certificate</option><option value="image">Image</option><option value="other">Other</option></select></label><label>Description<textarea rows={3} value={description} onChange={(event) => setDescription(event.target.value)} /></label><label>Finding<select value={findingId} onChange={(event) => setFindingId(event.target.value)}><option value="">Not linked</option>{findings.map((finding) => <option value={finding.id} key={finding.id}>{finding.title}</option>)}</select></label><fieldset className="resource-checklist"><legend>Linked assets</legend>{assets.length ? assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={assetIds.includes(asset.id)} onChange={(event) => setAssetIds(event.target.checked ? [...assetIds, asset.id] : assetIds.filter((id) => id !== asset.id))} />{asset.displayName}</label>) : <p>No assets available.</p>}</fieldset><p className="provider-dialog-note">{activeOperator ? `Captured by ${activeOperator.displayName}.` : "No active operator profile; this upload will be stored without operator attribution. You can create one in Settings."}</p>{file && file.size > MAX_EVIDENCE_BYTES && <p className="form-error" role="alert">The selected file exceeds 25 MB.</p>}{error && <p className="form-error" role="alert">{error}</p>}<footer><button className="button secondary" type="button" onClick={closeUpload}>Cancel</button><button className="button primary" type="submit" disabled={saving || !file || file.size > MAX_EVIDENCE_BYTES}>{saving ? "Uploading…" : "Store evidence"}</button></footer></form></div>}
    </div>
  );
}
