import { useEffect, useId, useState } from "react";
import { FileCode2, LoaderCircle, Search, TextSearch, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { WorkspaceSearchMatch } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { ModalSurface } from "./DialogSystem";

interface EditorWorkspaceSearchProps {
  api: ApiClient;
  engagementId: string;
  initialMode: "files" | "text";
  initialQuery?: string;
  onClose(): void;
  onOpen(match: WorkspaceSearchMatch): void;
}

export function EditorWorkspaceSearch({ api, engagementId, initialMode, initialQuery = "", onClose, onOpen }: EditorWorkspaceSearchProps) {
  const titleId = useId();
  const [mode, setMode] = useState(initialMode);
  const [query, setQuery] = useState(initialQuery);
  const [matches, setMatches] = useState<WorkspaceSearchMatch[]>([]);
  const [scannedFiles, setScannedFiles] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    const normalized = query.trim();
    if (!normalized) {
      setMatches([]);
      setScannedFiles(0);
      setTruncated(false);
      setError(undefined);
      return;
    }
    const controller = new AbortController();
    const timer = globalThis.setTimeout(() => {
      setLoading(true);
      setError(undefined);
      void api.searchWorkspace(engagementId, normalized, mode, "", controller.signal)
        .then((result) => {
          setMatches(result.matches);
          setScannedFiles(result.scannedFiles);
          setTruncated(result.truncated);
        })
        .catch((caughtError: unknown) => {
          void logCaughtDiagnostic("interface.code_editor.workspace_search", "Workspace search failed.", caughtError, "code_editor");
          if (!controller.signal.aborted) setError(caughtError instanceof Error ? caughtError.message : "Could not search the workspace.");
        })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 140);
    return () => {
      globalThis.clearTimeout(timer);
      controller.abort();
    };
  }, [api, engagementId, mode, query]);

  return <ModalSurface className="editor-search-dialog" labelledBy={titleId} onClose={onClose}>
    <header>
      <div><small>Project-wide navigation</small><h2 id={titleId}>{mode === "files" ? "Quick open" : "Search workspace text"}</h2></div>
      <button className="icon-button subtle" type="button" aria-label="Close workspace search" onClick={onClose}><X size={17} /></button>
    </header>
    <div className="editor-search-modes" role="tablist" aria-label="Search mode">
      <button type="button" role="tab" aria-selected={mode === "files"} onClick={() => setMode("files")}><FileCode2 size={14} /> Files <kbd>Ctrl P</kbd></button>
      <button type="button" role="tab" aria-selected={mode === "text"} onClick={() => setMode("text")}><TextSearch size={14} /> Text <kbd>Ctrl Shift F</kbd></button>
    </div>
    <label className="editor-search-input"><Search size={16} aria-hidden="true" /><span className="sr-only">{mode === "files" ? "Find a workspace file" : "Search workspace text"}</span><input autoFocus value={query} placeholder={mode === "files" ? "Type part of a file path…" : "Search UTF-8 workspace files…"} onChange={(event) => setQuery(event.target.value)} /></label>
    {error && <DiagnosticErrorNotice error={error} fallback="Workspace search failed." compact />}
    <div className="editor-search-results" role="listbox" aria-label="Workspace search results">
      {matches.map((match, index) => <button type="button" role="option" aria-selected="false" key={`${match.path}:${match.line ?? 0}:${match.column ?? 0}:${index}`} onClick={() => onOpen(match)}>
        {match.kind === "path" ? <FileCode2 size={16} /> : <TextSearch size={16} />}
        <span><strong>{match.path}</strong>{match.line && <small>Line {match.line}, column {match.column} · {match.preview}</small>}</span>
      </button>)}
      {!loading && Boolean(query.trim()) && !matches.length && !error && <div className="empty-state compact"><Search size={20} /><strong>No matches</strong><p>Try a shorter term or switch search modes.</p></div>}
      {!query.trim() && <div className="empty-state compact"><Search size={20} /><strong>{mode === "files" ? "Open any project file" : "Search across project code"}</strong><p>Results stay inside the current workspace and symbolic links are never followed.</p></div>}
    </div>
    <footer><span>{loading ? <><LoaderCircle className="spin" size={13} /> Searching…</> : query.trim() ? `${matches.length} result${matches.length === 1 ? "" : "s"} across ${scannedFiles} file${scannedFiles === 1 ? "" : "s"}` : "Ready"}</span>{truncated && <small>Bounded result limit reached; refine the query.</small>}</footer>
  </ModalSurface>;
}
