import {useState, type FormEvent} from "react";
import {Upload} from "lucide-react";
import {ACCEPTED_LIBRARY_FILES, encodeLibraryFile, MAX_LIBRARY_ITEM_BYTES} from "../api/libraryFiles";
import {sha256BlobHex} from "../sha256";
import {useWorkspace} from "../state/WorkspaceContext";
import {DiagnosticErrorNotice, logCaughtDiagnostic} from "../diagnostics";
import type {KnowledgeSource} from "../api/types";
import styles from "./NebulaControlAssistant.module.css";

export function ButlerSourceAdd({projectId, inputKind, currentProjectId, onSaved, onCancel, onUncertain}: {
  projectId: string; inputKind: "file" | "url"; currentProjectId: () => string | undefined;
  onSaved: (source: Pick<KnowledgeSource, "id" | "engagementId" | "name" | "status"> & {revision: number}, refreshFailed: boolean) => void;
  onCancel: () => void; onUncertain: (message: string) => void;
}) {
  const {api, coreState, retryResource} = useWorkspace();
  const [file, setFile] = useState<File>();
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || coreState !== "online" || currentProjectId() !== projectId) {
      setError("Reconnect and select the original project before adding a source."); return;
    }
    if (inputKind === "file" && (!file || file.size > MAX_LIBRARY_ITEM_BYTES)) {
      setError("Choose a source file of at most 20 MB."); return;
    }
    setSaving(true); setError("");
    let started = false;
    try {
      const digest = file && inputKind === "file" ? await sha256BlobHex(file) : undefined;
      const content = file && inputKind === "file" ? encodeLibraryFile(await file.arrayBuffer()) : undefined;
      if (currentProjectId() !== projectId) throw new Error("The project changed before ingestion. Nothing was sent.");
      started = true;
      const created = inputKind === "file"
        ? await api.ingestKnowledgeSource({engagementId: projectId, filename: file!.name, mediaType: file!.type || undefined, contentBase64: content!})
        : await api.ingestKnowledgeUrlSource({engagementId: projectId, url: url.trim()});
      const [observed, listed] = await Promise.all([api.getKnowledgeSource(created.id), api.listKnowledgeSources(projectId)]);
      if (currentProjectId() !== projectId || observed.id !== created.id || observed.engagementId !== projectId
          || !listed.items.some(source => source.id === created.id)
          || (inputKind === "file" && (observed.metadata.sha256 !== digest || observed.metadata.size !== file!.size))) {
        throw new Error("Core did not confirm the exact source in this project's list.");
      }
      let refreshFailed = false;
      try { await retryResource("sources"); }
      catch (caught) {
        void logCaughtDiagnostic("interface.butler_source_add.refresh_failed", "The source was saved but its project list could not refresh.", caught, "butler_sources");
        refreshFailed = true;
      }
      onSaved({id: observed.id, engagementId: observed.engagementId, name: observed.name,
        revision: observed.revision, status: observed.status}, refreshFailed);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_source_add.save_failed", "The source ingestion or its readback could not be confirmed.", caught, "butler_sources");
      if (started) onUncertain("Core did not confirm this source. Inspect this project's Sources before another upload or URL capture; it may already have been saved.");
      else setError("The source could not be prepared. Reconnect and choose it again.");
    } finally { setSaving(false); }
  };
  return <section className={`${styles.libraryHandoff} ${styles.evidenceHandoff}`} aria-label="Add project source">
    <div><strong>Add project source</strong><small>{inputKind === "file" ? "Choose a file on this device." : "Enter a document URL here."} Core indexes it for this project. Butler can later retrieve matching passages.</small></div>
    <form className={styles.evidenceHandoffForm} onSubmit={event => void submit(event)}>
      {inputKind === "file" ? <label>File<input type="file" accept={ACCEPTED_LIBRARY_FILES} aria-label="Choose project source file" required disabled={saving} onChange={event => setFile(event.target.files?.[0])} /></label>
        : <label>Source URL<input type="url" required maxLength={4096} autoComplete="off" value={url} disabled={saving} onChange={event => setUrl(event.target.value)} /></label>}
      {error && <DiagnosticErrorNotice error={error} compact />}
      <div className={styles.libraryHandoffActions}>
        <button type="submit" className="button primary" disabled={saving || (inputKind === "file" ? !file : !url.trim())}><Upload size={16} aria-hidden="true" />{saving ? "Adding…" : "Add source"}</button>
        <button type="button" className="button quiet" disabled={saving} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  </section>;
}
