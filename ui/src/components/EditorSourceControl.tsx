import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Braces, Diff, GitBranch, LoaderCircle, RefreshCw, SquareTerminal, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { SourceControlDiff, SourceControlFile, SourceControlFileStatus, SourceControlStatus } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { ModalSurface } from "./DialogSystem";

interface EditorSourceControlProps {
  active: boolean;
  api: ApiClient;
  engagementId: string;
  onOpenFile(path: string): void;
  onOpenTerminal?(): void;
  refreshKey?: number;
}

const labels: Record<SourceControlFileStatus, string> = {
  unmodified: "unchanged",
  modified: "modified",
  added: "added",
  deleted: "deleted",
  renamed: "renamed",
  copied: "copied",
  unmerged: "conflict",
  untracked: "untracked",
  ignored: "ignored",
  unknown: "changed",
};

function primaryStatus(file: SourceControlFile): SourceControlFileStatus {
  return file.worktreeStatus !== "unmodified" ? file.worktreeStatus : file.indexStatus;
}

export function EditorSourceControl({ active, api, engagementId, onOpenFile, onOpenTerminal, refreshKey = 0 }: EditorSourceControlProps) {
  const [status, setStatus] = useState<SourceControlStatus>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [diff, setDiff] = useState<SourceControlDiff>();
  const [diffLoading, setDiffLoading] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      setStatus(await api.sourceControlStatus(engagementId, signal));
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.code_editor.source_control_status", "Source-control status could not be loaded.", caughtError, "code_editor");
      if (!signal?.aborted) setError(caughtError instanceof Error ? caughtError.message : "Source control is unavailable.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, engagementId]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [active, load, refreshKey]);

  const showDiff = async (file: SourceControlFile, staged: boolean) => {
    setDiffLoading(true);
    setError(undefined);
    try {
      setDiff(await api.sourceControlDiff(engagementId, file.path, staged));
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.code_editor.source_control_diff", "A source-control diff could not be loaded.", caughtError, "code_editor");
      setError(caughtError instanceof Error ? caughtError.message : "The diff could not be loaded.");
    } finally {
      setDiffLoading(false);
    }
  };

  if (loading && !status) return <div className="code-source-control-state" role="status"><LoaderCircle className="spin" size={18} /><span>Reading Git status…</span></div>;
  if (error && !status) return <div className="code-source-control-state"><DiagnosticErrorNotice error={error} fallback="Source control is unavailable." compact /><button className="button quiet" type="button" onClick={() => void load()}><RefreshCw size={13} /> Retry</button></div>;
  if (!status) return null;

  return <div className="code-source-control">
    <header>
      <div><GitBranch size={15} /><span><strong>{status.branch ?? "Source control"}</strong>{status.head && <small>{status.head}</small>}</span></div>
      <button className="icon-button subtle" type="button" aria-label="Refresh source control" disabled={loading} onClick={() => void load()}><RefreshCw className={loading ? "spin" : undefined} size={14} /></button>
    </header>
    {status.state !== "ready" ? <div className="code-source-control-state"><AlertTriangle size={18} /><p>{status.detail}</p>{onOpenTerminal && <button className="button quiet" type="button" onClick={onOpenTerminal}><SquareTerminal size={14} /> Open Terminal</button>}</div> : <>
      <div className="code-source-control-summary" role="status"><span>{status.detail}</span>{status.truncated && <strong>First 500 shown</strong>}</div>
      <div className="code-source-control-files">
        {status.files.map((file) => {
          const staged = file.indexStatus !== "unmodified" && file.indexStatus !== "untracked";
          const working = file.worktreeStatus !== "unmodified" && file.worktreeStatus !== "untracked";
          const openable = !["deleted"].includes(primaryStatus(file));
          return <article key={`${file.path}:${file.originalPath ?? ""}`}>
            <button className="code-source-file" type="button" disabled={!openable} title={file.path} onClick={() => onOpenFile(file.path)}><Braces size={14} /><span><strong>{file.path.split("/").at(-1)}</strong><small>{file.path}</small></span><i className={`source-status ${primaryStatus(file)}`}>{labels[primaryStatus(file)]}</i></button>
            <div aria-label={`Diff actions for ${file.path}`}>
              {working && <button type="button" disabled={diffLoading} onClick={() => void showDiff(file, false)}><Diff size={12} /> Working diff</button>}
              {staged && <button type="button" disabled={diffLoading} onClick={() => void showDiff(file, true)}><Diff size={12} /> Staged diff</button>}
              {!working && !staged && <small>Open the file to review untracked content.</small>}
            </div>
          </article>;
        })}
        {!status.files.length && <div className="code-source-control-state"><GitBranch size={19} /><p>Working tree clean.</p></div>}
      </div>
      <footer><span>Stage, commit, branch, pull, and push remain in Nebula Terminal so repository mutations use its existing operator-visible workflow.</span>{onOpenTerminal && <button className="button quiet" type="button" onClick={onOpenTerminal}><SquareTerminal size={14} /> Open Terminal</button>}</footer>
    </>}
    {error && status && <DiagnosticErrorNotice error={error} fallback="The source-control operation failed." compact />}
    {diff && <ModalSurface className="source-control-diff-dialog" labelledBy="source-control-diff-title" onClose={() => setDiff(undefined)}>
      <header><div><small>{diff.staged ? "Staged changes" : "Working tree changes"}</small><h2 id="source-control-diff-title">{diff.path}</h2></div><button className="icon-button subtle" type="button" aria-label="Close source-control diff" onClick={() => setDiff(undefined)}><X size={17} /></button></header>
      <div className="source-control-diff-meta"><span>{diff.head ? `Compared with ${diff.head}` : "Repository has no commit yet"}</span>{diff.truncated && <strong>Diff truncated at 512 KiB</strong>}</div>
      <pre tabIndex={0} aria-label={`Diff for ${diff.path}`}>{diff.text || "No textual diff is available for this path."}</pre>
    </ModalSurface>}
  </div>;
}
