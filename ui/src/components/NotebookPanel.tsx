import { useCallback, useEffect, useState } from "react";
import DOMPurify from "dompurify";
import ReactMarkdown from "react-markdown";
import { CircleStop, NotebookPen, PackageCheck, Bookmark, Play, Plus, RefreshCw, RotateCcw, Save, Plug, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { NotebookCell, NotebookFile, NotebookListing, NotebookRun, NotebookRunState, NotebookStatus } from "../api/types";
import { CodeMirrorSurface } from "./CodeMirrorSurface";
import "./notebook.css";

const cellText = (source: string | string[]) => Array.isArray(source) ? source.join("") : source;
const activeRun = (run?: NotebookRun) => run?.status === "queued" || run?.status === "running";

function Output({ value }: { value: Record<string, unknown> }) {
  const kind = String(value.output_type ?? value.kind ?? "");
  const content = (value.content && typeof value.content === "object" ? value.content : value) as Record<string, unknown>;
  if (kind === "stream") return <pre className="notebook-output-text">{String(content.text ?? "")}</pre>;
  if (kind === "error") return <pre className="notebook-output-error" role="alert">{Array.isArray(content.traceback) ? content.traceback.join("\n") : `${content.ename ?? "Error"}: ${content.evalue ?? ""}`}</pre>;
  const data = content.data && typeof content.data === "object" ? content.data as Record<string, unknown> : {};
  if (typeof data["image/png"] === "string" && data["image/png"].length < 12_000_000) return <img className="notebook-output-image" src={`data:image/png;base64,${data["image/png"]}`} alt="Notebook cell output" />;
  if (typeof data["image/jpeg"] === "string" && data["image/jpeg"].length < 12_000_000) return <img className="notebook-output-image" src={`data:image/jpeg;base64,${data["image/jpeg"]}`} alt="Notebook cell output" />;
  if (typeof data["text/html"] === "string") {
    const safe = DOMPurify.sanitize(data["text/html"], {USE_PROFILES: {html: true}});
    return <iframe className="notebook-html-output" title="Sanitized notebook HTML output" sandbox="" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">${safe}`} />;
  }
  const text = data["text/plain"];
  return text !== undefined ? <pre className="notebook-output-text">{Array.isArray(text) ? text.join("") : String(text)}</pre> : null;
}

function Cell({ api, projectId, path, cell, file, run, live, active, onChanged, onStarted, onError }: {
  api: ApiClient; projectId: string; path: string; cell: NotebookCell; file: NotebookFile;
  run?: NotebookRun; live?: NotebookRunState; active: boolean;
  onChanged: (file: NotebookFile) => void; onStarted: (run: NotebookRun) => void; onError: (message: string) => void;
}) {
  const source = cellText(cell.source);
  const [draft, setDraft] = useState(source);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState(cell.cell_type === "markdown");
  const [baseline, setBaseline] = useState(file.cell_revisions[cell.id]);
  const changedElsewhere = dirty && baseline !== file.cell_revisions[cell.id];
  useEffect(() => {
    if (!dirty) { setDraft(source); setBaseline(file.cell_revisions[cell.id]); }
  }, [source, file.cell_revisions, cell.id, dirty]);

  const save = async (): Promise<boolean> => {
    if (!dirty) return true;
    if (changedElsewhere) { onError("This cell changed in another tab or by an agent. Review it before replacing your draft."); return false; }
    setSaving(true);
    try {
      const next = await api.saveNotebookCell(projectId, {path, cellId: cell.id, source: draft, cellType: cell.cell_type, expectedSourceHash: baseline});
      setDirty(false);
      setBaseline(next.cell_revisions[cell.id]);
      onChanged(next);
      return true;
    } catch (error) { onError(error instanceof Error ? error.message : "Cell could not be saved"); return false; }
    finally { setSaving(false); }
  };
  const execute = async () => {
    if (!await save()) return;
    try { onStarted(await api.runNotebookCell(projectId, path, cell.id)); }
    catch (error) { onError(error instanceof Error ? error.message : "Cell could not start"); }
  };
  const liveOutputs = live?.events.filter(event => event.event_type === "notebook.output").map(event => event.payload) ?? [];
  const outputs = activeRun(run) && liveOutputs.length ? liveOutputs : (cell.outputs ?? []);
  return <section className={`notebook-cell ${cell.cell_type}`} aria-label={`${cell.cell_type === "code" ? "Code" : "Markdown"} cell`}>
    <div className="notebook-cell-toolbar">
      <span>{cell.cell_type === "code" ? `In [${cell.execution_count ?? " "}]` : "Markdown"}</span>
      {cell.cell_type === "markdown" && <button type="button" className="button quiet" onClick={() => setPreview(value => !value)}>{preview ? "Edit" : "Preview"}</button>}
      {dirty && <button className="icon-button subtle" type="button" aria-label="Save cell" title="Save cell" disabled={saving || changedElsewhere} onClick={() => void save()}><Save size={16} /></button>}
      {cell.cell_type === "code" && (activeRun(run)
        ? <button className="icon-button subtle" type="button" aria-label="Interrupt cell" title="Interrupt cell" onClick={() => {if (run) void api.interruptNotebookRun(projectId, run.id).catch(error => onError(String(error)));}}><CircleStop size={16} /></button>
        : <button className="icon-button subtle" type="button" aria-label="Run cell" title="Run cell" disabled={saving} onClick={() => void execute()}><Play size={16} /></button>)}
      {run && <span className={`notebook-run-status ${run.status}`} role="status">{run.status}</span>}
    </div>
    {changedElsewhere && <p className="notebook-cell-conflict" role="alert">Cell changed elsewhere. Your draft is kept here; copy it before reloading.</p>}
    {cell.cell_type === "markdown" && preview
      ? <div className="notebook-markdown"><ReactMarkdown>{draft}</ReactMarkdown></div>
      : <CodeMirrorSurface active={active} ariaLabel={`${cell.cell_type} cell editor`} filePath={`${path}.${cell.cell_type === "code" ? "py" : "md"}`} value={draft} onChange={value => {setDraft(value); setDirty(value !== source);}} onCursorChange={() => undefined} onSave={() => void save()} minimap={false} stickyScroll={false} />}
    {outputs.length > 0 && <div className="notebook-outputs" aria-live="polite">{outputs.map((output, index) => <Output key={index} value={output} />)}</div>}
    {run?.status === "failed" && run.detail && <p className="notebook-cell-conflict" role="alert">{run.detail}</p>}
  </section>;
}

export function NotebookPanel({api, projectId, active, path, onPathChange, pinnedPath, onPin, onProjectSettings}: {
  api: ApiClient; projectId: string; active: boolean; path?: string;
  onPathChange: (path: string) => void; pinnedPath?: string; onPin?: (path: string | null) => Promise<void>;
  onProjectSettings: () => void;
}) {
  const [status, setStatus] = useState<NotebookStatus>();
  const [list, setList] = useState<NotebookListing[]>([]);
  const [file, setFile] = useState<NotebookFile>();
  const [runs, setRuns] = useState<NotebookRun[]>([]);
  const [live, setLive] = useState<Record<string, NotebookRunState>>({});
  const [newPath, setNewPath] = useState("");
  const [packageName, setPackageName] = useState("");
  const [packageLookup, setPackageLookup] = useState<{name: string; version: string; summary: string} | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    const [nextStatus, nextList] = await Promise.all([api.notebookStatus(projectId), api.listNotebooks(projectId)]);
    setStatus(nextStatus); setList(nextList);
    if (!path && nextList.length) onPathChange(nextList[0].path);
    if (path) {
      const [nextFile, nextRuns] = await Promise.all([api.readNotebook(projectId, path), api.listNotebookRuns(projectId, path)]);
      setFile(nextFile); setRuns(nextRuns);
      const active = nextRuns.filter(activeRun);
      if (active.length) {
        const states = await Promise.all(active.map(run => api.readNotebookRun(projectId, run.id)));
        setLive(previous => ({...previous, ...Object.fromEntries(states.map((state, index) => [active[index].id, state]))}));
      }
    } else setFile(undefined);
  }, [api, projectId, path, onPathChange]);

  useEffect(() => { if (!active) return; void refresh().catch(err => setError(String(err))); }, [active, refresh]);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => { void refresh().catch(err => setError(String(err))); }, runs.some(activeRun) ? 450 : 1400);
    return () => window.clearInterval(timer);
  }, [active, refresh, runs]);

  const activeRunIds = runs.filter(activeRun).map(run => run.id).join(",");
  useEffect(() => {
    if (!active || !path || !activeRunIds) return;
    const controllers = runs.filter(activeRun).map(run => {
      const controller = new AbortController();
      void api.watchNotebookRun(projectId, run.id, live[run.id]?.next_sequence ?? 0, event => {
        setLive(previous => {
          const current = previous[run.id] ?? {run, events: [], next_sequence: 0};
          const cursor = current.next_sequence ?? current.events[current.events.length - 1]?.sequence ?? 0;
          if (event.sequence <= cursor) return previous;
          return {...previous, [run.id]: {...current, events: [...current.events, event], next_sequence: event.sequence}};
        });
        if (event.event_type === "notebook.completed") {
          const result = String(event.payload.status);
          const status: NotebookRun["status"] = result === "succeeded" || result === "interrupted" ? result : "failed";
          setRuns(current => current.map(item => item.id === run.id ? {...item, status} : item));
          void api.readNotebook(projectId, path).then(setFile).catch(err => setError(String(err)));
        }
      }, controller.signal).catch(error => {
        if (!controller.signal.aborted) setError(`Live notebook updates stopped: ${String(error)}`);
      });
      return controller;
    });
    return () => controllers.forEach(controller => controller.abort());
  }, [active, activeRunIds, api, path, projectId]);

  const create = async () => {
    const name = newPath.trim().endsWith(".ipynb") ? newPath.trim() : `${newPath.trim()}.ipynb`;
    if (!newPath.trim()) return;
    setBusy("create"); setError("");
    try { const next = await api.createNotebook(projectId, name); setNewPath(""); setFile(next); onPathChange(name); await refresh(); }
    catch (err) { setError(String(err)); }
    finally { setBusy(""); }
  };
  const add = async (type: "code" | "markdown") => {
    if (!file || !path) return;
    setBusy("add"); setError("");
    try { setFile(await api.saveNotebookCell(projectId, {path, source: "", cellType: type, expectedRevision: file.revision})); }
    catch (err) { setError(String(err)); }
    finally { setBusy(""); }
  };
  const install = async () => {
    if (!path || !packageName.trim()) return;
    setBusy("package"); setError(""); setNotice("");
    try { const result = await api.installNotebookPackage(projectId, path, packageName.trim()); setNotice(`${result.requirement} installed. Restart the kernel if it was already running.`); setPackageName(""); }
    catch (err) { setError(String(err)); }
    finally { setBusy(""); }
  };
  const searchPackage = async () => {
    if (!path || !packageName.trim()) return;
    setBusy("package-search"); setError(""); setPackageLookup(null);
    try { setPackageLookup(await api.searchNotebookPackage(projectId, path, packageName.trim())); }
    catch (err) { setError(String(err)); }
    finally { setBusy(""); }
  };
  const latestRun = (id: string) => runs.find(run => run.cell_id === id);

  return <section className="notebook-panel" aria-label="Project notebook">
    <header className="notebook-header"><div><NotebookPen size={18} /><strong>Notebook</strong>{status && <small>{status.executionMode === "host" ? "Host" : "Docker"} Python</small>}</div><button className="icon-button subtle" type="button" aria-label="Refresh notebook" title="Refresh notebook" onClick={() => void refresh().catch(err => setError(String(err)))}><RefreshCw size={16} /></button></header>
    {error && <p className="notebook-error" role="alert">{error} <button className="button quiet" type="button" onClick={() => {setError(""); void refresh();}}>Retry</button></p>}
    {notice && <p className="notebook-notice" role="status">{notice}<button className="icon-button subtle" type="button" aria-label="Dismiss notice" onClick={() => setNotice("")}><X size={14} /></button></p>}
    {status?.consentRequired && <div className="notebook-consent"><Plug size={17} /><span>Host execution needs one-time project approval.</span><button className="button secondary" type="button" onClick={onProjectSettings}>Review Host access</button></div>}
    <div className="notebook-filebar"><label>Notebook <select value={path ?? ""} onChange={event => onPathChange(event.target.value)}><option value="">Choose a notebook</option>{list.map(item => <option key={item.path} value={item.path}>{item.path}</option>)}</select></label>{path && onPin && <button className="icon-button subtle" type="button" aria-label={pinnedPath === path ? "Unpin notebook from chat" : "Pin notebook beside chat"} title={pinnedPath === path ? "Unpin from chat" : "Pin beside chat"} onClick={() => void onPin(pinnedPath === path ? null : path).catch(err => setError(String(err)))}><Bookmark size={16} fill={pinnedPath === path ? "currentColor" : "none"} /></button>}</div>
    <form className="notebook-create" onSubmit={event => {event.preventDefault(); void create();}}><input aria-label="New notebook name" placeholder="New notebook name" value={newPath} onChange={event => setNewPath(event.target.value)} /><button className="icon-button subtle" type="submit" disabled={!newPath.trim() || busy === "create"} aria-label="Create notebook" title="Create notebook"><Plus size={17} /></button></form>
    {file && path ? <div className="notebook-body"><div className="notebook-actions"><button className="button quiet" type="button" disabled={Boolean(busy)} onClick={() => void add("code")}><Plus size={15} /> Code</button><button className="button quiet" type="button" disabled={Boolean(busy)} onClick={() => void add("markdown")}><Plus size={15} /> Markdown</button><button className="button quiet" type="button" disabled={Boolean(busy) || runs.some(activeRun)} onClick={() => void api.restartNotebookKernel(projectId, path).then(() => setNotice("Kernel restarted; variables were cleared.")).catch(err => setError(String(err)))}><RotateCcw size={15} /> Restart</button></div>
      {file.document.cells.length ? file.document.cells.map(cell => <Cell key={`${path}:${cell.id}`} api={api} projectId={projectId} path={path} cell={cell} file={file} run={latestRun(cell.id)} live={live[latestRun(cell.id)?.id ?? ""]} active={active} onChanged={setFile} onStarted={run => {setRuns(current => [run, ...current]); void refresh();}} onError={setError} />) : <div className="notebook-empty"><strong>Empty notebook</strong><p>Add a code cell to try Python.</p><button className="button primary" type="button" onClick={() => void add("code")}><Plus size={15} /> Add code cell</button></div>}
      <form className="notebook-packages" onSubmit={event => {event.preventDefault(); void searchPackage();}}><label>Project Python packages<input value={packageName} onChange={event => {setPackageName(event.target.value); setPackageLookup(null);}} placeholder="Exact package name, for example numpy" /></label><button className="button quiet" type="submit" disabled={!packageName.trim() || Boolean(busy)}>{busy === "package-search" ? "Finding…" : "Find"}</button><button className="button secondary" type="button" onClick={() => void install()} disabled={!packageName.trim() || Boolean(busy)}><PackageCheck size={15} /> {busy === "package" ? "Installing…" : "Install"}</button>{packageLookup && <p className="notebook-package-result" role="status"><strong>{packageLookup.name} {packageLookup.version}</strong>{packageLookup.summary && <span>{packageLookup.summary}</span>}</p>}</form>
    </div> : <div className="notebook-empty"><strong>No notebook open</strong><p>Create a Python notebook or choose one from this project.</p></div>}
  </section>;
}
