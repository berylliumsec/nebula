import { useCallback, useEffect, useState, type FormEvent } from "react";
import { History, LoaderCircle, Search, Trash2 } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { TerminalCommandHistoryStatus, TerminalCommandRecord } from "../api/types";
import { useConfirmation } from "./DialogSystem";
import "./TerminalCommandHistoryPanel.css";

interface TerminalCommandHistoryPanelProps {
  api: ApiClient;
  engagementId: string;
}

export function TerminalCommandHistoryPanel({ api, engagementId }: TerminalCommandHistoryPanelProps) {
  const confirm = useConfirmation();
  const [status, setStatus] = useState<TerminalCommandHistoryStatus>();
  const [records, setRecords] = useState<TerminalCommandRecord[]>([]);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [nextOffset, setNextOffset] = useState<number>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const load = useCallback(async (offset = 0, signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const [nextStatus, page] = await Promise.all([
        api.terminalCommandHistoryStatus(engagementId, signal),
        api.listTerminalCommands(engagementId, search, offset, 100, signal),
      ]);
      setStatus(nextStatus);
      setRecords((current) => offset ? [...current, ...page.records] : page.records);
      setNextOffset(page.nextOffset);
    } catch (loadError) {
      if (!signal?.aborted) setError(loadError instanceof Error ? loadError.message : "Could not load command history.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, engagementId, search]);

  useEffect(() => {
    const controller = new AbortController();
    void load(0, controller.signal);
    return () => controller.abort();
  }, [load]);

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setSearch(query.trim());
  };

  const toggle = async () => {
    if (!status) return;
    setBusy(true);
    setError(undefined);
    try {
      setStatus(await api.setTerminalCommandHistoryEnabled(engagementId, !status.enabled));
    } catch (toggleError) {
      setError(toggleError instanceof Error ? toggleError.message : "Could not change command history.");
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    if (!status?.recordCount) return;
    const approved = await confirm({
      title: "Clear local command history?",
      message: "This removes command metadata for this Project. Terminal output and evidence are unaffected.",
      confirmLabel: "Clear history",
      tone: "danger",
    });
    if (!approved) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.clearTerminalCommands(engagementId);
      setRecords([]);
      setNextOffset(undefined);
      setStatus(await api.terminalCommandHistoryStatus(engagementId));
    } catch (clearError) {
      setError(clearError instanceof Error ? clearError.message : "Could not clear command history.");
    } finally {
      setBusy(false);
    }
  };

  return <section className="terminal-command-history panel" aria-labelledby="terminal-command-history-title">
    <header><div><small>Local convenience data</small><h2 id="terminal-command-history-title"><History size={17} /> Terminal commands</h2><p>Commands and working directories only—never output, evidence, exports, or automatic model context.</p></div><div><label className="history-toggle"><input type="checkbox" checked={status?.enabled ?? true} disabled={!status || busy} onChange={() => void toggle()} /> Record commands</label><button className="button quiet" type="button" disabled={!status?.recordCount || busy} onClick={() => void clear()}><Trash2 size={14} /> Clear</button></div></header>
    <form className="history-search" onSubmit={submitSearch}><label><Search size={14} /><span className="sr-only">Search terminal commands</span><input type="search" value={query} placeholder="Search commands" onChange={(event) => setQuery(event.target.value)} /></label><button className="button secondary" type="submit" disabled={loading}>Search</button></form>
    {error && <p className="form-error" role="alert">{error}</p>}
    {!status?.enabled && <p className="workspace-notice" role="status">New commands are not being recorded. Existing local history is retained until cleared or expired.</p>}
    <div className="terminal-command-list" aria-busy={loading}>{records.map((record) => <article key={record.id} data-selection-source-kind="terminal_command" data-selection-source-id={record.id} data-selection-source-label="Terminal command"><code>{record.command}</code><footer><span title={record.cwd}>{record.cwd || "/"}</span><span className={record.exitCode === 0 ? "success" : "failure"}>exit {record.exitCode}</span><time dateTime={record.occurredAt}>{new Date(record.occurredAt).toLocaleString()}</time></footer></article>)}{!records.length && !loading && <div className="empty-state compact"><History size={21} /><strong>{search ? "No matching commands" : "No commands recorded yet"}</strong><p>Completed shell commands will appear here after the next prompt.</p></div>}{loading && <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading command history…</div>}</div>
    {nextOffset !== undefined && <button className="button quiet full" type="button" disabled={loading} onClick={() => void load(nextOffset)}>Load more</button>}
    {status && <footer className="history-retention">Retained for {status.retentionDays} days or {status.maxRecords.toLocaleString()} commands per Project, whichever comes first.</footer>}
  </section>;
}
