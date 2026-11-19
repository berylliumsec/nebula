import {useCallback, useEffect, useId, useRef, useState, type FormEvent} from "react";
import {Link} from "react-router-dom";
import {Square, X} from "lucide-react";
import {ApiError, type ApiClient} from "../api/client";
import {HOST_COMMAND_LIMIT_MAX, MEMORY_BUDGET_CHANGED, cancelHostCommand, elapsedSince, getHostCommands, isHostCommandView, memoryAmount, saveHostCommandSettings, type HostCommandItem, type HostCommandView} from "../api/memoryBudgets";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import {announceSettingsSaved} from "./SettingsSaveFeedback";
import styles from "./HostCommandSettings.module.css";

export const HOST_COMMAND_REFRESH_MS = 3000;
const errorText = (error: unknown, fallback: string) => error instanceof Error && error.message ? error.message : fallback;
const isConflict = (error: unknown) => error instanceof ApiError ? error.status === 409 : /409|conflict/i.test(errorText(error, ""));

function chatHref(item: HostCommandItem): string | undefined {
  if (!item.project_id || !item.chat_session_id) return undefined;
  return `/projects/${encodeURIComponent(item.project_id)}/workbench?view=chat&session=${encodeURIComponent(item.chat_session_id)}`;
}

function HostCommandRow({item, projectName, now, busy, onCancel}: {item: HostCommandItem; projectName: string; now: number; busy: boolean; onCancel: (item: HostCommandItem) => void}) {
  const commandId = useId();
  const queued = item.state === "queued";
  const href = chatHref(item);
  const origin = item.chat_session_id ? "Chat" : item.owner_kind ? item.owner_kind.replaceAll("_", " ") : "Command";
  const label = queued ? "Cancel queued command" : "Stop command";
  const stopping = item.cancel_requested;
  return <li className={`${styles.row} ${queued ? styles.queuedRow : ""} ${item.detached ? styles.detachedRow : ""}`}>
    {queued && <span className={styles.position} aria-label={`Position ${item.position ?? "unknown"}`}>{item.position ?? "–"}</span>}
    <div className={styles.main}>
      <code id={commandId} className={styles.command}>{item.command || "Command text unavailable"}</code>
      <small className={styles.origin}>
        {item.detached && <span className={styles.detachedLabel}><span className={styles.dot} aria-hidden="true" />Detached</span>}
        <span>{projectName}</span>
        <span aria-hidden="true">·</span>
        {href ? <Link to={href}>{origin}</Link> : <span>{origin}</span>}
        {item.background && <><span aria-hidden="true">·</span><span>background</span></>}
      </small>
    </div>
    <span className={styles.meta}>{stopping
      ? queued ? "Cancelling…" : "Stopping…"
      : queued ? `Waiting ${elapsedSince(item.queued_at, now)}` : `${elapsedSince(item.started_at ?? item.queued_at, now)} · ${memoryAmount(item.reserved_bytes)}`}</span>
    <button className={`icon-button subtle ${styles.action} ${queued ? "" : styles.stop}`} type="button" aria-label={label} aria-describedby={commandId} title={label} disabled={busy || stopping} onClick={() => onCancel(item)}>
      {queued ? <X size={16} aria-hidden="true" /> : <Square size={13} aria-hidden="true" fill="currentColor" />}
    </button>
  </li>;
}

/** Core-wide Host command queue: the running limit, then every running and queued command. */
export function HostCommandSettings({api, previewMode = false, projects = []}: {api?: ApiClient; previewMode?: boolean; projects?: {id: string; name: string}[]}) {
  const [view, setView] = useState<HostCommandView>();
  const [draft, setDraft] = useState<string>("");
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const savedLimit = view?.settings.running_at_once;
  const dirty = savedLimit !== undefined && draft !== String(savedLimit);
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const revisionRef = useRef<number | undefined>(undefined);
  const limitId = useId();

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!api || previewMode) return;
    try {
      const next = await getHostCommands(api, {signal});
      if (signal?.aborted) return;
      if (!isHostCommandView(next)) throw new Error("Core returned an unreadable Host command queue.");
      setNow(Date.now());
      if (dirtyRef.current && revisionRef.current !== undefined && next.settings.revision !== revisionRef.current) {
        setConflict(true); setError("Host command settings changed elsewhere. Reload before saving your change.");
      } else {
        revisionRef.current = next.settings.revision;
        if (!dirtyRef.current) setDraft(String(next.settings.running_at_once));
      }
      setView(current => current && dirtyRef.current ? {...next, settings: current.settings} : next);
    } catch (caught) {
      if (signal?.aborted) return;
      void logCaughtDiagnostic("interface.host_commands.load_failed", "Host commands could not be read.", caught, "settings");
      setError(errorText(caught, "Host commands could not be read. Reload to check Core state."));
    }
  }, [api, previewMode]);

  useEffect(() => {
    if (!api || previewMode) return;
    const controller = new AbortController();
    void load(controller.signal);
    const refresh = () => {if (document.visibilityState !== "hidden") void load(controller.signal);};
    const tick = window.setInterval(refresh, HOST_COMMAND_REFRESH_MS);
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    window.addEventListener("focus", refresh); window.addEventListener(MEMORY_BUDGET_CHANGED, refresh);
    return () => {controller.abort(); clearInterval(tick); clearInterval(clock); window.removeEventListener("focus", refresh); window.removeEventListener(MEMORY_BUDGET_CHANGED, refresh);};
  }, [api, previewMode, load]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !view || busy || conflict || !dirty) return;
    const value = Number(draft);
    if (!Number.isInteger(value) || value < 1 || value > HOST_COMMAND_LIMIT_MAX) {setError(`Enter a whole number from 1 to ${HOST_COMMAND_LIMIT_MAX}.`); return;}
    setBusy(true); setError(undefined); setNotice(undefined);
    try {
      const result = await saveHostCommandSettings(api, view.settings.revision, value);
      const observed = await getHostCommands(api);
      if (!isHostCommandView(observed) || observed.settings.revision !== result.revision || observed.settings.running_at_once !== result.running_at_once || result.running_at_once !== value) {
        throw new Error("The save could not be verified. Reload before changing it again.");
      }
      revisionRef.current = observed.settings.revision;
      dirtyRef.current = false;
      setView(observed); setDraft(String(observed.settings.running_at_once));
      setNotice(`Saved and read back. Up to ${observed.settings.running_at_once} Host ${observed.settings.running_at_once === 1 ? "command runs" : "commands run"} at once.`);
      announceSettingsSaved("Host commands running at once saved");
      window.dispatchEvent(new Event(MEMORY_BUDGET_CHANGED));
    } catch (caught) {
      void logCaughtDiagnostic("interface.host_commands.save_failed", "Host command settings were not verified saved.", caught, "settings");
      if (isConflict(caught)) {setConflict(true); setError("Host command settings changed elsewhere. Reload before saving your change.");}
      else setError(errorText(caught, "Host command settings could not be saved."));
    } finally {setBusy(false);}
  };

  const cancel = async (item: HostCommandItem) => {
    if (!api || cancelling) return;
    setCancelling(item.job_id); setError(undefined);
    try {
      const updated = await cancelHostCommand(api, item.job_id);
      setView(current => current && {...current,
        running: current.running.map(entry => entry.job_id === updated.job_id ? updated : entry),
        queued: current.queued.map(entry => entry.job_id === updated.job_id ? updated : entry)});
      await load();
    } catch (caught) {
      void logCaughtDiagnostic("interface.host_commands.cancel_failed", "The Host command could not be cancelled.", caught, "settings");
      setError(errorText(caught, "The command could not be stopped. Try again."));
    } finally {setCancelling(undefined);}
  };

  const reload = () => {setConflict(false); setError(undefined); setNotice(undefined); dirtyRef.current = false; revisionRef.current = undefined; setView(undefined); void load();};
  const projectName = (id: string | null) => !id ? "Core" : projects.find(project => project.id === id)?.name ?? "Unknown project";
  const reserved = view?.running.reduce((total, item) => total + item.reserved_bytes, 0) ?? 0;

  return <section id="host-command-settings" className={styles.root} aria-labelledby="host-command-title">
    <h3 id="host-command-title">Host commands</h3>
    <p>Commands from every project share one queue. Each running command holds its memory limit until it and anything it started exit.</p>
    {previewMode && <p>Connect to Core to see and limit Host commands.</p>}
    {error && <p role="alert">{error}{conflict && <> <button className="button quiet" type="button" onClick={reload}>Reload</button></>}</p>}
    {notice && <p role="status">{notice}</p>}
    {!view && !error && !previewMode && api && <p role="status">Loading Host commands…</p>}
    {view && <>
      <form className={styles.form} onSubmit={event => void save(event)}>
        <label htmlFor={limitId}>Running at once</label>
        <div className={styles.limit}>
          <input id={limitId} type="number" inputMode="numeric" min={1} max={HOST_COMMAND_LIMIT_MAX} step={1} required value={draft} disabled={busy || conflict} aria-describedby={`${limitId}-hint`} onChange={event => {setDraft(event.target.value); setNotice(undefined);}} />
          <button className="button primary" type="submit" disabled={busy || conflict || !dirty}>{busy ? "Verifying…" : "Save"}</button>
        </div>
        <small id={`${limitId}-hint`}>Commands beyond this wait in line; projects with fewer running commands go first.</small>
      </form>
      <p className={styles.summary}>{view.running_count} of {view.settings.running_at_once} running · {view.queued.length} queued · {memoryAmount(reserved)} reserved</p>
      {!view.running.length && !view.queued.length && <p>No Host commands are running or waiting.</p>}
      {view.running.length > 0 && <><h4>Running</h4><ul className={styles.list} aria-label="Running Host commands">{view.running.map(item => <HostCommandRow key={item.job_id} item={item} projectName={projectName(item.project_id)} now={now} busy={cancelling === item.job_id} onCancel={item => void cancel(item)} />)}</ul></>}
      {view.queued.length > 0 && <><h4>Queued</h4><ul className={styles.list} aria-label="Queued Host commands">{view.queued.map(item => <HostCommandRow key={item.job_id} item={item} projectName={projectName(item.project_id)} now={now} busy={cancelling === item.job_id} onCancel={item => void cancel(item)} />)}</ul></>}
    </>}
  </section>;
}
