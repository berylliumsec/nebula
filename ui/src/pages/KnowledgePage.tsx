import { useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { useSearchParams } from "react-router-dom";
import {
  BookOpen,
  Database,
  Download,
  FileText,
  Globe2,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldAlert,
  Trash2,
  Upload,
} from "lucide-react";
import type { KnowledgeSource } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { useWorkspace } from "../state/WorkspaceContext";

const previewSources: KnowledgeSource[] = [
  { id: "preview-roe", engagementId: "preview", name: "Acme rules of engagement.pdf", sourceType: "document", status: "ready", citation: "Acme rules of engagement.pdf", documentCount: 84, createdAt: "2026-07-12T08:04:00Z", updatedAt: "2026-07-12T08:04:00Z", metadata: { filename: "Acme rules of engagement.pdf", mediaType: "application/pdf", chunkCount: 84 } },
  { id: "preview-owasp", engagementId: "preview", name: "OWASP Web Security Testing Guide", sourceType: "web", status: "ready", citation: "OWASP WSTG", documentCount: 1240, createdAt: "2026-07-10T09:00:00Z", updatedAt: "2026-07-10T09:00:00Z", metadata: { chunkCount: 1240 } },
  { id: "preview-scanner", engagementId: "preview", name: "Imported scanner observations", sourceType: "structured", status: "ready", documentCount: 386, createdAt: "2026-07-12T18:00:00Z", updatedAt: "2026-07-12T19:02:00Z", metadata: { chunkCount: 386 } },
];

const MAX_SOURCE_BYTES = 20 * 1024 * 1024;

function sourceIcon(source: KnowledgeSource) {
  if (source.sourceType.includes("web")) return Globe2;
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

export function KnowledgePage() {
  const [searchParams] = useSearchParams();
  const {
    api,
    coreState,
    engagement,
    ingestKnowledgeSource,
    knowledgeSources,
    previewMode,
    reindexKnowledgeSource,
    removeKnowledgeSource,
  } = useWorkspace();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [uploading, setUploading] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string>();
  const [error, setError] = useState<string>();
  const sources = previewMode ? previewSources : knowledgeSources;
  const requestedSourceId = searchParams.get("source");
  const visibleSources = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return sources;
    return sources.filter((source) => `${source.name} ${source.sourceType} ${source.citation ?? ""}`.toLowerCase().includes(needle));
  }, [query, sources]);

  useEffect(() => {
    if (!requestedSourceId) return;
    const requested = sources.find((source) => source.id === requestedSourceId);
    if (requested) setQuery(requested.name);
  }, [requestedSourceId, sources]);

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
      setError("Create or select an engagement before adding a knowledge source.");
      return;
    }
    setUploading(true);
    setError(undefined);
    setStatusMessage(`Reading ${file.name}…`);
    try {
      const contentBase64 = encodeBase64(await file.arrayBuffer());
      setStatusMessage(`Indexing ${file.name}…`);
      await ingestKnowledgeSource({
        engagementId: engagement.id,
        filename: file.name,
        mediaType: file.type || undefined,
        contentBase64,
      });
      setStatusMessage(`${file.name} is ready for cited retrieval.`);
    } catch (uploadError) {
      setStatusMessage(undefined);
      setError(uploadError instanceof Error ? uploadError.message : "Could not ingest the selected source.");
    } finally {
      setUploading(false);
    }
  };

  const reindex = async (source: KnowledgeSource) => {
    setSourceBusy(source.id, true);
    setError(undefined);
    try {
      await reindexKnowledgeSource(source.id);
      setStatusMessage(`${source.name} was reindexed.`);
    } catch (reindexError) {
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
    const results = await Promise.allSettled(candidates.map((source) => reindexKnowledgeSource(source.id)));
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
    if (!window.confirm(`Remove “${source.name}” from retrieval? The immutable source artifact will be retained.`)) return;
    setSourceBusy(source.id, true);
    setError(undefined);
    try {
      await removeKnowledgeSource(source.id);
      setStatusMessage(`${source.name} was removed from retrieval.`);
    } catch (removeError) {
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
      setError(downloadError instanceof Error ? downloadError.message : `Could not download ${source.name}.`);
    } finally {
      setSourceBusy(source.id, false);
    }
  };

  const canMutate = coreState === "online" && Boolean(engagement);
  return (
    <div className="page knowledge-page">
      <PageHeader
        eyebrow="Cited context"
        title="Knowledge"
        description="Ingested sources are visible, removable, reindexable, and isolated from executable instructions."
        actions={<>
          <input ref={inputRef} className="sr-only" type="file" aria-label="Choose knowledge source" accept=".txt,.md,.markdown,.rst,.log,.csv,.json,.jsonl,.ndjson,.html,.htm,.pdf,.docx,text/plain,text/markdown,text/x-markdown,text/csv,application/csv,application/json,application/jsonl,application/x-jsonlines,application/x-ndjson,text/html,application/xhtml+xml,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={(event) => void uploadFile(event)} />
          <button className="button primary" type="button" disabled={!canMutate || uploading} onClick={() => inputRef.current?.click()}>{uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />} {uploading ? "Adding source…" : "Add source"}</button>
        </>}
      />
      {statusMessage && <div className="knowledge-status" role="status">{uploading && <LoaderCircle className="spin" size={15} />}{statusMessage}</div>}
      {error && <div className="knowledge-status error" role="alert"><ShieldAlert size={15} />{error}</div>}
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
                  <div><h3>{source.name}</h3><p>{sourceType(source)}</p></div>
                  <span><strong>{source.documentCount || "—"}</strong><small>chunks</small></span>
                  <span className={`source-state ${busy ? "indexing" : source.status}`}>{busy && <RefreshCw className="spin" size={13} />}{busy ? "working" : source.status}</span>
                  <span className="source-updated">{displayTime(source.updatedAt)}</span>
                  <div className="source-actions">
                    <button className="icon-button subtle" type="button" title="Reindex source" aria-label={`Reindex ${source.name}`} disabled={!canMutate || busy} onClick={() => void reindex(source)}><RefreshCw size={14} /></button>
                    <button className="icon-button subtle" type="button" title="Download original" aria-label={`Download ${source.name}`} disabled={!source.artifactId || !api || busy || previewMode} onClick={() => void download(source)}><Download size={14} /></button>
                    <button className="icon-button subtle" type="button" title="Remove from retrieval" aria-label={`Remove ${source.name}`} disabled={!canMutate || busy} onClick={() => void remove(source)}><Trash2 size={14} /></button>
                  </div>
                </article>
              );
            })}
            {visibleSources.length === 0 && <div className="empty-state compact"><BookOpen size={23} /><strong>{query ? "No matching knowledge sources" : "No knowledge sources loaded"}</strong><p>{query ? "Try a different source name or citation." : canMutate ? "Add a document to make it available for cited analyst chat." : "Connect Core and select an engagement to add sources."}</p></div>}
          </div>
        </section>
        <aside className="panel knowledge-policy">
          <span className="policy-illustration"><ShieldAlert size={28} /></span><h2>Retrieval boundary</h2><p>Knowledge content is treated as untrusted data. Instructions found inside sources cannot grant tools, expand scope, or alter system policy.</p>
          <ul><li>Source identity included with every chunk</li><li>Cloud retrieval stays off until the operator explicitly consents</li><li>Local-only sources are never sent to cloud providers</li><li>Retrieval index can be rebuilt from authoritative data</li></ul>
        </aside>
      </div>
    </div>
  );
}
