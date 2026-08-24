import { useEffect, useId, useMemo, useState } from "react";
import { FlaskConical, Hammer, ListTodo, LoaderCircle, Play, RefreshCw, Search, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { WorkspaceTask } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { sha256Hex } from "../sha256";
import type { FencedRunCandidate } from "./AssistantMarkdown";
import { ModalSurface } from "./DialogSystem";

export function EditorTasksDialog({ api, engagementId, onClose, onRun }: { api: ApiClient; engagementId: string; onClose(): void; onRun(candidate: FencedRunCandidate): void }) {
  const titleId = useId();
  const [tasks, setTasks] = useState<WorkspaceTask[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const [revision, setRevision] = useState(0);
  const [scannedEntries, setScannedEntries] = useState(0);
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    void api.workspaceTasks(engagementId, controller.signal)
      .then((result) => {
        setTasks(result.tasks);
        setScannedEntries(result.scannedEntries);
        setTruncated(result.truncated);
      })
      .catch((caughtError: unknown) => {
        void logCaughtDiagnostic("interface.code_editor.tasks", "Workspace task discovery failed.", caughtError, "code_editor");
        if (!controller.signal.aborted) setError(caughtError instanceof Error ? caughtError.message : "Could not discover workspace tasks.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, engagementId, revision]);
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return needle ? tasks.filter((task) => `${task.label} ${task.command} ${task.detail}`.toLocaleLowerCase().includes(needle)) : tasks;
  }, [query, tasks]);
  const run = (task: WorkspaceTask) => {
    const source = `set -eu\n${task.command}\n`;
    onRun({ source, language: "bash", declaredLanguage: "bash", origin: { kind: "selection", sourceKind: "workspace_task", sourceId: task.id, sourceLabel: task.label, sourceSha256: sha256Hex(source) } });
    onClose();
  };
  const icon = (task: WorkspaceTask) => task.kind === "test" ? <FlaskConical size={16} /> : task.kind === "build" ? <Hammer size={16} /> : <Play size={16} />;
  return <ModalSurface className="editor-search-dialog" labelledBy={titleId} onClose={onClose}>
    <header><div><small>Reviewed execution handoff</small><h2 id={titleId}>Project tasks and tests</h2></div><button className="icon-button subtle" type="button" aria-label="Close project tasks" onClick={onClose}><X size={17} /></button></header>
    <label className="editor-search-input"><Search size={16} aria-hidden="true" /><span className="sr-only">Filter project tasks</span><input autoFocus value={query} placeholder="Filter tests, builds, and project scripts…" onChange={(event) => setQuery(event.target.value)} /></label>
    {error && <DiagnosticErrorNotice error={error} fallback="Task discovery failed." compact />}
    <div className="editor-search-results" role="listbox" aria-label="Discovered project tasks">
      {visible.map((task) => <button type="button" role="option" aria-selected="false" key={task.id} onClick={() => run(task)}>{icon(task)}<span><strong>{task.label}</strong><small><code>{task.command}</code> · {task.detail}</small></span></button>)}
      {!loading && !visible.length && !error && <div className="empty-state compact"><ListTodo size={20} /><strong>No project tasks discovered</strong><p>Add package.json scripts, Make targets, Python tests, go.mod, or Cargo.toml. Terminal remains available for one-off commands.</p></div>}
    </div>
    <footer><span>{loading ? <><LoaderCircle className="spin" size={13} /> Discovering…</> : `${visible.length} task${visible.length === 1 ? "" : "s"} · ${scannedEntries} workspace entries scanned${truncated ? " · bounded limit reached" : ""}`}</span><button className="button quiet" type="button" disabled={loading} onClick={() => setRevision((value) => value + 1)}><RefreshCw size={13} /> Refresh</button></footer>
  </ModalSurface>;
}
