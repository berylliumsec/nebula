import { useCallback, useEffect, useMemo, useState } from "react";
import { Ban, Clipboard, FileClock, LoaderCircle, MessageSquare, NotebookPen, Play, RefreshCw } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { OperatorExecution, ProviderHealth } from "../api/types";
import type { FencedRunCandidate } from "./AssistantMarkdown";
import { ExecutionInsightDialog } from "./ExecutionInsightDialog";

interface ExecutionHistoryProps {
  api: ApiClient;
  engagementId: string;
  refreshKey?: number;
  onRerun: (candidate: FencedRunCandidate) => void;
  providers: ProviderHealth[];
  onChatAttached: (sessionId: string) => void | Promise<void>;
}

const ACTIVE = new Set(["queued", "running", "cancelling"]);

function duration(execution: OperatorExecution): string {
  if (!execution.startedAt) return "Not launched";
  const end = execution.completedAt ? new Date(execution.completedAt).getTime() : Date.now();
  const milliseconds = Math.max(0, end - new Date(execution.startedAt).getTime());
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
}

export function ExecutionHistory({ api, engagementId, refreshKey = 0, onRerun, providers, onChatAttached }: ExecutionHistoryProps) {
  const [items, setItems] = useState<OperatorExecution[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [status, setStatus] = useState("");
  const [language, setLanguage] = useState("");
  const [operatorId, setOperatorId] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [stdout, setStdout] = useState("");
  const [stderr, setStderr] = useState("");
  const [stdoutNext, setStdoutNext] = useState(0);
  const [stderrNext, setStderrNext] = useState(0);
  const [stdoutTotal, setStdoutTotal] = useState(0);
  const [stderrTotal, setStderrTotal] = useState(0);
  const [insightAction, setInsightAction] = useState<"draft" | "chat">();
  const selected = useMemo(() => items.find((item) => item.id === selectedId), [items, selectedId]);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const exclusiveDateTo = dateTo
        ? new Date(new Date(`${dateTo}T00:00:00`).getTime() + 24 * 60 * 60 * 1000).toISOString()
        : undefined;
      const page = await api.listExecutions(engagementId, {
        status,
        language,
        operatorId,
        dateFrom: dateFrom ? new Date(`${dateFrom}T00:00:00`).toISOString() : undefined,
        dateTo: exclusiveDateTo,
        query,
      }, signal);
      setItems(page.items.sort((left, right) => right.queuedAt.localeCompare(left.queuedAt)));
      setSelectedId((current) => current && !page.items.some((item) => item.id === current) ? "" : current);
    } catch (loadError) {
      if (!signal?.aborted) setError(loadError instanceof Error ? loadError.message : "Could not load execution history.");
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, dateFrom, dateTo, engagementId, language, operatorId, query, status]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, refreshKey]);

  useEffect(() => {
    setStdout("");
    setStderr("");
    setStdoutNext(0);
    setStderrNext(0);
    setStdoutTotal(0);
    setStderrTotal(0);
    if (!selected || ACTIVE.has(selected.status) || (!selected.completedAt && selected.status !== "denied")) return;
    const controller = new AbortController();
    void Promise.allSettled([
      api.executionOutput(selected.id, "stdout", 0, controller.signal).then((page) => {
        setStdout(page.text); setStdoutNext(page.nextOffset); setStdoutTotal(page.totalBytes);
      }),
      api.executionOutput(selected.id, "stderr", 0, controller.signal).then((page) => {
        setStderr(page.text); setStderrNext(page.nextOffset); setStderrTotal(page.totalBytes);
      }),
    ]);
    return () => controller.abort();
  }, [api, selected]);

  const source = async (execution: OperatorExecution) => {
    const blob = await api.getArtifactContent(execution.sourceArtifactId);
    return blob.text();
  };

  const copySource = async (execution: OperatorExecution) => {
    try {
      await navigator.clipboard.writeText(await source(execution));
    } catch (copyError) {
      setError(copyError instanceof Error ? copyError.message : "Could not copy source.");
    }
  };

  const rerun = async (execution: OperatorExecution) => {
    try {
      onRerun({
        source: await source(execution),
        language: execution.language,
        declaredLanguage: execution.language,
        origin: { kind: "rerun", executionId: execution.id },
      });
    } catch (rerunError) {
      setError(rerunError instanceof Error ? rerunError.message : "Could not load source for review.");
    }
  };

  const cancel = async (execution: OperatorExecution) => {
    try {
      const updated = await api.cancelExecution(execution.id);
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (cancelError) {
      setError(cancelError instanceof Error ? cancelError.message : "Could not cancel execution.");
    }
  };

  const moreOutput = async (stream: "stdout" | "stderr") => {
    if (!selected) return;
    const offset = stream === "stdout" ? stdoutNext : stderrNext;
    try {
      const next = await api.executionOutput(selected.id, stream, offset);
      if (stream === "stdout") {
        setStdout((current) => current + next.text); setStdoutNext(next.nextOffset); setStdoutTotal(next.totalBytes);
      } else {
        setStderr((current) => current + next.text); setStderrNext(next.nextOffset); setStderrTotal(next.totalBytes);
      }
    } catch (outputError) {
      setError(outputError instanceof Error ? outputError.message : "Could not load more output.");
    }
  };

  return (
    <div className="execution-history">
      <header className="execution-history-toolbar">
        <label>Search<input value={query} placeholder="Source or operator" onChange={(event) => setQuery(event.target.value)} /></label>
        <label>Status<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All</option>{["queued", "running", "completed", "denied", "timed_out", "cancelled", "failed", "interrupted"].map((value) => <option value={value} key={value}>{value.replaceAll("_", " ")}</option>)}</select></label>
        <label>Language<select value={language} onChange={(event) => setLanguage(event.target.value)}><option value="">All</option><option value="bash">Bash</option><option value="sh">sh</option><option value="python">Python</option></select></label>
        <label>Operator<input value={operatorId} placeholder="Any operator" onChange={(event) => setOperatorId(event.target.value)} /></label>
        <label>From<input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
        <label>Through<input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label>
        <button className="button quiet" type="button" disabled={loading} onClick={() => void load()}><RefreshCw className={loading ? "spin" : undefined} size={14} /> Refresh</button>
      </header>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="execution-history-layout">
        <aside aria-label="Execution records">
          {loading && !items.length ? <div className="empty-state compact"><LoaderCircle className="spin" size={20} /><strong>Loading executions…</strong></div> : items.length ? items.map((execution) => (
            <button type="button" className={execution.id === selectedId ? "active" : undefined} onClick={() => setSelectedId(execution.id)} key={execution.id}>
              <span className={`execution-status ${execution.status}`} />
              <span><strong>{execution.language} · {execution.status.replaceAll("_", " ")}</strong><small>{new Date(execution.queuedAt).toLocaleString()} · {execution.operatorId}</small><code>{execution.sourcePreview.slice(0, 90) || execution.sourceSha256.slice(0, 16)}</code></span>
            </button>
          )) : <div className="empty-state compact"><FileClock size={21} /><strong>No executions match</strong><p>Reviewed code runs will appear here.</p></div>}
        </aside>
        <section className="execution-detail">
          {selected ? <>
            <header><div><h3>{selected.language} execution</h3><p>{selected.status.replaceAll("_", " ")} · exit {selected.exitCode ?? "—"} · {duration(selected)}</p></div><div><button className="button quiet" type="button" onClick={() => void copySource(selected)}><Clipboard size={13} /> Copy</button><button className="button secondary" type="button" onClick={() => void rerun(selected)}><Play size={13} /> Rerun through review</button>{!ACTIVE.has(selected.status) && <><button className="button secondary" type="button" onClick={() => setInsightAction("draft")}><NotebookPen size={13} /> Draft note</button><button className="button secondary" type="button" onClick={() => setInsightAction("chat")}><MessageSquare size={13} /> Discuss in chat</button></>}{ACTIVE.has(selected.status) && <button className="button danger" type="button" onClick={() => void cancel(selected)}><Ban size={13} /> Cancel</button>}</div></header>
            <dl><div><dt>Image</dt><dd><code>{selected.runtime.image}</code></dd></div><div><dt>Manifest</dt><dd><code>{selected.runtime.manifestDigest}</code></dd></div><div><dt>Network</dt><dd>{selected.network.mode === "none" ? "Offline" : `${selected.network.target} · ${selected.network.ports.join(", ")}`}</dd></div><div><dt>Policy</dt><dd>{selected.policyDecision}{selected.errorCode ? ` · ${selected.errorCode}` : ""}</dd></div></dl>
            {selected.errorDetail && <p className="execution-error">{selected.errorDetail}</p>}
            {selected.workspaceChanges.length > 0 && <details><summary>{selected.workspaceChanges.length} workspace change{selected.workspaceChanges.length === 1 ? "" : "s"}</summary><ul>{selected.workspaceChanges.map((change) => <li key={`${change.change}-${change.path}`}><span>{change.change}</span><code>{change.path}</code></li>)}</ul></details>}
            <div className="execution-output-grid">
              <section><h4>stdout {selected.outputTruncated && <em>capture truncated</em>}</h4><pre>{stdout || "No captured stdout."}</pre>{stdoutNext < stdoutTotal && <button className="button quiet" type="button" onClick={() => void moreOutput("stdout")}>Load next 256 KiB</button>}</section>
              <section><h4>stderr</h4><pre>{stderr || "No captured stderr."}</pre>{stderrNext < stderrTotal && <button className="button quiet" type="button" onClick={() => void moreOutput("stderr")}>Load next 256 KiB</button>}</section>
            </div>
          </> : <div className="empty-state"><FileClock size={24} /><strong>Select an execution</strong><p>Output is loaded lazily and shown only in its redacted form here.</p></div>}
        </section>
      </div>
      {selected && insightAction && <ExecutionInsightDialog action={insightAction} api={api} execution={selected} providers={providers} onClose={() => setInsightAction(undefined)} onChatAttached={onChatAttached} />}
    </div>
  );
}
