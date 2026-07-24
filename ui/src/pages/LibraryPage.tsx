import { useMemo, useRef, useState, type ChangeEvent } from "react";
import {
  BookMarked,
  Code2,
  Database,
  Download,
  FileText,
  LoaderCircle,
  RefreshCw,
  Search,
  Trash2,
  Upload,
} from "lucide-react";
import type { LibraryItem } from "../api/types";
import { useConfirmation } from "../components/DialogSystem";
import { PageHeader } from "../components/PageHeader";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";

const MAX_ITEM_BYTES = 20 * 1024 * 1024;
const ACCEPTED_FILES = [
  ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".json", ".jsonl",
  ".ndjson", ".html", ".htm", ".pdf", ".docx", ".xlsx", ".py", ".sh",
  ".bash", ".zsh", ".js", ".jsx", ".ts", ".tsx", ".rb", ".go", ".rs",
  ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".pl", ".lua",
  ".sql", ".yaml", ".yml", ".toml", ".xml", ".ini", ".conf",
].join(",");

function encodeBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function displayTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function itemIcon(item: LibraryItem) {
  if (item.sourceType === "script") return Code2;
  if (["json", "jsonl", "csv", "xlsx"].includes(item.sourceType)) return Database;
  return FileText;
}

export function LibraryPage() {
  const confirm = useConfirmation();
  const {
    api,
    coreState,
    libraryItems,
    ingestLibraryItem,
    reindexLibraryItem,
    removeLibraryItem,
  } = useWorkspace();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [uploading, setUploading] = useState(false);
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const [message, setMessage] = useState<string>();
  const [error, setError] = useState<string>();
  const visibleItems = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return libraryItems;
    return libraryItems.filter((item) =>
      `${item.name} ${item.sourceType} ${item.citation ?? ""}`.toLowerCase().includes(needle),
    );
  }, [libraryItems, query]);

  const setBusy = (id: string, busy: boolean) => {
    setBusyIds((current) => {
      const next = new Set(current);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > MAX_ITEM_BYTES) {
      setError(`${file.name} is larger than the 20 MB Library limit.`);
      return;
    }
    setUploading(true);
    setError(undefined);
    setMessage(`Adding ${file.name} to the Library…`);
    try {
      await ingestLibraryItem({
        filename: file.name,
        mediaType: file.type || undefined,
        contentBase64: encodeBase64(await file.arrayBuffer()),
      });
      setMessage(`${file.name} is available to every project.`);
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.library_page.ingest_failed",
        "A Library item could not be added.",
        caughtError,
        "library_page",
      );
      setMessage(undefined);
      setError(caughtError instanceof Error ? caughtError.message : "Could not add the Library item.");
    } finally {
      setUploading(false);
    }
  };

  const reindex = async (item: LibraryItem) => {
    setBusy(item.id, true);
    setError(undefined);
    try {
      await reindexLibraryItem(item.id);
      setMessage(`${item.name} was reindexed.`);
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.library_page.reindex_failed",
        "A Library item could not be reindexed.",
        caughtError,
        "library_page",
      );
      setError(caughtError instanceof Error ? caughtError.message : `Could not reindex ${item.name}.`);
    } finally {
      setBusy(item.id, false);
    }
  };

  const remove = async (item: LibraryItem) => {
    if (!await confirm({
      title: `Remove ${item.name}?`,
      message: "It will stop appearing in retrieval for every project. The immutable repository artifact is retained.",
      confirmLabel: "Remove item",
      tone: "danger",
    })) return;
    setBusy(item.id, true);
    try {
      await removeLibraryItem(item.id);
      setMessage(`${item.name} was removed from the Library index.`);
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.library_page.remove_failed",
        "A Library item could not be removed.",
        caughtError,
        "library_page",
      );
      setError(caughtError instanceof Error ? caughtError.message : `Could not remove ${item.name}.`);
    } finally {
      setBusy(item.id, false);
    }
  };

  const download = async (item: LibraryItem) => {
    if (!api || !item.artifactId) return;
    setBusy(item.id, true);
    try {
      const blob = await api.getArtifactContent(item.artifactId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = item.metadata.filename || item.name;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.library_page.download_failed",
        "A Library artifact could not be downloaded.",
        caughtError,
        "library_page",
      );
      setError(caughtError instanceof Error ? caughtError.message : `Could not download ${item.name}.`);
    } finally {
      setBusy(item.id, false);
    }
  };

  const canMutate = coreState === "online";
  return (
    <div className="page knowledge-page library-page">
      <PageHeader
        title="Library"
        description="A reusable, local repository and semantic index shared across projects."
        actions={<>
          <input
            ref={inputRef}
            className="sr-only"
            type="file"
            aria-label="Choose Library item"
            accept={ACCEPTED_FILES}
            onChange={(event) => void upload(event)}
          />
          <button className="button primary" type="button" disabled={!canMutate || uploading} onClick={() => inputRef.current?.click()}>
            {uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
            {uploading ? "Indexing…" : "Add document or script"}
          </button>
        </>}
      />
      <section className="knowledge-model-status" role="note">
        <span className="metric-icon"><BookMarked size={18} /></span>
        <div><strong>Workspace-wide, local by design</strong><p>Files are stored as immutable artifacts. Chroma indexes bounded text chunks on this device; scripts are never executed.</p></div>
      </section>
      {message && <div className="knowledge-status" role="status">{message}</div>}
      {error && <DiagnosticErrorNotice error={error} fallback="The Library operation could not be completed." />}
      <section className="panel data-panel knowledge-sources">
        <header className="data-toolbar">
          <label className="search-field"><Search size={16} /><span className="sr-only">Search Library</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search the Library…" /></label>
          <span className="toolbar-summary">{libraryItems.length} reusable item{libraryItems.length === 1 ? "" : "s"}</span>
        </header>
        <div className="source-list">
          {visibleItems.map((item) => {
            const Icon = itemIcon(item);
            const busy = busyIds.has(item.id);
            return <article key={item.id}>
              <span className="source-icon"><Icon size={19} /></span>
              <div><h3 title={item.name}>{item.name}</h3><p>{item.sourceType === "script" ? "Executable source · indexed as text only" : item.metadata.mediaType || item.sourceType}</p></div>
              <span><strong>{item.documentCount || "—"}</strong><small>chunks</small></span>
              <span className={`source-state ${busy ? "indexing" : item.status}`}>{busy && <RefreshCw className="spin" size={13} />}{busy ? "working" : item.status}</span>
              <span className="source-updated">{displayTime(item.updatedAt)}</span>
              <div className="source-actions">
                <button className="icon-button subtle" type="button" title="Reindex item" aria-label={`Reindex ${item.name}`} disabled={!canMutate || busy} onClick={() => void reindex(item)}><RefreshCw size={14} /></button>
                <button className="icon-button subtle" type="button" title="Download original" aria-label={`Download ${item.name}`} disabled={!item.artifactId || busy} onClick={() => void download(item)}><Download size={14} /></button>
                <button className="icon-button subtle" type="button" title="Remove from Library" aria-label={`Remove ${item.name}`} disabled={!canMutate || busy} onClick={() => void remove(item)}><Trash2 size={14} /></button>
              </div>
            </article>;
          })}
          {visibleItems.length === 0 && <div className="empty-state compact"><BookMarked size={24} /><strong>{query ? "No matching Library items" : "Your Library is empty"}</strong><p>{query ? "Try a different name or type." : "Add a document or script once, then retrieve it from any project."}</p></div>}
        </div>
      </section>
    </div>
  );
}
