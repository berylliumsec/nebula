import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Copy, Download, History, LoaderCircle, ShieldCheck } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { TerminalCommandHistoryStatus, TerminalCommandRecord } from "../api/types";
import { useConfirmation } from "./DialogSystem";
import "./TerminalCommandHistoryPanel.css";

interface TerminalCommandHistoryPanelProps {
  api: ApiClient;
  engagementId: string;
}

function sizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function durationLabel(record: TerminalCommandRecord): string | undefined {
  if (!record.startedAt || !record.completedAt) return undefined;
  const milliseconds = new Date(record.completedAt).getTime() - new Date(record.startedAt).getTime();
  if (milliseconds < 1000) return `${Math.max(0, milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`;
}

export function TerminalCommandHistoryPanel({ api, engagementId }: TerminalCommandHistoryPanelProps) {
  const confirm = useConfirmation();
  const [status, setStatus] = useState<TerminalCommandHistoryStatus>();
  const [records, setRecords] = useState<TerminalCommandRecord[]>([]);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [nextOffset, setNextOffset] = useState<number>();
  const [expanded, setExpanded] = useState<string>();
  const [outputs, setOutputs] = useState<Record<string, string>>({});
  const [outputLoading, setOutputLoading] = useState<string>();
  const [loading, setLoading] = useState(false);
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
      if (!signal?.aborted) setError(loadError instanceof Error ? loadError.message : "Could not load terminal audit records.");
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

  const toggleOutput = async (record: TerminalCommandRecord) => {
    if (expanded === record.id) {
      setExpanded(undefined);
      return;
    }
    setExpanded(record.id);
    if (outputs[record.id] !== undefined || !record.redactedOutputAvailable) return;
    setOutputLoading(record.id);
    setError(undefined);
    try {
      const blob = await api.terminalCommandOutput(engagementId, record.id);
      const text = await blob.text();
      setOutputs((current) => ({ ...current, [record.id]: text }));
    } catch (outputError) {
      setError(outputError instanceof Error ? outputError.message : "Could not load the recorded result.");
    } finally {
      setOutputLoading(undefined);
    }
  };

  const copyText = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      setError(`Could not copy ${label}.`);
    }
  };

  const downloadRaw = async (record: TerminalCommandRecord) => {
    const approved = await confirm({
      title: "Download unredacted terminal result?",
      message: "Raw terminal output can contain credentials, tokens, and other sensitive engagement data.",
      confirmLabel: "Download raw result",
      tone: "danger",
    });
    if (!approved) return;
    try {
      const blob = await api.terminalCommandOutput(engagementId, record.id, true);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `terminal-command-${record.id}.raw`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (downloadError) {
      setError(downloadError instanceof Error ? downloadError.message : "Could not download the raw result.");
    }
  };

  const warningCount = (status?.degradedCount ?? 0)
    + (status?.truncatedCount ?? 0)
    + (status?.auditGapCount ?? 0);

  return <section className="terminal-command-history panel" aria-labelledby="terminal-command-history-title">
    <header>
      <div><small>Immutable project record</small><h2 id="terminal-command-history-title"><History size={17} /> Terminal audit</h2><p>Completed human-terminal commands and their merged PTY results, attributed to an operator and retained for the Project lifetime.</p></div>
      <span className={`audit-health ${warningCount ? "degraded" : "active"}`}><ShieldCheck size={14} /> {warningCount ? `${warningCount} capture warning${warningCount === 1 ? "" : "s"}` : "Audit capture active"}</span>
    </header>
    <form className="history-search" onSubmit={submitSearch}><label><History size={14} /><span className="sr-only">Search terminal audit commands</span><input type="search" value={query} placeholder="Search exact commands" onChange={(event) => setQuery(event.target.value)} /></label><button className="button secondary" type="submit" disabled={loading}>Search</button></form>
    {error && <p className="form-error" role="alert">{error}</p>}
    {warningCount ? <p className="workspace-notice audit-warning" role="alert"><AlertTriangle size={14} /> One or more commands were interrupted, truncated, recovered after restart, or could not be durably persisted. Inspect the marked records before relying on the audit.</p> : null}
    <div className="terminal-command-list" aria-busy={loading}>
      {records.map((record, index) => <div className="terminal-audit-record" key={record.id}>
        {(index === 0 || records[index - 1]?.sessionId !== record.sessionId) && <div className="terminal-session-heading"><span>Session {record.sessionId.slice(0, 8)}</span><time dateTime={record.occurredAt}>{new Date(record.occurredAt).toLocaleString()}</time></div>}
        <article data-selection-source-kind="terminal_command" data-selection-source-id={record.id} data-selection-source-label="Terminal audit command">
          <button className="terminal-command-summary" type="button" aria-expanded={expanded === record.id} onClick={() => void toggleOutput(record)}>
            {expanded === record.id ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            <code>{record.command}</code>
          </button>
          <footer><span title={record.cwd}>{record.cwd || "/"}</span><span title={record.operatorId}>operator {record.operatorId ? (record.operatorId === "system" ? "system" : record.operatorId.slice(0, 8)) : "unknown (legacy)"}</span><span className={record.exitCode === 0 ? "success" : record.exitCode === undefined ? "warning" : "failure"}>{record.exitCode === undefined ? record.status.replaceAll("_", " ") : `exit ${record.exitCode}`}</span>{durationLabel(record) && <span>{durationLabel(record)}</span>}<time dateTime={record.occurredAt}>{new Date(record.occurredAt).toLocaleString()}</time></footer>
          {expanded === record.id && <div className="terminal-audit-output">
            <div className="terminal-audit-output-toolbar"><span>{sizeLabel(record.capturedOutputBytes)} captured{record.outputTruncated ? ` of ${sizeLabel(record.observedOutputBytes)}` : ""}</span><div><button className="button quiet" type="button" onClick={() => void copyText(record.command, "command")}><Copy size={13} /> Copy command</button>{outputs[record.id] !== undefined && <button className="button quiet" type="button" onClick={() => void copyText(outputs[record.id], "result")}><Copy size={13} /> Copy result</button>}<button className="button quiet" type="button" disabled={!record.rawOutputAvailable} onClick={() => void downloadRaw(record)}><Download size={13} /> Raw</button></div></div>
            {record.outputTruncated && <p className="terminal-output-warning"><AlertTriangle size={13} /> Result exceeded the 10 MiB capture limit. The full observed stream hash is retained.</p>}
            {record.captureError && <p className="terminal-output-warning"><AlertTriangle size={13} /> {record.captureError}</p>}
            {outputLoading === record.id ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading redacted result…</div> : record.redactedOutputAvailable ? <pre>{outputs[record.id] ?? record.outputPreview}</pre> : <p className="empty-output">No result artifact is available for this metadata-only record.</p>}
          </div>}
        </article>
      </div>)}
      {!records.length && !loading && <div className="empty-state compact"><History size={21} /><strong>{search ? "No matching commands" : "No commands recorded yet"}</strong><p>Completed shell commands and their results will appear here automatically.</p></div>}
      {loading && <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading terminal audit…</div>}
    </div>
    {nextOffset !== undefined && <button className="button quiet full" type="button" disabled={loading} onClick={() => void load(nextOffset)}>Load more</button>}
    {status && <footer className="history-retention">{status.recordCount.toLocaleString()} commands · {sizeLabel(status.capturedOutputBytes)} retained · Project-lifetime audit included in sensitive engagement exports.</footer>}
  </section>;
}
