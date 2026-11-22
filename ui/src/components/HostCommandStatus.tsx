import {useCallback, useEffect, useRef, useState} from "react";
import {Square, X} from "lucide-react";
import type {ApiClient} from "../api/client";
import {cancelHostCommand, elapsedSince, getHostCommands, isHostCommandView, memoryAmount, ordinal, type HostCommandItem} from "../api/memoryBudgets";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import styles from "./HostCommandStatus.module.css";

export const HOST_COMMAND_POLL_MS = 2000;
/** Stop asking after this many empty reads when the command never appeared (not a Host command). */
const MISSES_BEFORE_GIVING_UP = 3;

/** Whether a native tool card may be a Host command worth asking Core about. */
export function mayBeHostCommand(card: {capability: string; status: string; receipt?: Record<string, unknown>}): boolean {
  const mode = card.receipt?.execution_mode;
  return (card.capability === "run_command" || card.capability === "Command runtime")
    && ["running", "queued", "pending"].includes(card.status)
    && (mode === undefined || mode === "host");
}

export function hostCommandStatusText(item: HostCommandItem, now: number): string {
  if (item.state === "queued") return ["Waiting for Host capacity", item.position ? `${ordinal(item.position)} in line` : "", elapsedSince(item.queued_at, now)].filter(Boolean).join(" · ");
  if (item.detached) return `Exited · detached process still running · ${memoryAmount(item.reserved_bytes)}`;
  return `Running · ${elapsedSince(item.started_at ?? item.queued_at, now)} · ${memoryAmount(item.reserved_bytes)}`;
}

/** Live Host queue state for one run_command tool call, read from Core. */
export function HostCommandStatus({api, toolCallId}: {api: ApiClient; toolCallId: string}) {
  const [item, setItem] = useState<HostCommandItem>();
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [now, setNow] = useState(() => Date.now());
  const seen = useRef(false);
  const misses = useRef(0);
  const mounted = useRef(true);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const view = await getHostCommands(api, {toolCallId, signal});
      if (signal?.aborted || !mounted.current) return;
      const found = isHostCommandView(view)
        ? [...view.queued, ...view.running].find(entry => entry.tool_call_id === toolCallId)
        : undefined;
      setNow(Date.now());
      if (found) {seen.current = true; misses.current = 0; setItem(found); return;}
      setItem(undefined);
      if (seen.current || ++misses.current >= MISSES_BEFORE_GIVING_UP) setDone(true);
    } catch (caught) {
      if (signal?.aborted || !mounted.current) return;
      void logCaughtDiagnostic("interface.host_command.status_failed", "Host command status could not be read.", caught, "chat");
    }
  }, [api, toolCallId]);

  useEffect(() => {
    mounted.current = true;
    return () => {mounted.current = false;};
  }, []);

  useEffect(() => {
    if (done) return;
    const controller = new AbortController();
    const poll = () => {if (document.visibilityState !== "hidden") void refresh(controller.signal);};
    poll();
    const timer = window.setInterval(poll, HOST_COMMAND_POLL_MS);
    const clock = window.setInterval(() => setNow(Date.now()), 1000);
    document.addEventListener("visibilitychange", poll);
    return () => {controller.abort(); clearInterval(timer); clearInterval(clock); document.removeEventListener("visibilitychange", poll);};
  }, [done, refresh]);

  if (done || !item) return null;
  const queued = item.state === "queued";
  const tone = queued ? styles.queued : item.detached ? styles.detached : styles.running;
  const label = queued ? "Cancel queued command" : "Stop command";
  const stop = async () => {
    if (busy) return;
    setBusy(true); setError(undefined);
    try {
      const updated = await cancelHostCommand(api, item.job_id);
      if (mounted.current) setItem(updated);
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.host_command.cancel_failed", "The Host command could not be cancelled.", caught, "chat");
      if (mounted.current) setError(caught instanceof Error ? caught.message : "The command could not be stopped. Try again.");
    } finally {if (mounted.current) setBusy(false);}
  };
  const stopping = item.cancel_requested;
  return <div className={`${styles.root} ${tone}`} data-host-command-state={queued ? "queued" : item.detached ? "detached" : "running"}>
    <div className={styles.line}>
      <span className={styles.marker} aria-hidden="true" />
      <span className={styles.text} role="status">{stopping ? (queued ? "Cancelling…" : "Stopping…") : hostCommandStatusText(item, now)}</span>
      <button className={`icon-button subtle ${styles.action}`} type="button" aria-label={label} title={label} disabled={busy || stopping} onClick={() => void stop()}>
        {queued ? <X size={15} aria-hidden="true" /> : <Square size={13} aria-hidden="true" fill="currentColor" />}
      </button>
    </div>
    {item.detached && !queued && <small className={styles.hint}>Its capacity stays held until the detached process exits or you stop it.</small>}
    {error && <small className={styles.error} role="alert">{error}</small>}
  </div>;
}
