import { useState, type FormEvent } from "react";
import { Upload } from "lucide-react";
import type { EvidenceSummary } from "../api/types";
import { encodeLibraryFile } from "../api/libraryFiles";
import { sha256BlobHex } from "../sha256";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import styles from "./NebulaControlAssistant.module.css";

const MAX_EVIDENCE_BYTES = 25 * 1024 * 1024;

export function ButlerEvidenceUpload({projectId, currentProjectId, onSaved, onCancel, onUncertain}: {
  projectId: string;
  currentProjectId: () => string | undefined;
  onSaved: (record: EvidenceSummary, refreshFailed: boolean) => void;
  onCancel: () => void;
  onUncertain: (message: string) => void;
}) {
  const {api, coreState, activeOperator, assets, findings, retryResource} = useWorkspace();
  const [file, setFile] = useState<File>();
  const [title, setTitle] = useState("");
  const [evidenceType, setEvidenceType] = useState("operator_upload");
  const [description, setDescription] = useState("");
  const [findingId, setFindingId] = useState("");
  const [assetIds, setAssetIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file || !api || coreState !== "online" || currentProjectId() !== projectId) {
      setError("Reconnect to Core and select the original project before choosing evidence.");
      return;
    }
    if (file.size > MAX_EVIDENCE_BYTES) {
      setError(`${file.name} exceeds the 25 MB evidence limit.`);
      return;
    }
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      setError("Give this evidence a title before storing it.");
      return;
    }
    setSaving(true);
    setError("");
    let uploadStarted = false;
    try {
      const expectedSha256 = await sha256BlobHex(file);
      const contentBase64 = encodeLibraryFile(await file.arrayBuffer());
      if (currentProjectId() !== projectId) throw new Error("The selected project changed before upload. No file was sent.");
      uploadStarted = true;
      const created = await api.uploadEvidence({
        engagementId: projectId,
        filename: file.name,
        title: trimmedTitle,
        evidenceType,
        contentBase64,
        mediaType: file.type || undefined,
        description,
        source: "operator_upload",
        findingId: findingId || undefined,
        assetIds,
        capturedBy: activeOperator?.id,
      });
      const [observed, listed] = await Promise.all([
        api.getEvidence(created.id),
        api.listEvidence(projectId),
      ]);
      if (currentProjectId() !== projectId || observed.engagementId !== projectId
          || observed.id !== created.id || observed.sha256 !== expectedSha256
          || observed.metadata.filename !== file.name || observed.metadata.size !== file.size
          || observed.title !== trimmedTitle || observed.evidenceType !== evidenceType
          || (observed.findingId ?? "") !== findingId
          || observed.assetIds.length !== assetIds.length
          || assetIds.some((id) => !observed.assetIds.includes(id))
          || !listed.items.some((item) => item.id === created.id && item.sha256 === expectedSha256)) {
        throw new Error("Core did not confirm the saved evidence and its project links.");
      }
      let refreshFailed = false;
      try {
        await retryResource("evidence");
      } catch (refreshError) {
        void logCaughtDiagnostic("interface.butler_evidence_upload.list_refresh_failed", "The project Evidence list could not refresh after Core verified the saved record.", refreshError, "butler_evidence_upload");
        refreshFailed = true;
      }
      onSaved(observed, refreshFailed);
    } catch (caught) {
      void logCaughtDiagnostic(uploadStarted ? "interface.butler_evidence_upload.result_uncertain" : "interface.butler_evidence_upload.preparation_failed", uploadStarted ? "A project evidence upload or Core read-back could not be confirmed." : "The selected evidence file could not be prepared for upload.", caught, "butler_evidence_upload");
      const detail = caught instanceof Error ? caught.message : "The evidence upload could not be confirmed.";
      if (uploadStarted) onUncertain(`Core did not confirm whether ${file.name} was stored. Inspect this project's Evidence list before choosing the file again. ${detail}`);
      else setError(detail);
    } finally {
      setSaving(false);
    }
  };

  return <section className={`${styles.libraryHandoff} ${styles.evidenceHandoff}`} aria-label="Add project evidence">
    <div><strong>Add project evidence</strong><small>Choose a file on this device and review its details. Core keeps the original in this project; Butler receives no file bytes.</small></div>
    <form className={styles.evidenceHandoffForm} onSubmit={(event) => void submit(event)}>
      <label>File<input aria-label="Choose project evidence file" type="file" required disabled={saving} onChange={(event) => {const selected = event.target.files?.[0]; setFile(selected); if (selected && (!title || title === file?.name)) setTitle(selected.name);}} /></label>
      <label>Title<input value={title} required maxLength={500} disabled={saving} onChange={(event) => setTitle(event.target.value)} /></label>
      <label>Evidence type<select value={evidenceType} disabled={saving} onChange={(event) => setEvidenceType(event.target.value)}><option value="operator_upload">Operator upload</option><option value="scanner_output">Scanner output</option><option value="http_exchange">HTTP exchange</option><option value="certificate">Certificate</option><option value="image">Image</option><option value="other">Other</option></select></label>
      <label>Description<textarea rows={2} value={description} maxLength={20_000} disabled={saving} onChange={(event) => setDescription(event.target.value)} /></label>
      <label>Finding<select value={findingId} disabled={saving} onChange={(event) => setFindingId(event.target.value)}><option value="">Not linked</option>{findings.map((finding) => <option key={finding.id} value={finding.id}>{finding.title}</option>)}</select></label>
      <fieldset><legend>Linked assets</legend>{assets.length ? assets.map((asset) => <label key={asset.id}><input type="checkbox" checked={assetIds.includes(asset.id)} disabled={saving} onChange={(event) => setAssetIds(event.target.checked ? [...assetIds, asset.id] : assetIds.filter((id) => id !== asset.id))} />{asset.displayName}</label>) : <small>No assets available.</small>}</fieldset>
      <small>{activeOperator ? `Captured by ${activeOperator.displayName}.` : "No active operator profile; this upload will be stored without operator attribution."}</small>
      {file && file.size > MAX_EVIDENCE_BYTES && <p role="alert">The selected file exceeds 25 MB.</p>}
      {error && <DiagnosticErrorNotice error={error} fallback="The evidence could not be stored." compact />}
      <div className={styles.libraryHandoffActions}>
        <button className="button primary" type="submit" disabled={saving || !file || file.size > MAX_EVIDENCE_BYTES}><Upload size={16} aria-hidden="true" />{saving ? "Storing…" : "Store evidence"}</button>
        <button className="button quiet" type="button" disabled={saving} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  </section>;
}
