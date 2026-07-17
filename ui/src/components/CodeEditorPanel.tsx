import { useCallback, useEffect, useMemo, useState } from "react";
import { Braces, File, FilePlus2, Folder, LoaderCircle, RefreshCw, RotateCcw, Save, ShieldAlert } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { WorkspaceEntry } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { useWorkbenchEditor, type WorkbenchEditorBuffer } from "../state/WorkbenchEditorContext";
import { CodeMirrorSurface, languageLabelForPath } from "./CodeMirrorSurface";
import { useConfirmation } from "./DialogSystem";

const MAX_EDITOR_BYTES = 1024 * 1024;

interface CodeEditorPanelProps {
  active: boolean;
  api: ApiClient;
  engagementId: string;
}

function validWorkspacePath(path: string): boolean {
  return Boolean(path)
    && path.length <= 4096
    && !path.startsWith("/")
    && !path.includes("\\")
    && path.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

async function decodeWorkspaceFile(blob: Blob): Promise<{ content: string; sha256: string }> {
  if (blob.size > MAX_EDITOR_BYTES) throw new Error("This file is larger than the editor's 1 MiB text limit.");
  const bytes = await blob.arrayBuffer();
  const payload = new Uint8Array(bytes);
  const content = new TextDecoder("utf-8", { fatal: true }).decode(payload);
  if (content.includes("\0")) throw new Error("This file appears to be binary and cannot be edited as text.");
  const digest = await crypto.subtle.digest("SHA-256", payload);
  return {
    content,
    sha256: [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join(""),
  };
}

function nextUntitledPath(directory: string, entries: WorkspaceEntry[]): string {
  const names = new Set(entries.map((entry) => entry.name));
  let name = "untitled.txt";
  let suffix = 2;
  while (names.has(name)) name = `untitled-${suffix++}.txt`;
  return directory ? `${directory}/${name}` : name;
}

export function CodeEditorPanel({ active, api, engagementId }: CodeEditorPanelProps) {
  const confirm = useConfirmation();
  const { buffer, setBuffer } = useWorkbenchEditor(engagementId);
  const [directory, setDirectory] = useState("");
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [nextOffset, setNextOffset] = useState<number>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const dirty = Boolean(buffer && (!buffer.existing || buffer.content !== buffer.savedContent));
  const crumbs = useMemo(() => directory ? directory.split("/") : [], [directory]);

  const load = useCallback(async (offset = 0, signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const listing = await api.listWorkspace(engagementId, directory, offset, signal);
      setEntries((current) => offset ? [...current, ...listing.entries] : listing.entries);
      setNextOffset(listing.nextOffset);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.code_editor.list", "The code editor could not list workspace files.", caughtError, "code_editor");
      if (!signal?.aborted) setError(caughtError instanceof Error ? caughtError.message : "Could not list workspace files.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, directory, engagementId]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(0, controller.signal);
    return () => controller.abort();
  }, [active, load]);

  useEffect(() => {
    setDirectory("");
    setEntries([]);
    setError(undefined);
    setNotice(undefined);
    setConflict(false);
  }, [engagementId]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const canReplaceBuffer = async () => !dirty || confirm({
    title: "Discard unsaved changes?",
    message: "Your editor changes have not been saved to /workspace.",
    confirmLabel: "Discard changes",
    tone: "danger",
  });

  const openFile = async (entry: WorkspaceEntry, skipDirtyCheck = false) => {
    if (!skipDirtyCheck && !await canReplaceBuffer()) return;
    setLoading(true);
    setError(undefined);
    setNotice(undefined);
    setConflict(false);
    try {
      const decoded = await decodeWorkspaceFile(await api.downloadWorkspaceFile(engagementId, entry.path));
      setBuffer({
        content: decoded.content,
        expectedSha256: decoded.sha256,
        existing: true,
        filePath: entry.path,
        savedContent: decoded.content,
      });
      setCursor({ line: 1, column: 1 });
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.code_editor.open", "The code editor could not open a workspace file.", caughtError, "code_editor");
      setError(caughtError instanceof Error ? caughtError.message : "Could not open this file.");
    } finally {
      setLoading(false);
    }
  };

  const chooseEntry = (entry: WorkspaceEntry) => {
    if (entry.kind === "directory") setDirectory(entry.path);
    else if (entry.kind === "file") void openFile(entry);
  };

  const createFile = async () => {
    if (!await canReplaceBuffer()) return;
    setBuffer({
      content: "",
      existing: false,
      filePath: nextUntitledPath(directory, entries),
      savedContent: "",
    });
    setConflict(false);
    setError(undefined);
    setNotice("Choose a workspace-relative path, then start typing.");
    setCursor({ line: 1, column: 1 });
  };

  const save = useCallback(async (force = false) => {
    if (!buffer || saving) return;
    const path = buffer.filePath.trim();
    if (!validWorkspacePath(path)) {
      setError("Enter a workspace-relative file path without empty, . or .. segments.");
      return;
    }
    const payload = new Blob([buffer.content], { type: "text/plain;charset=utf-8" });
    if (payload.size > MAX_EDITOR_BYTES) {
      setError("Editor files may not exceed 1 MiB when encoded as UTF-8.");
      return;
    }
    setSaving(true);
    setError(undefined);
    setNotice(undefined);
    setConflict(false);
    try {
      const result = await api.uploadWorkspaceFile(
        engagementId,
        path,
        payload,
        buffer.existing,
        undefined,
        force ? undefined : buffer.expectedSha256,
      );
      const saved: WorkbenchEditorBuffer = {
        content: buffer.content,
        expectedSha256: result.sha256,
        existing: true,
        filePath: result.path,
        savedContent: buffer.content,
      };
      setBuffer(saved);
      setNotice(`Saved /workspace/${result.path}. Use it from Terminal when you're ready.`);
      const parent = result.path.includes("/") ? result.path.slice(0, result.path.lastIndexOf("/")) : "";
      if (parent === directory) await load(0);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.code_editor.save", "The code editor could not save a workspace file.", caughtError, "code_editor");
      if (caughtError instanceof ApiError && caughtError.status === 412) {
        setConflict(true);
        setError("This file changed in Terminal or another workspace client after you opened it.");
      } else if (caughtError instanceof ApiError && caughtError.status === 409 && !buffer.existing) {
        setError("A workspace file already exists at this path. Choose another filename.");
      } else {
        setError(caughtError instanceof Error ? caughtError.message : "Could not save this file.");
      }
    } finally {
      setSaving(false);
    }
  }, [api, buffer, directory, engagementId, load, saving, setBuffer]);

  const reloadConflict = async () => {
    if (!buffer) return;
    const approved = await confirm({
      title: "Reload the workspace file?",
      message: "This discards your unsaved editor changes and loads the version currently in /workspace.",
      confirmLabel: "Reload file",
      tone: "danger",
    });
    if (approved) await openFile({ path: buffer.filePath, name: buffer.filePath.split("/").at(-1) ?? buffer.filePath, kind: "file", size: 0, modifiedAt: new Date().toISOString() }, true);
  };

  const forceOverwrite = async () => {
    const approved = await confirm({
      title: "Overwrite the newer workspace file?",
      message: "This replaces the version changed outside the editor with your current draft.",
      confirmLabel: "Overwrite file",
      tone: "danger",
    });
    if (approved) await save(true);
  };

  const updateBuffer = (changes: Partial<WorkbenchEditorBuffer>) => {
    if (buffer) setBuffer({ ...buffer, ...changes });
  };

  return <div className="code-editor-panel">
    <aside className="code-editor-sidebar" aria-label="Editor files">
      <header><div><Braces size={16} /><strong>Workspace</strong></div><div><button className="icon-button subtle" type="button" aria-label="New file" onClick={() => void createFile()}><FilePlus2 size={15} /></button><button className="icon-button subtle" type="button" aria-label="Refresh editor files" disabled={loading} onClick={() => void load(0)}><RefreshCw className={loading ? "spin" : undefined} size={14} /></button></div></header>
      <nav className="code-editor-crumbs" aria-label="Editor workspace path"><button type="button" onClick={() => setDirectory("")}>/workspace</button>{crumbs.map((crumb, index) => <span key={`${crumb}-${index}`}>/<button type="button" onClick={() => setDirectory(crumbs.slice(0, index + 1).join("/"))}>{crumb}</button></span>)}</nav>
      <div className="code-editor-files">{entries.map((entry) => <button type="button" className={buffer?.existing && buffer.filePath === entry.path ? "active" : undefined} disabled={entry.kind === "symlink" || entry.kind === "other"} onClick={() => chooseEntry(entry)} key={entry.path}>{entry.kind === "directory" ? <Folder size={15} /> : <File size={15} />}<span><strong>{entry.name}</strong><small>{entry.kind === "file" ? `${entry.size.toLocaleString()} bytes` : entry.kind}</small></span></button>)}{!entries.length && !loading && <div className="empty-state compact"><Folder size={20} /><strong>No files here</strong><p>Create a text file or use Terminal to populate /workspace.</p></div>}{nextOffset !== undefined && <button className="button quiet" type="button" onClick={() => void load(nextOffset)}>Load more</button>}</div>
    </aside>
    <section className={`code-editor-main${buffer ? "" : " is-empty"}`}>
      {buffer ? <>
        <header className="code-editor-toolbar"><label><span className="sr-only">File path</span><span aria-hidden="true">/workspace/</span><input aria-label="File path" value={buffer.filePath} readOnly={buffer.existing} spellCheck={false} onChange={(event) => updateBuffer({ filePath: event.target.value })} /></label><span className={`code-editor-dirty${dirty ? " dirty" : ""}`} aria-live="polite">{dirty ? "Unsaved" : "Saved"}</span><button className="button primary" type="button" disabled={saving || (!dirty && buffer.existing) || !validWorkspacePath(buffer.filePath.trim())} onClick={() => void save()}>{saving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />} {saving ? "Saving…" : "Save"}</button></header>
        {error && <DiagnosticErrorNotice error={error} fallback="The editor operation failed." compact />}{notice && <p className="workspace-notice" role="status">{notice}</p>}
        {conflict && <div className="code-editor-conflict" role="alert"><ShieldAlert size={17} /><span><strong>Newer workspace version detected</strong><small>Your draft is still open. Reload the Terminal version or overwrite it explicitly.</small></span><button className="button quiet" type="button" onClick={() => void reloadConflict()}><RotateCcw size={13} /> Reload</button><button className="button danger" type="button" onClick={() => void forceOverwrite()}>Force overwrite</button></div>}
        <CodeMirrorSurface active={active} filePath={buffer.filePath} value={buffer.content} onChange={(content) => updateBuffer({ content })} onCursorChange={(line, column) => setCursor({ line, column })} onSave={() => void save()} />
        <footer><span>{languageLabelForPath(buffer.filePath)}</span><span>Ln {cursor.line}, Col {cursor.column}</span><span>UTF-8 · spaces: 2</span><span>/workspace · Terminal execution</span></footer>
      </> : <><div className="empty-state"><Braces size={25} /><strong>Shared workspace editor</strong><p>Open or create a text file here, then run it from Terminal in /workspace using its interpreter.</p><button className="button primary" type="button" onClick={() => void createFile()}><FilePlus2 size={15} /> New file</button></div>{error && <DiagnosticErrorNotice error={error} fallback="The editor operation failed." compact />}</>}
    </section>
  </div>;
}
