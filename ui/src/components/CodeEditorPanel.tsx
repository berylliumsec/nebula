import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Braces, Columns2, File, FileCheck2, FilePlus2, Folder, GitBranch, LoaderCircle, MessageSquareText, MoreHorizontal, Play, RefreshCw, RotateCcw, Save, Search, Settings2, ShieldAlert, Sparkles, TextSearch, X } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { ExecutionLanguage, WorkspaceEntry, WorkspaceSearchMatch } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { newEditorBufferId, useWorkbenchEditor, type WorkbenchEditorBuffer } from "../state/WorkbenchEditorContext";
import { CodeMirrorSurface, languageLabelForPath } from "./CodeMirrorSurface";
import { useConfirmation } from "./DialogSystem";
import { WorkspaceEntryContextMenu, type WorkspaceEntryMenuState } from "./WorkspaceEntryContextMenu";
import { InlineValidationNotice } from "./InlineValidationNotice";
import { AIWritingDialog } from "./AIWritingDialog";
import type { HarnessProfile, ProviderHealth, WritingTransformResponse } from "../api/types";
import { codeSuggestionRuntimeOptions } from "./aiRuntimes";
import type { CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import { sha256Hex } from "../sha256";
import { StandardEmptyState } from "./SurfacePrimitives";
import { EditorWorkspaceSearch } from "./EditorWorkspaceSearch";
import type { FencedRunCandidate } from "./AssistantMarkdown";
import { EditorSourceControl } from "./EditorSourceControl";
import { EditorPreferencesDialog } from "./EditorPreferencesDialog";
import { codeMirrorKey, eventMatchesShortcut, useEditorPreferences } from "../state/editorPreferences";

const MAX_EDITOR_BYTES = 1024 * 1024;

interface CodeEditorPanelProps {
  active: boolean;
  api: ApiClient;
  engagementId: string;
  providers?: ProviderHealth[];
  harnesses?: HarnessProfile[];
  onRun?: (candidate: FencedRunCandidate) => void;
  onOpenTerminal?: () => void;
  onUseWithAssistant?: (context: {
    text: string;
    sourceKind: "workspace_file";
    sourceId: string;
    sourceLabel: string;
    truncated: boolean;
  }) => void;
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
  return {
    content,
    sha256: sha256Hex(payload),
  };
}

function nextUntitledPath(directory: string, entries: WorkspaceEntry[], buffers: WorkbenchEditorBuffer[]): string {
  const names = new Set(entries.map((entry) => entry.name));
  for (const candidate of buffers) {
    const parts = candidate.filePath.split("/");
    const parent = parts.slice(0, -1).join("/");
    if (parent === directory) names.add(parts.at(-1) ?? "");
  }
  let name = "untitled.txt";
  let suffix = 2;
  while (names.has(name)) name = `untitled-${suffix++}.txt`;
  return directory ? `${directory}/${name}` : name;
}

export function CodeEditorPanel({ active, api, engagementId, providers = [], harnesses = [], onRun, onOpenTerminal, onUseWithAssistant }: CodeEditorPanelProps) {
  const confirm = useConfirmation();
  const { buffer, buffers, activateBuffer, closeBuffer, closeSplit, focusPane, persistenceError, persistenceState, primaryBuffer, retryPersistence, secondaryBuffer, setBuffer, splitEditor, updateBuffer: updateBufferById, updateBuffers } = useWorkbenchEditor(engagementId);
  const { preferences, savePreferences } = useEditorPreferences();
  const [directory, setDirectory] = useState("");
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [nextOffset, setNextOffset] = useState<number>();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [validationError, setValidationError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const [entryMenu, setEntryMenu] = useState<WorkspaceEntryMenuState>();
  const [selection, setSelection] = useState("");
  const [suggestionOpen, setSuggestionOpen] = useState(false);
  const [suggestionRevision, setSuggestionRevision] = useState<string>();
  const [localCompletionEnabled, setLocalCompletionEnabled] = useState(true);
  const [mobileFilesOpen, setMobileFilesOpen] = useState(false);
  const [mobileToolsOpen, setMobileToolsOpen] = useState(false);
  const [findRequest, setFindRequest] = useState(0);
  const [workspaceSearchMode, setWorkspaceSearchMode] = useState<"files" | "text">();
  const [sidebarMode, setSidebarMode] = useState<"files" | "source-control">("files");
  const [sourceControlRevision, setSourceControlRevision] = useState(0);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const [restoreRetry, setRestoreRetry] = useState(0);
  const [navigation, setNavigation] = useState<{ line: number; column: number; request: number }>();
  const [preserving, setPreserving] = useState(false);
  const restoringIds = useRef(new Set<string>());
  const failedRestoreIds = useRef(new Set<string>());
  const suggestionRuntimes = useMemo(() => codeSuggestionRuntimeOptions(providers, harnesses), [providers, harnesses]);
  const dirty = Boolean(buffer && (!buffer.existing || buffer.content !== buffer.savedContent));
  const anyDirty = buffers.some((candidate) => !candidate.existing || candidate.content !== candidate.savedContent);
  const crumbs = useMemo(() => directory ? directory.split("/") : [], [directory]);

  const load = useCallback(async (offset = 0, signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    setValidationError(undefined);
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
    setSidebarMode("files");
    failedRestoreIds.current.clear();
    restoringIds.current.clear();
  }, [engagementId]);

  useEffect(() => {
    if (!active) return;
    for (const candidate of [primaryBuffer, secondaryBuffer]) {
      if (!candidate?.restoreFromCore || restoringIds.current.has(candidate.id) || failedRestoreIds.current.has(candidate.id)) continue;
      restoringIds.current.add(candidate.id);
      void api.downloadWorkspaceFile(engagementId, candidate.filePath)
        .then(decodeWorkspaceFile)
        .then((decoded) => updateBufferById(candidate.id, {
          content: decoded.content,
          expectedSha256: decoded.sha256,
          restoreFromCore: false,
          savedContent: decoded.content,
        }))
        .catch((restoreError) => {
          failedRestoreIds.current.add(candidate.id);
          void logCaughtDiagnostic("interface.code_editor.restore", "A restored editor tab could not reload its workspace file.", restoreError, "code_editor");
          setError(restoreError instanceof Error ? `Could not restore ${candidate.filePath}: ${restoreError.message}` : `Could not restore ${candidate.filePath}.`);
        })
        .finally(() => restoringIds.current.delete(candidate.id));
    }
  }, [active, api, engagementId, primaryBuffer, restoreRetry, secondaryBuffer, updateBufferById]);

  useEffect(() => {
    if (!anyDirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyDirty]);

  const openFile = async (entry: WorkspaceEntry, skipDirtyCheck = false) => {
    const open = buffers.find((candidate) => candidate.existing && candidate.filePath === entry.path);
    if (!skipDirtyCheck && open) {
      activateBuffer(open.id);
      setMobileFilesOpen(false);
      return;
    }
    setLoading(true);
    setError(undefined);
    setNotice(undefined);
    setConflict(false);
    try {
      const decoded = await decodeWorkspaceFile(await api.downloadWorkspaceFile(engagementId, entry.path));
      setBuffer({
        id: open?.id ?? newEditorBufferId("file"),
        content: decoded.content,
        expectedSha256: decoded.sha256,
        existing: true,
        filePath: entry.path,
        savedContent: decoded.content,
      });
      setCursor({ line: 1, column: 1 });
      setMobileFilesOpen(false);
      setMobileToolsOpen(false);
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

  const createFile = () => {
    setBuffer({
      id: newEditorBufferId(),
      content: "",
      existing: false,
      filePath: nextUntitledPath(directory, entries, buffers),
      savedContent: "",
    });
    setConflict(false);
    setError(undefined);
    setNotice("Choose a workspace-relative path, then start typing.");
    setCursor({ line: 1, column: 1 });
    setMobileFilesOpen(false);
    setMobileToolsOpen(false);
  };

  const save = useCallback(async (force = false, target = buffer) => {
    if (!target || saving) return;
    const path = target.filePath.trim();
    if (!validWorkspacePath(path)) {
      setValidationError("Enter a workspace-relative file path without empty, . or .. segments.");
      return;
    }
    const payload = new Blob([target.content], { type: "text/plain;charset=utf-8" });
    if (payload.size > MAX_EDITOR_BYTES) {
      setValidationError("Editor files may not exceed 1 MiB when encoded as UTF-8.");
      return;
    }
    setSaving(true);
    setError(undefined);
    setValidationError(undefined);
    setNotice(undefined);
    setConflict(false);
    try {
      const result = await api.uploadWorkspaceFile(
        engagementId,
        path,
        payload,
        target.existing,
        undefined,
        force ? undefined : target.expectedSha256,
      );
      const saved: WorkbenchEditorBuffer = {
        id: target.id,
        content: target.content,
        expectedSha256: result.sha256,
        existing: true,
        filePath: result.path,
        savedContent: target.content,
      };
      updateBufferById(target.id, saved);
      setSourceControlRevision((revision) => revision + 1);
      setNotice(`Saved /workspace/${result.path}. Use it from Terminal when you're ready.`);
      const parent = result.path.includes("/") ? result.path.slice(0, result.path.lastIndexOf("/")) : "";
      if (parent === directory) await load(0);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.code_editor.save", "The code editor could not save a workspace file.", caughtError, "code_editor");
      if (caughtError instanceof ApiError && caughtError.status === 412) {
        setConflict(true);
        setError("This file changed in Terminal or another workspace client after you opened it.");
      } else if (caughtError instanceof ApiError && caughtError.status === 409 && !target.existing) {
        setError("A workspace file already exists at this path. Choose another filename.");
      } else {
        setError(caughtError instanceof Error ? caughtError.message : "Could not save this file.");
      }
    } finally {
      setSaving(false);
    }
  }, [api, buffer, directory, engagementId, load, saving, updateBufferById]);

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
    if (buffer) updateBufferById(buffer.id, changes);
  };

  const openSuggestion = () => {
    if (!buffer) return;
    setSuggestionRevision(buffer.expectedSha256);
    setSuggestionOpen(true);
    setMobileToolsOpen(false);
  };

  const applySuggestion = (result: WritingTransformResponse) => {
    if (!buffer || suggestionRevision !== buffer.expectedSha256) {
      setError("The file changed while the suggestion was being prepared. Reload it before applying.");
      setSuggestionOpen(false);
      return;
    }
    const source = selection || buffer.content;
    const start = selection ? buffer.content.indexOf(source) : 0;
    const next = selection && start >= 0
      ? `${buffer.content.slice(0, start)}${result.content}${buffer.content.slice(start + source.length)}`
      : result.content;
    updateBuffer({ content: next });
    setNotice("Suggestion applied to the editor draft. Review it, then save explicitly.");
    setSuggestionOpen(false);
  };

  const completionSource = async (context: CompletionContext): Promise<CompletionResult | null> => {
    if (!localCompletionEnabled || !buffer || !context.matchBefore(/[A-Za-z_][A-Za-z0-9_]*/)?.text) return null;
    const items = await api.completeCode(engagementId, buffer.filePath, context.state.doc.toString(), context.pos);
    return { from: context.matchBefore(/[A-Za-z_][A-Za-z0-9_]*/)?.from ?? context.pos, options: items.map((item) => ({ label: item.label, type: item.type, detail: item.detail })) };
  };

  const copyPath = async (entry: WorkspaceEntry) => {
    try {
      await navigator.clipboard.writeText(`/workspace/${entry.path}`);
      setNotice(`Copied /workspace/${entry.path}.`);
    } catch (copyError) {
      void logCaughtDiagnostic("interface.code_editor.copy_path_failed", "A workspace path could not be copied.", copyError, "code_editor");
      setError(copyError instanceof Error ? copyError.message : "Could not copy the file path.");
    }
  };

  const copyContents = async (entry: WorkspaceEntry) => {
    try {
      const decoded = await decodeWorkspaceFile(await api.downloadWorkspaceFile(engagementId, entry.path));
      await navigator.clipboard.writeText(decoded.content);
      setNotice(`Copied the contents of ${entry.name}.`);
    } catch (copyError) {
      void logCaughtDiagnostic("interface.code_editor.copy_contents_failed", "Workspace file contents could not be copied.", copyError, "code_editor");
      setError(copyError instanceof Error ? copyError.message : "Could not copy file contents.");
    }
  };

  const renameEntry = async (entry: WorkspaceEntry, newName: string) => {
    const affected = buffers.filter((candidate) => candidate.existing && (candidate.filePath === entry.path || candidate.filePath.startsWith(`${entry.path}/`)));
    if (affected.some((candidate) => candidate.content !== candidate.savedContent) && !await confirm({
      title: "Rename with unsaved changes?",
      message: "Open drafts will keep their unsaved changes and move to the renamed workspace path.",
      confirmLabel: "Rename",
    })) return;
    try {
      const result = await api.renameWorkspaceEntry(engagementId, entry.path, newName);
      updateBuffers((current) => current.map((candidate) => {
        if (!candidate.existing || (candidate.filePath !== entry.path && !candidate.filePath.startsWith(`${entry.path}/`))) return candidate;
        return { ...candidate, filePath: `${result.path}${candidate.filePath.slice(entry.path.length)}` };
      }));
      setNotice(`Renamed ${entry.name} to ${newName}.`);
      setSourceControlRevision((revision) => revision + 1);
      await load(0);
    } catch (renameError) {
      void logCaughtDiagnostic("interface.code_editor.rename_failed", "A workspace entry could not be renamed.", renameError, "code_editor");
      setError(renameError instanceof Error ? renameError.message : "Could not rename the workspace entry.");
    }
  };

  const deleteEntry = async (entry: WorkspaceEntry) => {
    const open = buffers.filter((candidate) => candidate.existing && (candidate.filePath === entry.path || candidate.filePath.startsWith(`${entry.path}/`)));
    const approved = await confirm({
      title: `Delete ${entry.name}?`,
      message: open.some((candidate) => candidate.content !== candidate.savedContent) ? "This deletes the file and discards unsaved editor changes. This cannot be undone." : entry.kind === "directory" ? "Only an empty folder can be deleted. This cannot be undone." : "This deletes the scratch workspace file. This cannot be undone.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!approved) return;
    try {
      await api.deleteWorkspaceEntry(engagementId, entry.path);
      for (const candidate of open) closeBuffer(candidate.id);
      setNotice(`Deleted ${entry.name}.`);
      setSourceControlRevision((revision) => revision + 1);
      await load(0);
    } catch (deleteError) {
      void logCaughtDiagnostic("interface.code_editor.delete_failed", "A workspace entry could not be deleted.", deleteError, "code_editor");
      setError(deleteError instanceof Error ? deleteError.message : "Could not delete the workspace entry.");
    }
  };

  const closeEditorTab = async (candidate: WorkbenchEditorBuffer) => {
    const tabDirty = !candidate.existing || candidate.content !== candidate.savedContent;
    if (tabDirty && !await confirm({
      title: `Close ${candidate.filePath.split("/").at(-1) || "untitled file"}?`,
      message: "This discards the unsaved editor draft. Other open files are unaffected.",
      confirmLabel: "Discard and close",
      tone: "danger",
    })) return;
    closeBuffer(candidate.id);
  };

  const openWorkspaceMatch = async (match: WorkspaceSearchMatch) => {
    setWorkspaceSearchMode(undefined);
    await openFile({
      path: match.path,
      name: match.path.split("/").at(-1) ?? match.path,
      kind: "file",
      size: 0,
      modifiedAt: new Date().toISOString(),
    });
    if (match.line) setNavigation({ line: match.line, column: match.column ?? 1, request: Date.now() });
  };

  const executionLanguage = (path: string): ExecutionLanguage | undefined => {
    const extension = path.split(".").at(-1)?.toLowerCase();
    if (extension === "py") return "python";
    if (["sh", "bash", "zsh"].includes(extension ?? "")) return extension === "sh" ? "sh" : "bash";
    return undefined;
  };

  const runDraft = () => {
    if (!buffer || !onRun) return;
    const language = executionLanguage(buffer.filePath);
    if (!language) {
      setError("Reviewed execution currently supports Python and shell files. Use Terminal for this language.");
      return;
    }
    onRun({
      source: buffer.content,
      language,
      declaredLanguage: language,
      origin: {
        kind: "selection",
        sourceKind: buffer.existing ? "workspace_file" : "editor_draft",
        sourceId: sha256Hex(buffer.filePath),
        sourceLabel: buffer.filePath.slice(-500),
        sourceSha256: sha256Hex(buffer.content),
      },
    });
  };

  const preserveAsEvidence = async () => {
    if (!buffer?.existing || dirty || preserving) return;
    const approved = await confirm({
      title: `Preserve ${buffer.filePath.split("/").at(-1)} as Evidence?`,
      message: "Nebula will store the exact saved bytes and SHA-256 as immutable project Evidence. Future edits remain separate.",
      confirmLabel: "Preserve as Evidence",
    });
    if (!approved) return;
    setPreserving(true);
    setError(undefined);
    try {
      const evidence = await api.promoteWorkspaceFile(engagementId, buffer.filePath, buffer.filePath.split("/").at(-1));
      setNotice(`Preserved ${buffer.filePath} as Evidence ${evidence.id}.`);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.code_editor.preserve", "The editor file could not be preserved as Evidence.", caughtError, "code_editor");
      setError(caughtError instanceof Error ? caughtError.message : "Could not preserve this file as Evidence.");
    } finally {
      setPreserving(false);
    }
  };

  useEffect(() => {
    if (!active) return;
    const shortcut = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (buffer && eventMatchesShortcut(event, preferences.keybindings.save)) {
        event.preventDefault();
        void save(false, buffer);
      } else if (eventMatchesShortcut(event, preferences.keybindings.quickOpen)) {
        event.preventDefault();
        setWorkspaceSearchMode("files");
      } else if (eventMatchesShortcut(event, preferences.keybindings.workspaceSearch)) {
        event.preventDefault();
        setWorkspaceSearchMode("text");
      } else if (buffer && eventMatchesShortcut(event, preferences.keybindings.closeEditor)) {
        event.preventDefault();
        void closeEditorTab(buffer);
      } else if (buffers.length > 1 && eventMatchesShortcut(event, preferences.keybindings.nextEditor)) {
        event.preventDefault();
        const current = buffers.findIndex((candidate) => candidate.id === buffer?.id);
        activateBuffer(buffers[(current + 1) % buffers.length].id);
      } else if (buffers.length > 1 && eventMatchesShortcut(event, preferences.keybindings.splitEditor)) {
        event.preventDefault();
        if (secondaryBuffer) closeSplit();
        else splitEditor();
      }
    };
    document.addEventListener("keydown", shortcut);
    return () => document.removeEventListener("keydown", shortcut);
  }, [active, buffer, buffers, closeSplit, preferences.keybindings, save, secondaryBuffer, splitEditor]);

  const editorPane = (candidate: WorkbenchEditorBuffer, pane: "primary" | "secondary") => <div className={`code-editor-pane${candidate.id === buffer?.id ? " active" : ""}`} key={`${pane}:${candidate.id}`}>
    {secondaryBuffer && <header><button className="code-editor-pane-focus" type="button" aria-label={`Focus ${candidate.filePath} editor`} aria-pressed={candidate.id === buffer?.id} onClick={() => focusPane(candidate.id)}><span>{candidate.filePath}</span></button>{pane === "secondary" && <button className="icon-button subtle" type="button" aria-label="Close split editor" onClick={closeSplit}><X size={14} /></button>}</header>}
    <CodeMirrorSurface active={active} ariaLabel={secondaryBuffer ? `${pane === "primary" ? "Primary" : "Secondary"} code editor: ${candidate.filePath}` : "Code editor"} filePath={candidate.filePath} fontSize={preferences.fontSize} tabSize={preferences.tabSize} wordWrap={preferences.wordWrap} saveKey={codeMirrorKey(preferences.keybindings.save)} value={candidate.content} findRequest={candidate.id === buffer?.id ? findRequest : 0} reveal={candidate.id === buffer?.id ? navigation : undefined} onFocus={() => focusPane(candidate.id)} onChange={(content) => updateBufferById(candidate.id, { content })} onSelectionChange={(text) => { if (candidate.id === buffer?.id) setSelection(text); }} completionSource={completionSource} onCursorChange={(line, column) => { if (candidate.id === buffer?.id) setCursor({ line, column }); }} onSave={() => void save(false, candidate)} />
  </div>;

  return <div className={`code-editor-panel${buffer ? " has-buffer" : ""}${mobileFilesOpen ? " mobile-files-open" : ""}`}>
    <aside className="code-editor-sidebar" aria-label="Editor files">
      <header><div><Braces size={16} /><strong>Code workspace</strong></div><div><button className="icon-button subtle" type="button" aria-label="New file" onClick={() => void createFile()}><FilePlus2 size={15} /></button><button className="icon-button subtle" type="button" aria-label="Refresh editor files" disabled={loading} onClick={() => void load(0)}><RefreshCw className={loading ? "spin" : undefined} size={14} /></button>{buffer && <button className="icon-button subtle code-editor-mobile-only" type="button" aria-label="Hide editor files" onClick={() => setMobileFilesOpen(false)}><X size={15} /></button>}</div></header>
      <div className="code-editor-sidebar-tabs" role="tablist" aria-label="Editor sidebar"><button type="button" role="tab" aria-selected={sidebarMode === "files"} onClick={() => setSidebarMode("files")}><Folder size={13} /> Files</button><button type="button" role="tab" aria-selected={sidebarMode === "source-control"} onClick={() => setSidebarMode("source-control")}><GitBranch size={13} /> Changes</button></div>
      {sidebarMode === "files" ? <>
        <nav className="code-editor-crumbs" aria-label="Editor workspace path"><button type="button" onClick={() => setDirectory("")}>/workspace</button>{crumbs.map((crumb, index) => <span key={`${crumb}-${index}`}>/<button type="button" onClick={() => setDirectory(crumbs.slice(0, index + 1).join("/"))}>{crumb}</button></span>)}</nav>
        <div className="code-editor-files">{entries.map((entry) => <button type="button" title={`${entry.path} · Right-click for actions`} className={buffer?.existing && buffer.filePath === entry.path ? "active" : undefined} disabled={entry.kind === "symlink" || entry.kind === "other"} onContextMenu={(event) => { event.preventDefault(); setEntryMenu({ entry, x: event.clientX, y: event.clientY }); }} onClick={() => chooseEntry(entry)} key={entry.path}>{entry.kind === "directory" ? <Folder size={15} /> : <File size={15} />}<span><strong>{entry.name}</strong><small>{entry.kind === "file" ? `${entry.size.toLocaleString()} bytes` : entry.kind}</small></span></button>)}{!entries.length && !loading && <div className="empty-state compact"><Folder size={20} /><strong>No files here</strong><p>Create a text file or use Terminal to populate /workspace.</p></div>}{nextOffset !== undefined && <button className="button quiet" type="button" onClick={() => void load(nextOffset)}>Load more</button>}</div>
      </> : <EditorSourceControl active={active} api={api} engagementId={engagementId} refreshKey={sourceControlRevision} onOpenTerminal={onOpenTerminal} onOpenFile={(path) => void openFile({ path, name: path.split("/").at(-1) ?? path, kind: "file", size: 0, modifiedAt: new Date().toISOString() })} />}
    </aside>
    <section className={`code-editor-main${buffer ? "" : " is-empty"}`}>
      {buffer ? <>
        <div className="code-editor-tabs" role="tablist" aria-label="Open editor files">
          {buffers.map((candidate) => {
            const tabDirty = !candidate.existing || candidate.content !== candidate.savedContent;
            const label = candidate.filePath.split("/").at(-1) || "Untitled";
            return <div className={candidate.id === buffer.id ? "active" : undefined} role="presentation" key={candidate.id}>
              <button type="button" role="tab" aria-selected={candidate.id === buffer.id} title={candidate.filePath} onClick={() => activateBuffer(candidate.id)}><File size={13} /><span>{label}</span>{tabDirty && <i aria-label="Unsaved changes" />}</button>
              <button type="button" aria-label={`Close ${label}`} onClick={() => void closeEditorTab(candidate)}><X size={13} /></button>
            </div>;
          })}
          <button className="code-editor-new-tab" type="button" aria-label="New editor file" onClick={() => void createFile()}><FilePlus2 size={14} /></button>
        </div>
        <header className="code-editor-toolbar">
          <div className="code-editor-file-row"><label><span className="sr-only">File path</span><span aria-hidden="true">/workspace/</span><input aria-label="File path" value={buffer.filePath} readOnly={buffer.existing} spellCheck={false} onChange={(event) => updateBuffer({ filePath: event.target.value })} /></label><span className={`code-editor-dirty${dirty ? " dirty" : ""}`} aria-live="polite">{dirty ? "Unsaved" : "Saved"}</span></div>
          <div className="code-editor-actions">
            <strong className="code-editor-mobile-title"><Braces size={15} /> Code</strong>
            <div className="code-editor-secondary-actions"><button className="button quiet" type="button" title={`Quick open (${preferences.keybindings.quickOpen})`} onClick={() => setWorkspaceSearchMode("files")}><Search size={14} /> Open</button><button className="button quiet" type="button" title={`Search workspace (${preferences.keybindings.workspaceSearch})`} onClick={() => setWorkspaceSearchMode("text")}><TextSearch size={14} /> Search</button><button className="button quiet" type="button" onClick={() => setFindRequest((request) => request + 1)}><Search size={14} /> Find</button><button className="button quiet" type="button" disabled={buffers.length < 2} title={buffers.length < 2 ? "Open another file before splitting" : preferences.keybindings.splitEditor} onClick={secondaryBuffer ? closeSplit : splitEditor}><Columns2 size={14} /> {secondaryBuffer ? "Unsplit" : "Split"}</button><button className="button quiet" type="button" aria-label="Editor settings" onClick={() => setPreferencesOpen(true)}><Settings2 size={14} /></button><button className="button quiet" type="button" disabled={!suggestionRuntimes.length} title={!suggestionRuntimes.length ? "Enable a runtime with explicit code-suggestion capability" : undefined} onClick={openSuggestion}><Sparkles size={14} /> Suggest</button></div>
            <button className="icon-button subtle code-editor-mobile-tools-toggle" type="button" aria-label="More editor actions" aria-expanded={mobileToolsOpen} onClick={() => setMobileToolsOpen((open) => !open)}><MoreHorizontal size={18} /></button>
            <button className="icon-button subtle code-editor-mobile-files-toggle" type="button" aria-label="Show editor files" onClick={() => { setMobileFilesOpen(true); setMobileToolsOpen(false); }}><Folder size={18} /></button>
            <button className="button primary code-editor-save" type="button" disabled={saving || (!dirty && buffer.existing) || !validWorkspacePath(buffer.filePath.trim())} onClick={() => void save()}>{saving ? <LoaderCircle className="spin" size={14} /> : <Save size={14} />} {saving ? "Saving…" : "Save"}</button>
          </div>
          {mobileToolsOpen && <div className="code-editor-mobile-tools" aria-label="Editor options"><label className="code-editor-completion-toggle"><input type="checkbox" checked={localCompletionEnabled} onChange={(event) => setLocalCompletionEnabled(event.target.checked)} /> Local completions</label><button className="button quiet" type="button" onClick={() => { setWorkspaceSearchMode("files"); setMobileToolsOpen(false); }}><Search size={14} /> Quick open</button><button className="button quiet" type="button" onClick={() => { setWorkspaceSearchMode("text"); setMobileToolsOpen(false); }}><TextSearch size={14} /> Search files</button><button className="button quiet" type="button" onClick={() => { setFindRequest((request) => request + 1); setMobileToolsOpen(false); }}><Search size={14} /> Find</button><button className="button quiet" type="button" disabled={buffers.length < 2} onClick={() => { if (secondaryBuffer) closeSplit(); else splitEditor(); setMobileToolsOpen(false); }}><Columns2 size={14} /> {secondaryBuffer ? "Close split" : "Split editor"}</button><button className="button quiet" type="button" onClick={() => { setPreferencesOpen(true); setMobileToolsOpen(false); }}><Settings2 size={14} /> Editor settings</button><button className="button quiet" type="button" disabled={!suggestionRuntimes.length} title={!suggestionRuntimes.length ? "Enable a runtime with explicit code-suggestion capability" : undefined} onClick={openSuggestion}><Sparkles size={14} /> Suggest code</button></div>}
        </header>
        {error && <><DiagnosticErrorNotice error={error} fallback="The editor operation failed." compact />{failedRestoreIds.current.size > 0 && <button className="button quiet code-editor-restore-retry" type="button" onClick={() => { failedRestoreIds.current.clear(); setError(undefined); setRestoreRetry((revision) => revision + 1); }}><RefreshCw size={13} /> Retry restored files</button>}</>}{validationError && <InlineValidationNotice message={validationError} />}{notice && <p className="workspace-notice" role="status">{notice}</p>}
        {conflict && <div className="code-editor-conflict" role="alert"><ShieldAlert size={17} /><span><strong>Newer workspace version detected</strong><small>Your draft is still open. Reload the Terminal version or overwrite it explicitly.</small></span><button className="button quiet" type="button" onClick={() => void reloadConflict()}><RotateCcw size={13} /> Reload</button><button className="button danger" type="button" onClick={() => void forceOverwrite()}>Force overwrite</button></div>}
        <div className={`code-editor-surfaces${secondaryBuffer ? " split" : ""}`}>{primaryBuffer && editorPane(primaryBuffer, "primary")}{secondaryBuffer && editorPane(secondaryBuffer, "secondary")}</div>
        <footer><span>{languageLabelForPath(buffer.filePath)}</span><span>Ln {cursor.line}, Col {cursor.column}</span><span title="Tab and Shift+Tab indent or outdent code">UTF-8 · spaces: {preferences.tabSize}</span><span>{buffers.length} open · {buffers.filter((candidate) => !candidate.existing || candidate.content !== candidate.savedContent).length} unsaved · {persistenceState === "ready" ? "recovery on" : persistenceState}</span><div className="code-editor-security-actions" aria-label="Security workflow actions"><button className="button quiet" type="button" disabled={!onRun || !executionLanguage(buffer.filePath)} title={!executionLanguage(buffer.filePath) ? "Reviewed execution supports Python and shell files" : "Review and run this draft in Nebula's isolated execution runtime"} onClick={runDraft}><Play size={13} /> Review & run</button><button className="button quiet" type="button" disabled={!onUseWithAssistant} onClick={() => onUseWithAssistant?.({ text: buffer.content, sourceKind: "workspace_file", sourceId: buffer.filePath, sourceLabel: buffer.filePath, truncated: false })}><MessageSquareText size={13} /> Ask Nebula</button><button className="button quiet" type="button" disabled={!buffer.existing || dirty || preserving} title={dirty ? "Save this draft before preserving exact bytes" : undefined} onClick={() => void preserveAsEvidence()}>{preserving ? <LoaderCircle className="spin" size={13} /> : <FileCheck2 size={13} />} Preserve as Evidence</button></div></footer>
      </> : <><StandardEmptyState icon={<Braces size={25} />} title="Shared workspace editor" explanation="Open or create a text file here, then run it from Terminal in /workspace using its interpreter." primaryAction={<button className="button primary" type="button" onClick={() => void createFile()}><FilePlus2 size={15} /> New file</button>} />{error && <DiagnosticErrorNotice error={error} fallback="The editor operation failed." compact />}</>}
      {persistenceState === "failed" && <div className="code-editor-persistence-error" role="alert"><ShieldAlert size={15} /><span><strong>Hot-exit recovery is unavailable</strong><small>Save workspace files before closing this browser. {persistenceError}</small></span><button className="button quiet" type="button" onClick={retryPersistence}>Retry</button></div>}
    </section>
    {suggestionOpen && <AIWritingDialog api={api} engagementId={engagementId} providers={providers} harnesses={harnesses} purpose="code_suggestion" title="Suggest a code change" description="Generate an operator-reviewed suggestion for the active file. Nothing is saved until you review, apply, and save." sourceLabel={selection ? "Selected code" : buffer?.filePath ?? "Active file"} sourceText={selection || buffer?.content || ""} initialInstruction="Suggest a focused improvement for this code." onClose={() => setSuggestionOpen(false)} onApply={applySuggestion} />}
    {workspaceSearchMode && <EditorWorkspaceSearch api={api} engagementId={engagementId} initialMode={workspaceSearchMode} onClose={() => setWorkspaceSearchMode(undefined)} onOpen={(match) => void openWorkspaceMatch(match)} />}
    {preferencesOpen && <EditorPreferencesDialog preferences={preferences} onApply={savePreferences} onClose={() => setPreferencesOpen(false)} />}
    {entryMenu && <WorkspaceEntryContextMenu menu={entryMenu} onClose={() => setEntryMenu(undefined)} onCopyPath={copyPath} onCopyContents={copyContents} onRename={renameEntry} onDelete={deleteEntry} />}
  </div>;
}
