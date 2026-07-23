import { useEffect, useMemo, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import {
  BookOpen,
  Database,
  Download,
  FileText,
  Globe2,
  Link2,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldAlert,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import type { KnowledgeIndexStatus, KnowledgeSource } from "../api/types";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

const MAX_SOURCE_BYTES = 20 * 1024 * 1024;

function sourceIcon(source: KnowledgeSource) {
  if (source.metadata.origin === "url" || source.sourceType.includes("web")) return Globe2;
  if (source.sourceType.includes("structured")) return Database;
  if (source.metadata.mediaType === "application/pdf" || source.sourceType.includes("document")) return FileText;
  return BookOpen;
}

function sourceType(source: KnowledgeSource): string {
  if (source.metadata.mediaType) return source.metadata.mediaType;
  return source.sourceType.replaceAll("_", " ");
}

function displayTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(date);
}

function encodeBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function formatModelBytes(value: number): string {
  if (value <= 0) return "0 MiB";
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

export function KnowledgePage() {
  const confirm = useConfirmation();
  const [searchParams] = useSearchParams();
  const {
    api,
    coreState,
    engagement,
    ingestKnowledgeSource,
    ingestKnowledgeUrlSource,
    knowledgeSources,
    reindexKnowledgeSource,
    removeKnowledgeSource,
  } = useWorkspace();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [uploading, setUploading] = useState(false);
  const [addingUrl, setAddingUrl] = useState(false);
  const [sourceUrl, setSourceUrl] = useState("");
  const [statusMessage, setStatusMessage] = useState<string>();
  const [indexStatus, setIndexStatus] = useState<KnowledgeIndexStatus>();
  const [error, setError] = useState<string>();
  const [selected, setSelected] = useState<KnowledgeSource>();
  const sources = knowledgeSources;
  const requestedSourceId = searchParams.get("source");
  const visibleSources = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return sources;
    return sources.filter((source) => `${source.name} ${source.sourceType} ${source.citation ?? ""}`.toLowerCase().includes(needle));
  }, [query, sources]);

  useEffect(() => {
    if (!requestedSourceId) return;
    const requested = sources.find((source) => source.id === requestedSourceId);
    if (requested) {
      setQuery(requested.name);
      setSelected(requested);
    }
  }, [requestedSourceId, sources]);

  useEffect(() => {
    if (!api || coreState !== "online") return;
    const controller = new AbortController();
    void api.getKnowledgeIndexStatus(controller.signal).then(setIndexStatus).catch((statusError) => {
      if (!controller.signal.aborted) {
        void logCaughtDiagnostic("interface.knowledge_page.caught_failure_05", "The knowledge index status could not be loaded.", statusError, "knowledge_page");
      }
    });
    return () => controller.abort();
  }, [api, coreState]);

  const withIndexStatusPolling = async <T,>(operation: () => Promise<T>): Promise<T> => {
    let polling = true;
    const poll = async () => {
      while (polling && api) {
        try {
          setIndexStatus(await api.getKnowledgeIndexStatus());
        } catch (pollError) {
          void logCaughtDiagnostic("interface.knowledge_page.caught_failure_06", "A knowledge index status poll failed during an active operation.", pollError, "knowledge_page");
          // The operation itself owns the operator-visible error.
        }
        await new Promise((resolve) => window.setTimeout(resolve, 250));
      }
    };
    const pollingTask = poll();
    try {
      return await operation();
    } finally {
      polling = false;
      await pollingTask;
      if (api) {
        try {
          setIndexStatus(await api.getKnowledgeIndexStatus());
        } catch (refreshError) {
          void logCaughtDiagnostic("interface.knowledge_page.caught_failure_07", "The final knowledge index status refresh failed.", refreshError, "knowledge_page");
          // Preserve the operation result when a final status refresh fails.
        }
      }
    }
  };

  const setSourceBusy = (id: string, busy: boolean) => {
    setBusyIds((current) => {
      const next = new Set(current);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const uploadFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > MAX_SOURCE_BYTES) {
      setError(`${file.name} is larger than the 20 MB ingestion limit.`);
      return;
    }
    if (!engagement) {
      setError("Create or select a project before adding a knowledge source.");
      return;
    }
    setUploading(true);
    setError(undefined);
    setStatusMessage(`Reading ${file.name}…`);
    try {
      const contentBase64 = encodeBase64(await file.arrayBuffer());
      setStatusMessage(`Indexing ${file.name}…`);
      await withIndexStatusPolling(() => ingestKnowledgeSource({
          engagementId: engagement.id,
          filename: file.name,
          mediaType: file.type || undefined,
          contentBase64,
        }));
      setStatusMessage(`${file.name} is ready for cited retrieval.`);
    } catch (uploadError) {
      void logCaughtDiagnostic("interface.knowledge_page.caught_failure_01", "A handled interface operation failed.", uploadError, "knowledge_page");
      setStatusMessage(undefined);
      setError(uploadError instanceof Error ? uploadError.message : "Could not ingest the selected source.");
    } finally {
      setUploading(false);
    }
  };

  const addUrl = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = sourceUrl.trim();
    if (!engagement) {
      setError("Create or select a project before adding a knowledge source.");
      return;
    }
    if (!URL.canParse(value)) {
      setError("Enter a valid HTTP or HTTPS URL.");
      return;
    }
    const parsed = new URL(value);
    if (!["http:", "https:"].includes(parsed.protocol)) {
      setError("Enter a valid HTTP or HTTPS URL.");
      return;
    }
    setUploading(true);
    setError(undefined);
    setStatusMessage(`Fetching ${value}…`);
    try {
      const created = await withIndexStatusPolling(() => ingestKnowledgeUrlSource({
        engagementId: engagement.id,
        url: value,
      }));
      setAddingUrl(false);
      setSourceUrl("");
      setStatusMessage(`${created.name} is ready for cited retrieval.`);
    } catch (urlError) {
      void logCaughtDiagnostic("interface.knowledge_page.caught_failure_08", "A URL knowledge source could not be added.", urlError, "knowledge_page");
      setStatusMessage(undefined);
      setError(urlError instanceof Error ? urlError.message : "Could not ingest the URL source.");
    } finally {
      setUploading(false);
    }
  };

  const reindex = async (source: KnowledgeSource) => {
    setSourceBusy(source.id, true);
    setError(undefined);
    try {
      await withIndexStatusPolling(() => reindexKnowledgeSource(source.id));
      setStatusMessage(`${source.name} was reindexed.`);
    } catch (reindexError) {
      void logCaughtDiagnostic("interface.knowledge_page.caught_failure_02", "A handled interface operation failed.", reindexError, "knowledge_page");
      setError(reindexError instanceof Error ? reindexError.message : `Could not reindex ${source.name}.`);
    } finally {
      setSourceBusy(source.id, false);
    }
  };

  const reindexAll = async () => {
    const candidates = visibleSources.filter((source) => !busyIds.has(source.id));
    if (!candidates.length) return;
    setError(undefined);
    setStatusMessage(`Reindexing ${candidates.length} source${candidates.length === 1 ? "" : "s"}…`);
    candidates.forEach((source) => setSourceBusy(source.id, true));
    const results = await withIndexStatusPolling(() => Promise.allSettled(candidates.map((source) => reindexKnowledgeSource(source.id))));
    candidates.forEach((source) => setSourceBusy(source.id, false));
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length) {
      setStatusMessage(undefined);
      const reason = failures[0].status === "rejected" ? failures[0].reason : undefined;
      setError(reason instanceof Error ? reason.message : `${failures.length} source reindex request${failures.length === 1 ? "" : "s"} failed.`);
    } else {
      setStatusMessage(`Reindexed ${candidates.length} source${candidates.length === 1 ? "" : "s"}.`);
    }
  };

  const remove = async (source: KnowledgeSource) => {
    if (!await confirm({
      title: `Remove ${source.name}?`,
      message: "This source will no longer be used for retrieval. The immutable source artifact will be retained.",
      confirmLabel: "Remove source",
      tone: "danger",
    })) return;
    setSourceBusy(source.id, true);
    setError(undefined);
    try {
      await removeKnowledgeSource(source.id);
      setStatusMessage(`${source.name} was removed from retrieval.`);
    } catch (removeError) {
      void logCaughtDiagnostic("interface.knowledge_page.caught_failure_03", "A handled interface operation failed.", removeError, "knowledge_page");
      setError(removeError instanceof Error ? removeError.message : `Could not remove ${source.name}.`);
    } finally {
      setSourceBusy(source.id, false);
    }
  };

  const download = async (source: KnowledgeSource) => {
    if (!source.artifactId || !api) return;
    setSourceBusy(source.id, true);
    setError(undefined);
    try {
      const blob = await api.getArtifactContent(source.artifactId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = source.metadata.filename || source.name;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (downloadError) {
      void logCaughtDiagnostic("interface.knowledge_page.caught_failure_04", "A handled interface operation failed.", downloadError, "knowledge_page");
      setError(downloadError instanceof Error ? downloadError.message : `Could not download ${source.name}.`);
    } finally {
      setSourceBusy(source.id, false);
    }
  };

  const canMutate = coreState === "online" && Boolean(engagement);
  return (
    <div className="page knowledge-page">
      <PageHeader
        title="Knowledge"
        description="Sources available for cited retrieval."
        actions={<>
          <input ref={inputRef} className="sr-only" type="file" aria-label="Choose knowledge source" accept=".txt,.md,.markdown,.rst,.log,.csv,.json,.jsonl,.ndjson,.html,.htm,.pdf,.docx,.xlsx,text/plain,text/markdown,text/x-markdown,text/csv,application/csv,application/json,application/jsonl,application/x-jsonlines,application/x-ndjson,text/html,application/xhtml+xml,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(event) => void uploadFile(event)} />
          <button className="button secondary" type="button" disabled={!canMutate || uploading} onClick={() => { setError(undefined); setAddingUrl(true); }}><Link2 size={16} /> Add URL</button>
          <button className="button primary" type="button" disabled={!canMutate || uploading} onClick={() => inputRef.current?.click()}>{uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />} {uploading ? "Adding source…" : "Upload file"}</button>
        </>}
      />
      {indexStatus && !["ready", "disabled"].includes(indexStatus.state) && <section className={`knowledge-model-status ${indexStatus.state}`} role="status" aria-live="polite">
        <span className="metric-icon">{["downloading", "preparing"].includes(indexStatus.state) ? <LoaderCircle className="spin" size={18} /> : <Database size={18} />}</span>
        <div>
          <strong>{indexStatus.state === "required" ? "Local semantic search" : indexStatus.state === "downloading" ? "Downloading the local retrieval model" : indexStatus.state === "preparing" ? "Preparing semantic search" : "Semantic search setup needs attention"}</strong>
          <p>{indexStatus.state === "required"
              ? `Adding or reindexing a source will download the ${formatModelBytes(indexStatus.totalBytes)} ${indexStatus.model} model once. It remains on this device.`
              : indexStatus.state === "downloading"
                ? `${formatModelBytes(indexStatus.downloadedBytes)} of ${formatModelBytes(indexStatus.totalBytes)} downloaded · ${Math.min(100, Math.round((indexStatus.downloadedBytes / Math.max(1, indexStatus.totalBytes)) * 100))}%`
                : indexStatus.state === "preparing"
                  ? "Download complete. Verifying and preparing the model on this device."
                  : `${indexStatus.detail ?? "The local embedding model could not be prepared."} Check the connection and retry Add source or Reindex.`}</p>
          {indexStatus.state === "downloading" && <div className="knowledge-model-progress" role="progressbar" aria-label="Embedding model download" aria-valuemin={0} aria-valuemax={indexStatus.totalBytes} aria-valuenow={indexStatus.downloadedBytes}><span style={{ width: `${Math.min(100, (indexStatus.downloadedBytes / Math.max(1, indexStatus.totalBytes)) * 100)}%` }} /></div>}
        </div>
      </section>}
      {statusMessage && <div className="knowledge-status" role="status">{uploading && <LoaderCircle className="spin" size={15} />}{statusMessage}</div>}
      {error && <DiagnosticErrorNotice error={error} fallback="The knowledge operation could not be completed." />}
      <div className="knowledge-layout">
        <section className="panel data-panel knowledge-sources">
          <header className="data-toolbar">
            <label className="search-field"><Search size={16} /><span className="sr-only">Search knowledge sources</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search sources…" /></label>
            <button className="button quiet" type="button" disabled={!canMutate || visibleSources.length === 0 || busyIds.size > 0} onClick={() => void reindexAll()}><RefreshCw className={busyIds.size > 0 ? "spin" : undefined} size={15} /> Reindex {query ? "results" : "all"}</button>
          </header>
          <div className="source-list">
            {visibleSources.map((source) => {
              const Icon = sourceIcon(source);
              const busy = busyIds.has(source.id);
              return (
                <article key={source.id}>
                  <span className="source-icon"><Icon size={19} /></span>
                  <div><h3 title={source.name}>{source.name}</h3><p>{sourceType(source)}</p></div>
                  <span><strong>{source.documentCount || "—"}</strong><small>chunks</small></span>
                  <span className={`source-state ${busy ? "indexing" : source.status}`}>{busy && <RefreshCw className="spin" size={13} />}{busy ? "working" : source.status}</span>
                  <span className="source-updated">{displayTime(source.updatedAt)}</span>
                  <div className="source-actions">
                    <button className="text-link" type="button" onClick={() => setSelected(source)}>Inspect</button>
                    <button className="icon-button subtle" type="button" title="Reindex source" aria-label={`Reindex ${source.name}`} disabled={!canMutate || busy} onClick={() => void reindex(source)}><RefreshCw size={14} /></button>
                    <button className="icon-button subtle" type="button" title="Download original" aria-label={`Download ${source.name}`} disabled={!source.artifactId || !api || busy} onClick={() => void download(source)}><Download size={14} /></button>
                    <button className="icon-button subtle" type="button" title="Remove from retrieval" aria-label={`Remove ${source.name}`} disabled={!canMutate || busy} onClick={() => void remove(source)}><Trash2 size={14} /></button>
                  </div>
                </article>
              );
            })}
            {visibleSources.length === 0 && <div className="empty-state compact"><BookOpen size={23} /><strong>{query ? "No matching knowledge sources" : "No knowledge sources loaded"}</strong><p>{query ? "Try a different source name or citation." : canMutate ? "Upload a document or add a public URL for cited analyst chat." : "Connect Core and select a project to add sources."}</p></div>}
          </div>
        </section>
        <aside className="panel knowledge-policy">
          <span className="policy-illustration"><ShieldAlert size={28} /></span><h2>Retrieval safety</h2><p>Sources are treated as untrusted data.</p>
          <details className="knowledge-safety-details">
            <summary>How retrieval stays bounded</summary>
            <ul><li>Every chunk keeps its source identity</li><li>Cloud retrieval requires operator consent</li><li>Local-only sources stay local</li></ul>
          </details>
        </aside>
      </div>
      {addingUrl && <div className="dialog-backdrop"><form className="provider-dialog" role="dialog" aria-modal="true" aria-labelledby="knowledge-url-dialog-title" onSubmit={(event) => void addUrl(event)}><header><div><small>Public web source</small><h2 id="knowledge-url-dialog-title">Add source from URL</h2></div><button className="icon-button subtle" type="button" aria-label="Close URL source dialog" disabled={uploading} onClick={() => { setAddingUrl(false); setSourceUrl(""); }}><X size={17} /></button></header><p className="provider-dialog-note">Nebula fetches the page once, stores an immutable copy, and indexes that copy. Redirects and every resolved address must remain on the public internet.</p><label>URL<input required autoFocus type="url" inputMode="url" maxLength={2048} placeholder="https://docs.example.com/guide" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} /></label><footer><button className="button secondary" type="button" disabled={uploading} onClick={() => { setAddingUrl(false); setSourceUrl(""); }}>Cancel</button><button className="button primary" type="submit" disabled={uploading || !sourceUrl.trim()}>{uploading ? <LoaderCircle className="spin" size={15} /> : <Link2 size={15} />} {uploading ? "Fetching and indexing…" : "Add URL source"}</button></footer></form></div>}
      {selected && <aside className="resource-inspector" role="complementary" aria-labelledby="knowledge-detail-title"><header><div><small>{sourceType(selected)}</small><h2 id="knowledge-detail-title">{selected.name}</h2></div><button className="icon-button subtle" type="button" aria-label="Close knowledge details" onClick={() => setSelected(undefined)}><X size={17} /></button></header><dl className="resource-details"><div><dt>Status</dt><dd>{selected.status}</dd></div><div><dt>Chunks</dt><dd>{selected.documentCount || "Not indexed"}</dd></div><div><dt>Citation</dt><dd>{selected.citation || selected.name}</dd></div>{typeof selected.metadata.sourceUrl === "string" && <div><dt>Source URL</dt><dd>{selected.metadata.sourceUrl}</dd></div>}<div><dt>Updated</dt><dd>{displayTime(selected.updatedAt)}</dd></div><div><dt>Source type</dt><dd>{sourceType(selected)}</dd></div></dl><section><h3>Retrieval boundary</h3><p>Content is untrusted data and cannot grant tools, expand scope, or modify system policy.</p></section><div className="inspector-actions"><button className="button secondary full" type="button" disabled={!canMutate || busyIds.has(selected.id)} onClick={() => void reindex(selected)}><RefreshCw size={14} /> Reindex source</button></div></aside>}
    </div>
  );
}
