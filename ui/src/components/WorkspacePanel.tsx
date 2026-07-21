import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent } from "react";
import { Activity, AlertTriangle, Download, File, FileCheck2, Folder, Link2, MessageSquareText, RefreshCw, SquareTerminal, Trash2, Upload, X } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { WorkspaceEntry, WorkspacePreview, WorkspaceResetStatus } from "../api/types";
import { useConfirmation } from "./DialogSystem";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { WorkspaceEntryContextMenu, type WorkspaceEntryMenuState } from "./WorkspaceEntryContextMenu";

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
  onOpenTerminal?: () => void;
  onOpenActivity?: () => void;
}

function sizeLabel(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

export function WorkspacePanel({ api, engagementId, engagementName, onUseWithAssistant, onOpenTerminal, onOpenActivity }: WorkspacePanelProps) {
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
  const [resetStatus, setResetStatus] = useState<WorkspaceResetStatus>();
  const [resetStatusLoading, setResetStatusLoading] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState<{ name: string; path: string }>();
  const [entryMenu, setEntryMenu] = useState<WorkspaceEntryMenuState>();
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
      void logCaughtDiagnostic("interface.workspace_panel.caught_failure_01", "A handled interface operation failed.", loadError, "workspace_panel");
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

  const loadResetStatus = useCallback(async (signal?: AbortSignal) => {
    setResetStatusLoading(true);
    try {
      setResetStatus(await api.workspaceResetStatus(engagementId, signal));
    } catch (statusError) {
      void logCaughtDiagnostic("interface.workspace_panel.reset_status_failed", "Workspace reset readiness could not be checked.", statusError, "workspace_panel");
      if (!signal?.aborted) setError(statusError instanceof Error ? statusError.message : "Could not check whether the workspace can be reset.");
    } finally {
      if (!signal?.aborted) setResetStatusLoading(false);
    }
  }, [api, engagementId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadResetStatus(controller.signal);
    return () => controller.abort();
  }, [loadResetStatus]);

  useEffect(() => {
    if (!resetStatus || resetStatus.canReset) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => void loadResetStatus(controller.signal), 2_000);
    return () => { window.clearInterval(timer); controller.abort(); };
  }, [loadResetStatus, resetStatus?.canReset]);

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
        void logCaughtDiagnostic("interface.workspace_panel.caught_failure_02", "A handled interface operation failed.", uploadError, "workspace_panel");
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
      void logCaughtDiagnostic("interface.workspace_panel.caught_failure_03", "A handled interface operation failed.", uploadError, "workspace_panel");
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
      void logCaughtDiagnostic("interface.workspace_panel.caught_failure_04", "A handled interface operation failed.", previewError, "workspace_panel");
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
      void logCaughtDiagnostic("interface.workspace_panel.caught_failure_05", "A handled interface operation failed.", downloadError, "workspace_panel");
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
      void logCaughtDiagnostic("interface.workspace_panel.caught_failure_06", "A handled interface operation failed.", promoteError, "workspace_panel");
      setError(promoteError instanceof Error ? promoteError.message : "Could not promote the file.");
    }
  };

  const reset = async () => {
    if (resetName !== engagementName) return;
    try {
      const latest = await api.workspaceResetStatus(engagementId);
      setResetStatus(latest);
      if (!latest.canReset) return;
    } catch (statusError) {
      void logCaughtDiagnostic("interface.workspace_panel.reset_preflight_failed", "Workspace reset readiness could not be confirmed.", statusError, "workspace_panel");
      setError(statusError instanceof Error ? statusError.message : "Could not confirm that the workspace is ready to reset.");
      return;
    }
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
      await loadResetStatus();
    } catch (resetError) {
      void logCaughtDiagnostic("interface.workspace_panel.caught_failure_07", "A handled interface operation failed.", resetError, "workspace_panel");
      setError(resetError instanceof Error ? resetError.message : "Could not reset the workspace.");
    }
  };

  const navigateCrumb = (index: number) => setPath(crumbs.slice(0, index + 1).join("/"));

  const copyPath = async (entry: WorkspaceEntry) => {
    try {
      await navigator.clipboard.writeText(`/workspace/${entry.path}`);
      setNotice(`Copied /workspace/${entry.path}.`);
    } catch (copyError) {
      void logCaughtDiagnostic("interface.workspace_panel.copy_path_failed", "A workspace path could not be copied.", copyError, "workspace_panel");
      setError(copyError instanceof Error ? copyError.message : "Could not copy the file path.");
    }
  };

  const copyContents = async (entry: WorkspaceEntry) => {
    try {
      const blob = await api.downloadWorkspaceFile(engagementId, entry.path);
      if (blob.size > 1024 * 1024) throw new Error("Copy to clipboard is limited to 1 MiB. Download larger files instead.");
      const bytes = new Uint8Array(await blob.arrayBuffer());
      const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      if (text.includes("\0")) throw new Error("Binary file contents cannot be copied as text.");
      await navigator.clipboard.writeText(text);
      setNotice(`Copied the contents of ${entry.name}.`);
    } catch (copyError) {
      void logCaughtDiagnostic("interface.workspace_panel.copy_contents_failed", "Workspace file contents could not be copied.", copyError, "workspace_panel");
      setError(copyError instanceof Error ? copyError.message : "Could not copy file contents.");
    }
  };

  const renameEntry = async (entry: WorkspaceEntry, newName: string) => {
    try {
      const result = await api.renameWorkspaceEntry(engagementId, entry.path, newName);
      if (selected?.path === entry.path) {
        setSelected({ ...entry, path: result.path, name: newName });
        setPreview(undefined);
      }
      setNotice(`Renamed ${entry.name} to ${newName}.`);
      await load(0);
    } catch (renameError) {
      void logCaughtDiagnostic("interface.workspace_panel.rename_failed", "A workspace entry could not be renamed.", renameError, "workspace_panel");
      setError(renameError instanceof Error ? renameError.message : "Could not rename the workspace entry.");
    }
  };

  const deleteEntry = async (entry: WorkspaceEntry) => {
    const approved = await confirm({
      title: `Delete ${entry.name}?`,
      message: entry.kind === "directory" ? "Only an empty folder can be deleted. This cannot be undone." : "This removes the scratch workspace entry. Promoted evidence remains unchanged.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!approved) return;
    try {
      await api.deleteWorkspaceEntry(engagementId, entry.path);
      if (selected?.path === entry.path) { setSelected(undefined); setPreview(undefined); }
      setNotice(`Deleted ${entry.name}.`);
      await load(0);
    } catch (deleteError) {
      void logCaughtDiagnostic("interface.workspace_panel.delete_failed", "A workspace entry could not be deleted.", deleteError, "workspace_panel");
      setError(deleteError instanceof Error ? deleteError.message : "Could not delete the workspace entry.");
    }
  };

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
      {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
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
          {entries.map((entry) => <button type="button" title={`${entry.path} · Right-click for actions`} className={selected?.path === entry.path ? "active" : undefined} disabled={entry.kind === "other"} onContextMenu={(event) => { event.preventDefault(); setEntryMenu({ entry, x: event.clientX, y: event.clientY }); }} onClick={() => void openEntry(entry)} key={entry.path}>{entry.kind === "directory" ? <Folder size={16} /> : entry.kind === "symlink" ? <Link2 size={16} /> : <File size={16} />}<span><strong>{entry.name}</strong><small>{entry.kind} · {sizeLabel(entry.size)} · {new Date(entry.modifiedAt).toLocaleString()}</small></span></button>)}
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
        {resetStatus && !resetStatus.canReset && <div className="callout workspace-reset-blocker" role="status"><AlertTriangle size={18} /><div><strong>Workspace is in use</strong><p>{resetStatus.detail}</p></div>{resetStatus.activeTerminalCount > 0 && <button className="button secondary" type="button" onClick={onOpenTerminal}><SquareTerminal size={14} /> Open Terminal</button>}{resetStatus.activeExecutionCount > 0 && <button className="button secondary" type="button" onClick={onOpenActivity}><Activity size={14} /> View Activity</button>}</div>}
        <button className="button danger" type="button" disabled={resetName !== engagementName || resetStatusLoading || resetStatus?.canReset !== true} onClick={() => void reset()}>{resetStatusLoading ? "Checking…" : "Reset workspace"}</button>
      </section>
      {entryMenu && <WorkspaceEntryContextMenu menu={entryMenu} onClose={() => setEntryMenu(undefined)} onCopyPath={copyPath} onCopyContents={copyContents} onRename={renameEntry} onDelete={deleteEntry} />}
    </div>
  );
}
