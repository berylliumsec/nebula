import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Copy, Download, History, LoaderCircle, Plus, RotateCcw, Save, ShieldCheck, Trash2, Wrench } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { TerminalCommandHistoryStatus, TerminalCommandRecord, TerminalRecordingTools } from "../api/types";
import { useConfirmation } from "./DialogSystem";
import "./TerminalCommandHistoryPanel.css";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

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

function captureDecisionLabel(record: TerminalCommandRecord): string {
  if (record.captureDecision === "selected_tool") return `output recorded${record.matchedTools.length ? ` · ${record.matchedTools.join(", ")}` : ""}`;
  if (record.captureDecision === "not_selected") return "metadata only · no selected tool";
  if (record.captureDecision === "classification_failed") return "metadata only · classification failed";
  if (record.captureDecision === "capture_failed") return "selected output capture failed";
  if (record.captureDecision === "legacy_all_commands") return "legacy full output";
  return "legacy metadata only";
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
  const [tools, setTools] = useState<TerminalRecordingTools>();
  const [customTools, setCustomTools] = useState<string[]>([]);
  const [disabledTools, setDisabledTools] = useState<string[]>([]);
  const [toolQuery, setToolQuery] = useState("");
  const [newTool, setNewTool] = useState("");
  const [savingTools, setSavingTools] = useState(false);
  const [error, setError] = useState<string>();

  const applyTools = (next: TerminalRecordingTools) => {
    setTools(next);
    setCustomTools(next.customTools);
    setDisabledTools(next.disabledTools);
  };

  const load = useCallback(async (offset = 0, signal?: AbortSignal) => {
    setLoading(true);
    setError(undefined);
    try {
      const [nextStatus, page, nextTools] = await Promise.all([
        api.terminalCommandHistoryStatus(engagementId, signal),
        api.listTerminalCommands(engagementId, search, offset, 100, signal),
        api.terminalRecordingTools(engagementId, signal),
      ]);
      setStatus(nextStatus);
      if (offset === 0) applyTools(nextTools);
      setRecords((current) => offset ? [...current, ...page.records] : page.records);
      setNextOffset(page.nextOffset);
    } catch (loadError) {
      void logCaughtDiagnostic("interface.terminal_command_history_panel.caught_failure_01", "A handled interface operation failed.", loadError, "terminal_command_history_panel");
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

  const availableTools = useMemo(
    () => [...new Set([...(tools?.defaultTools ?? []), ...customTools])].sort(),
    [customTools, tools?.defaultTools],
  );
  const visibleTools = useMemo(() => {
    const queryText = toolQuery.trim().toLocaleLowerCase();
    return queryText ? availableTools.filter((tool) => tool.toLocaleLowerCase().includes(queryText)) : availableTools;
  }, [availableTools, toolQuery]);
  const dirtyTools = tools !== undefined && (
    JSON.stringify(customTools) !== JSON.stringify(tools.customTools)
    || JSON.stringify(disabledTools) !== JSON.stringify(tools.disabledTools)
  );

  const toggleTool = (tool: string, enabled: boolean) => {
    setDisabledTools((current) => enabled
      ? current.filter((item) => item !== tool)
      : [...new Set([...current, tool])].sort());
  };

  const addCustomTool = () => {
    const normalized = newTool.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._+@-]{0,127}$/.test(normalized)) {
      setError("Custom tools must be executable basenames without spaces, paths, or shell syntax.");
      return;
    }
    setCustomTools((current) => [...new Set([...current, normalized])].sort());
    setDisabledTools((current) => current.filter((item) => item !== normalized));
    setNewTool("");
    setError(undefined);
  };

  const removeCustomTool = (tool: string) => {
    setCustomTools((current) => current.filter((item) => item !== tool));
    setDisabledTools((current) => current.filter((item) => item !== tool));
  };

  const saveTools = async () => {
    if (!tools) return;
    setSavingTools(true);
    setError(undefined);
    try {
      applyTools(await api.updateTerminalRecordingTools(engagementId, {
        customTools: [...customTools].sort(),
        disabledTools: [...disabledTools].sort(),
        expectedRevision: tools.revision,
        expectedManifestSha256: tools.manifestSha256,
      }));
    } catch (saveError) {
      void logCaughtDiagnostic("interface.terminal_command_history_panel.caught_failure_02", "A handled interface operation failed.", saveError, "terminal_command_history_panel");
      setError(saveError instanceof Error ? saveError.message : "Could not save recorded security tools.");
    } finally {
      setSavingTools(false);
    }
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
      void logCaughtDiagnostic("interface.terminal_command_history_panel.caught_failure_03", "A handled interface operation failed.", outputError, "terminal_command_history_panel");
      setError(outputError instanceof Error ? outputError.message : "Could not load the recorded result.");
    } finally {
      setOutputLoading(undefined);
    }
  };

  const copyText = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.terminal_command_history_panel.caught_failure_04", "A handled interface operation failed.", caughtError, "terminal_command_history_panel");
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
      void logCaughtDiagnostic("interface.terminal_command_history_panel.caught_failure_05", "A handled interface operation failed.", downloadError, "terminal_command_history_panel");
      setError(downloadError instanceof Error ? downloadError.message : "Could not download the raw result.");
    }
  };

  const warningCount = (status?.degradedCount ?? 0)
    + (status?.truncatedCount ?? 0)
    + (status?.auditGapCount ?? 0)
    + (status?.classificationFailureCount ?? 0);

  return <section className="terminal-command-history panel" aria-labelledby="terminal-command-history-title">
    <header>
      <div><small>Immutable project record</small><h2 id="terminal-command-history-title"><History size={17} /> Terminal audit</h2><p>Command metadata is retained for the Project lifetime. Merged PTY output is retained only when a selected security tool runs.</p></div>
      <span className={`audit-health ${warningCount ? "degraded" : "active"}`}><ShieldCheck size={14} /> {warningCount ? `${warningCount} capture warning${warningCount === 1 ? "" : "s"}` : "Selective capture active"}</span>
    </header>
    <section className="terminal-tool-editor" aria-labelledby="recorded-security-tools-title">
      <header>
        <div><h3 id="recorded-security-tools-title"><Wrench size={15} /> Recorded security tools</h3><p>Defaults come from executable security packages in the verified Kali image. Changes apply to the next top-level command.</p></div>
        <div className="terminal-tool-editor-actions">
          <button className="button quiet" type="button" disabled={!tools || savingTools} onClick={() => { setCustomTools([]); setDisabledTools([]); }}><RotateCcw size={13} /> Reset defaults</button>
          <button className="button primary" type="button" disabled={!dirtyTools || savingTools} onClick={() => void saveTools()}><Save size={13} /> {savingTools ? "Saving…" : "Save tools"}</button>
        </div>
      </header>
      {tools?.inventoryStatus === "verified" ? <p className="terminal-tool-provenance"><ShieldCheck size={13} /> {tools.defaultTools.length.toLocaleString()} image defaults · image <code title={tools.runtimeImageDigest}>{tools.runtimeImageDigest?.slice(0, 19)}…</code> · manifest <code title={tools.manifestSha256}>{tools.manifestSha256?.slice(0, 12)}…</code> · policy revision {tools.revision}</p> : <p className="workspace-notice"><AlertTriangle size={13} /> The verified Kali catalog is not available yet. Start image preparation to load defaults; custom names can still be saved.</p>}
      <div className="terminal-tool-controls">
        <label><span className="sr-only">Search recorded security tools</span><input type="search" value={toolQuery} placeholder="Search security tools" onChange={(event) => setToolQuery(event.target.value)} /></label>
        <button className="button quiet" type="button" disabled={!availableTools.length} onClick={() => setDisabledTools((current) => current.filter((item) => !availableTools.includes(item)))}>Select all</button>
        <button className="button quiet" type="button" disabled={!availableTools.length} onClick={() => setDisabledTools((current) => [...new Set([...current, ...availableTools])].sort())}>Deselect all</button>
      </div>
      <div className="terminal-tool-list">
        {visibleTools.map((tool) => {
          const custom = customTools.includes(tool);
          return <label key={tool}><input type="checkbox" checked={!disabledTools.includes(tool)} onChange={(event) => toggleTool(tool, event.target.checked)} /><span><strong>{tool}</strong><small>{custom ? "custom" : "Kali image default"}</small></span>{custom && <button className="icon-button subtle" type="button" aria-label={`Remove custom tool ${tool}`} onClick={(event) => { event.preventDefault(); removeCustomTool(tool); }}><Trash2 size={12} /></button>}</label>;
        })}
        {!visibleTools.length && <p>{toolQuery ? "No matching tools." : "No image defaults or custom tools are available."}</p>}
      </div>
      <div className="terminal-tool-add"><label><span className="sr-only">Custom executable name</span><input value={newTool} placeholder="Add executable name" maxLength={128} onChange={(event) => setNewTool(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); addCustomTool(); } }} /></label><button className="button secondary" type="button" disabled={!newTool.trim()} onClick={addCustomTool}><Plus size={13} /> Add custom</button></div>
    </section>
    <form className="history-search" onSubmit={submitSearch}><label><History size={14} /><span className="sr-only">Search terminal audit commands</span><input type="search" value={query} placeholder="Search exact commands" onChange={(event) => setQuery(event.target.value)} /></label><button className="button secondary" type="submit" disabled={loading}>Search</button></form>
    {error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}
    {warningCount ? <p className="workspace-notice audit-warning" role="alert"><AlertTriangle size={14} /> One or more selected captures were interrupted, truncated, could not be classified, recovered after restart, or could not be durably persisted. Inspect the marked records before relying on the audit.</p> : null}
    <div className="terminal-command-list" aria-busy={loading}>
      {records.map((record, index) => <div className="terminal-audit-record" key={record.id}>
        {(index === 0 || records[index - 1]?.sessionId !== record.sessionId) && <div className="terminal-session-heading"><span>Session {record.sessionId.slice(0, 8)}</span><time dateTime={record.occurredAt}>{new Date(record.occurredAt).toLocaleString()}</time></div>}
        <article data-selection-source-kind="terminal_command" data-selection-source-id={record.id} data-selection-source-label="Terminal audit command">
          <button className="terminal-command-summary" type="button" aria-expanded={expanded === record.id} onClick={() => void toggleOutput(record)}>
            {expanded === record.id ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            <code>{record.command}</code>
          </button>
          <footer><span title={record.cwd}>{record.cwd || "/"}</span><span title={record.operatorId}>operator {record.operatorId ? (record.operatorId === "system" ? "system" : record.operatorId.slice(0, 8)) : "unknown (legacy)"}</span><span className={record.exitCode === 0 ? "success" : record.exitCode === undefined ? "warning" : "failure"}>{record.exitCode === undefined ? record.status.replaceAll("_", " ") : `exit ${record.exitCode}`}</span><span className={record.captureDecision === "classification_failed" || record.captureDecision === "capture_failed" ? "warning" : undefined}>{captureDecisionLabel(record)}</span>{durationLabel(record) && <span>{durationLabel(record)}</span>}<time dateTime={record.occurredAt}>{new Date(record.occurredAt).toLocaleString()}</time></footer>
          {expanded === record.id && <div className="terminal-audit-output">
            <div className="terminal-audit-output-toolbar"><span>{sizeLabel(record.capturedOutputBytes)} captured{record.outputTruncated ? ` of ${sizeLabel(record.observedOutputBytes)}` : ""}</span><div><button className="button quiet" type="button" onClick={() => void copyText(record.command, "command")}><Copy size={13} /> Copy command</button>{outputs[record.id] !== undefined && <button className="button quiet" type="button" onClick={() => void copyText(outputs[record.id], "result")}><Copy size={13} /> Copy result</button>}<button className="button quiet" type="button" disabled={!record.rawOutputAvailable} onClick={() => void downloadRaw(record)}><Download size={13} /> Raw</button></div></div>
            {record.outputTruncated && <p className="terminal-output-warning"><AlertTriangle size={13} /> Result exceeded the 10 MiB capture limit. The full observed stream hash is retained.</p>}
            {record.captureError && <p className="terminal-output-warning"><AlertTriangle size={13} /> {record.captureError}</p>}
            {outputLoading === record.id ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading redacted result…</div> : record.redactedOutputAvailable ? <pre>{outputs[record.id] ?? record.outputPreview}</pre> : <p className="empty-output">Output was not retained for this metadata-only record.</p>}
          </div>}
        </article>
      </div>)}
      {!records.length && !loading && <div className="empty-state compact"><History size={21} /><strong>{search ? "No matching commands" : "No commands recorded yet"}</strong><p>Completed shell commands will appear here; selected security tools also retain their results.</p></div>}
      {loading && <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading terminal audit…</div>}
    </div>
    {nextOffset !== undefined && <button className="button quiet full" type="button" disabled={loading} onClick={() => void load(nextOffset)}>Load more</button>}
    {status && <footer className="history-retention">{status.recordCount.toLocaleString()} commands · {status.recordedOutputCount.toLocaleString()} selected results · {status.metadataOnlyCount.toLocaleString()} metadata only · {sizeLabel(status.capturedOutputBytes)} retained · included in sensitive engagement exports.</footer>}
  </section>;
}
