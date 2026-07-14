import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent } from "react";
import { Download, File, FileCheck2, Folder, Link2, MessageSquareText, RefreshCw, Trash2, Upload, X } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { WorkspaceEntry, WorkspacePreview } from "../api/types";
import { useConfirmation } from "./DialogSystem";

interface WorkspacePanelProps {
  api: ApiClient;
  engagementId: string;
  engagementName: string;
  onUseWithAssistant?: (context: {
    text: string;
    sourceKind: "workspace_file";
    sourceId: string;
    sourceLabel: string;
    truncated: boolean;
  }) => void;
}

function sizeLabel(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

export function WorkspacePanel({ api, engagementId, engagementName, onUseWithAssistant }: WorkspacePanelProps) {
  const confirm = useConfirmation();
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [nextOffset, setNextOffset] = useState<number>();
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<WorkspaceEntry>();
  const [preview, setPreview] = useState<WorkspacePreview>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [resetName, setResetName] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState<{ name: string; path: string }>();
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadAbortRef = useRef<AbortController | undefined>(undefined);
  const crumbs = useMemo(() => path ? path.split("/") : [], [path]);

  const load = useCallback(async (offset = 0, signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const listing = await api.listWorkspace(engagementId, path, offset, signal);
      setEntries((current) => offset ? [...current, ...listing.entries] : listing.entries);
      setNextOffset(listing.nextOffset);
      setTotal(listing.total);
    } catch (loadError) {
      if (!signal?.aborted) setError(loadError instanceof Error ? loadError.message : "Could not list the workspace.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, engagementId, path]);

  useEffect(() => {
    setSelected(undefined);
    setPreview(undefined);
    const controller = new AbortController();
    void load(0, controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => () => uploadAbortRef.current?.abort(), []);

  const uploadFile = async (file: File) => {
    if (uploading || !file.name) return;
    const destination = path ? `${path}/${file.name}` : file.name;
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    setUploading({ name: file.name, path: destination });
    setError(undefined);
    setNotice(undefined);
    try {
      let result;
      try {
        result = await api.uploadWorkspaceFile(engagementId, destination, file, false, controller.signal);
      } catch (uploadError) {
        if (!(uploadError instanceof ApiError) || uploadError.status !== 409 || controller.signal.aborted) throw uploadError;
        const approved = await confirm({
          title: `Replace ${file.name}?`,
          message: <span>A file already exists at <code>/workspace/{destination}</code>. Replace it atomically with the selected file?</span>,
          confirmLabel: "Replace file",
          tone: "danger",
        });
        if (!approved) {
          setNotice(`${file.name} was not uploaded.`);
          return;
        }
        result = await api.uploadWorkspaceFile(engagementId, destination, file, true, controller.signal);
      }
      setNotice(`${result.overwritten ? "Replaced" : "Uploaded"} ${result.path} · ${sizeLabel(result.size)} · SHA-256 ${result.sha256}.`);
      await load(0);
    } catch (uploadError) {
      if (controller.signal.aborted || (uploadError instanceof DOMException && uploadError.name === "AbortError")) {
        setNotice(`Upload of ${file.name} was cancelled.`);
      } else {
        setError(uploadError instanceof Error ? uploadError.message : "Could not upload the file.");
      }
    } finally {
      if (uploadAbortRef.current === controller) uploadAbortRef.current = undefined;
      setUploading(undefined);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  };

  const dropFile = (event: ReactDragEvent<HTMLElement>) => {
    event.preventDefault();
    setDragActive(false);
    const file = event.dataTransfer.files.item(0);
    if (file) void uploadFile(file);
  };

  const openEntry = async (entry: WorkspaceEntry) => {
    setSelected(entry);
    setPreview(undefined);
    setNotice(undefined);
    if (entry.kind === "directory") {
      setPath(entry.path);
      return;
    }
    if (entry.kind !== "file") return;
    try {
      setPreview(await api.previewWorkspaceFile(engagementId, entry.path));
    } catch (previewError) {
      setError(previewError instanceof Error ? previewError.message : "This file cannot be previewed.");
    }
  };

  const download = async () => {
    if (!selected || selected.kind !== "file") return;
    try {
      const blob = await api.downloadWorkspaceFile(engagementId, selected.path);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = selected.name;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : "Could not download the file.");
    }
  };

  const promote = async () => {
    if (!selected || selected.kind !== "file") return;
    const approved = await confirm({
      title: "Preserve exact file as evidence?",
      message: <span>Nebula will copy <code>{selected.path}</code> into immutable artifact storage and record operator-attributed evidence.</span>,
      confirmLabel: "Preserve as Evidence",
    });
    if (!approved) return;
    try {
      const evidence = await api.promoteWorkspaceFile(engagementId, selected.path, selected.name);
      setNotice(`Promoted as evidence ${evidence.id.slice(0, 8)} with SHA-256 ${evidence.sha256}.`);
    } catch (promoteError) {
      setError(promoteError instanceof Error ? promoteError.message : "Could not promote the file.");
    }
  };

  const reset = async () => {
    if (resetName !== engagementName) return;
    const approved = await confirm({
      title: "Reset the project workspace?",
      message: <span>This removes scratch files without following symlinks. Promoted artifacts and evidence remain. You entered <strong>{engagementName}</strong>.</span>,
      confirmLabel: "Reset workspace",
      tone: "danger",
    });
    if (!approved) return;
    try {
      const result = await api.resetWorkspace(engagementId, resetName);
      setResetName("");
      setPath("");
      setSelected(undefined);
      setPreview(undefined);
      setNotice(`Removed ${result.removedEntries} workspace entr${result.removedEntries === 1 ? "y" : "ies"}. Promoted evidence was retained.`);
      await load(0);
    } catch (resetError) {
      setError(resetError instanceof Error ? resetError.message : "Could not reset the workspace.");
    }
  };

  const navigateCrumb = (index: number) => setPath(crumbs.slice(0, index + 1).join("/"));

  return (
    <div className="workspace-browser">
      <header className="workspace-browser-toolbar">
        <nav aria-label="Workspace path"><button type="button" onClick={() => setPath("")}>/workspace</button>{crumbs.map((crumb, index) => <span key={`${crumb}-${index}`}>/<button type="button" onClick={() => navigateCrumb(index)}>{crumb}</button></span>)}</nav>
        <div>
          <input ref={uploadInputRef} className="sr-only" type="file" aria-label="Choose workspace file" onChange={(event) => { const file = event.target.files?.[0]; if (file) void uploadFile(file); }} />
          {uploading ? <button className="button quiet" type="button" onClick={() => uploadAbortRef.current?.abort()}><X size={14} /> Cancel upload</button> : <button className="button primary" type="button" onClick={() => uploadInputRef.current?.click()}><Upload size={14} /> Upload file</button>}
          <button className="button quiet" type="button" disabled={loading} onClick={() => void load(0)}><RefreshCw className={loading ? "spin" : undefined} size={14} /> Refresh</button>
        </div>
      </header>
      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="workspace-notice" role="status">{notice}</p>}
      <div className="workspace-browser-layout">
        <section
          className={`workspace-entry-list workspace-drop-zone${dragActive ? " dragging" : ""}`}
          aria-label="Workspace entries"
          aria-busy={Boolean(uploading)}
          onDragEnter={(event) => { event.preventDefault(); setDragActive(true); }}
          onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; setDragActive(true); }}
          onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragActive(false); }}
          onDrop={dropFile}
        >
          <header><span>{uploading ? `Uploading ${uploading.name}…` : `${total} entr${total === 1 ? "y" : "ies"}`}</span><small>Drop a file here · symlinks are inert</small></header>
          {dragActive && <div className="workspace-drop-prompt" role="status"><Upload size={22} /><strong>Upload to /workspace/{path}</strong><span>Drop to copy the file into this folder</span></div>}
          {entries.map((entry) => <button type="button" title={entry.path} className={selected?.path === entry.path ? "active" : undefined} disabled={entry.kind === "other"} onClick={() => void openEntry(entry)} key={entry.path}>{entry.kind === "directory" ? <Folder size={16} /> : entry.kind === "symlink" ? <Link2 size={16} /> : <File size={16} />}<span><strong>{entry.name}</strong><small>{entry.kind} · {sizeLabel(entry.size)} · {new Date(entry.modifiedAt).toLocaleString()}</small></span></button>)}
          {!entries.length && !loading && <div className="empty-state compact"><Folder size={21} /><strong>Workspace is empty</strong><p>Files created by reviewed executions persist here until reset.</p></div>}
          {nextOffset !== undefined && <button className="button quiet" type="button" disabled={loading} onClick={() => void load(nextOffset)}>Load more</button>}
        </section>
        <section className={`workspace-file-preview${selected?.kind === "file" ? "" : " is-empty"}`}>
          {selected?.kind === "symlink" ? <div className="empty-state"><Link2 size={22} /><strong>Inert symbolic link</strong><p>Nebula will not follow, preview, download, or preserve this entry.</p></div> : selected?.kind === "file" ? <><header><div><h3>{selected.name}</h3><p>{selected.path} · {sizeLabel(selected.size)}</p></div><div><button className="button quiet" type="button" onClick={() => void download()}><Download size={13} /> Download</button>{preview && onUseWithAssistant && <button className="button secondary" type="button" onClick={() => onUseWithAssistant({ text: preview.text, sourceKind: "workspace_file", sourceId: selected.path, sourceLabel: selected.name, truncated: preview.truncated })}><MessageSquareText size={13} /> Use with Assistant</button>}<button className="button primary" type="button" onClick={() => void promote()}><FileCheck2 size={13} /> Preserve as Evidence</button></div></header>{preview ? <><pre data-selection-source-kind="workspace_file" data-selection-source-id={selected.path} data-selection-source-label={selected.name}>{preview.text}</pre>{preview.truncated && <p>Preview stops at 256 KiB. Download or preserve uses exact full bytes.</p>}</> : <div className="empty-state compact"><File size={21} /><strong>No plain-text preview</strong><p>The file may be binary, non-UTF-8, or still loading.</p></div>}</> : <div className="empty-state"><Folder size={23} /><strong>Select a workspace file</strong><p>Preview is read-only and bounded to 256 KiB.</p></div>}
        </section>
      </div>
      <section className="workspace-reset panel">
        <div><Trash2 size={18} /><span><strong>Reset scratch workspace</strong><small>Application-enforced limits: 5 GiB allocated data, 50,000 entries, 1 GiB per file. Promoted artifacts survive reset.</small></span></div>
        <label>Type <strong>{engagementName}</strong><input value={resetName} onChange={(event) => setResetName(event.target.value)} /></label>
        <button className="button danger" type="button" disabled={resetName !== engagementName} onClick={() => void reset()}>Reset workspace</button>
      </section>
    </div>
  );
}
