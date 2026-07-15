import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  CircleDashed,
  Copy,
  Download,
  ExternalLink,
  FileText,
  FolderOpen,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import type { HealthResponse, SetupStatus } from "../api/types";
import { useConfirmation } from "../components/DialogSystem";
import { useWorkspace } from "../state/WorkspaceContext";
import {
  diagnosticsFallbackErrors,
  isDiagnosticsAvailable,
  logCaughtDiagnostic,
  logDiagnostic,
  nativeDiagnosticFiles,
  nativeDiagnosticSettings,
  nativeDiagnosticStatus,
  nativeRecentErrors,
  normalizeDiagnosticSettings,
  revealNativeLogs,
  setDiagnosticSettings,
  updateNativeDiagnosticSettings,
} from "./logger";
import {
  diagnosticFailurePresentation,
  diagnosticRecordMatchesReference,
  diagnosticTechnicalDetails,
  humanizeDiagnosticValue,
} from "./presentation";
import {
  diagnosticFeatures,
  type DiagnosticFile,
  type DiagnosticLevel,
  type DiagnosticRecord,
  type DiagnosticSettings,
  type DiagnosticStatus,
} from "./types";
import { DiagnosticErrorNotice } from "./DiagnosticErrorNotice";

const levels: Array<{ value: DiagnosticLevel; label: string; description: string }> = [
  { value: "error", label: "Errors", description: "Operation failures and cleanup gaps only (recommended)." },
  { value: "warning", label: "Warnings", description: "Also include degraded behavior, denials, fallbacks, and retries." },
  { value: "info", label: "Info", description: "Also include lifecycle and meaningful state changes." },
  { value: "debug", label: "Debug", description: "Also include bounded decisions, counts, and timings." },
  { value: "critical", label: "Critical only", description: "Only startup, integrity, security, or logger failures." },
];

function isNative(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KiB`;
  return `${(value / 1_048_576).toFixed(1)} MiB`;
}

function mergeErrors(...groups: DiagnosticRecord[][]): DiagnosticRecord[] {
  const records = new Map<string, DiagnosticRecord>();
  for (const record of groups.flat()) {
    const key = record.error_id
      ?? `${record.timestamp ?? "session"}:${record.sequence ?? 0}:${record.event_code}`;
    records.set(key, record);
  }
  return [...records.values()].sort((left, right) => {
    const timestampOrder = (left.timestamp ?? "").localeCompare(right.timestamp ?? "");
    return timestampOrder || (left.sequence ?? 0) - (right.sequence ?? 0);
  });
}

type StatusTone = "healthy" | "attention" | "unavailable" | "checking";

function StatusCard({ label, title, detail, tone }: {
  label: string;
  title: string;
  detail: string;
  tone: StatusTone;
}) {
  const Icon = tone === "healthy" ? CheckCircle2 : tone === "checking" ? CircleDashed : ShieldAlert;
  return (
    <article className={`diagnostics-status-card ${tone}`}>
      <Icon size={18} className={tone === "checking" ? "spin" : undefined} />
      <span><small>{label}</small><strong>{title}</strong><p>{detail}</p></span>
    </article>
  );
}

function FailureCard({ record, targeted }: { record: DiagnosticRecord; targeted: boolean }) {
  const presentation = diagnosticFailurePresentation(record);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const correlations = [
    ["Error reference", record.error_id],
    ["Request reference", record.request_id],
    ["Operation reference", record.operation_id],
    ["Parent operation", record.parent_operation_id],
    ["Project reference", record.project_id],
    ["Run reference", record.run_id],
    ["Execution reference", record.execution_id],
    ["Session reference", record.session_id],
  ].filter((item): item is [string, string] => Boolean(item[1]));

  const copyDetails = async () => {
    try {
      await navigator.clipboard.writeText(diagnosticTechnicalDetails(record));
      setCopyState("copied");
    } catch (error) {
      setCopyState("failed");
      void logCaughtDiagnostic(
        "interface.diagnostics.copy_failed",
        "Diagnostic technical details could not be copied.",
        error,
        "copy-details",
      );
    }
  };

  return (
    <article className={`diagnostic-failure-card${targeted ? " targeted" : ""}`} tabIndex={targeted ? -1 : undefined}>
      <header>
        <span className={`diagnostic-level ${String(record.level).toLowerCase()}`}>{record.level}</span>
        <div>
          <small>{presentation.operationLabel}</small>
          <h4>{record.message}</h4>
        </div>
        <time dateTime={record.timestamp}>{record.timestamp ? new Date(record.timestamp).toLocaleString() : "This session"}</time>
      </header>
      <div className="diagnostic-failure-explanation">
        <span><small>Why it failed</small><p>{presentation.cause}</p></span>
        <span><small>Verified next step</small><p>{presentation.recovery}</p></span>
      </div>
      <div className="diagnostic-failure-actions">
        {presentation.destination && presentation.actionLabel && (
          <a className="button secondary" href={presentation.destination}>{presentation.actionLabel} <ExternalLink size={13} /></a>
        )}
      </div>
      <details className="diagnostic-technical-details" open={targeted || undefined}>
        <summary>Technical details</summary>
        <dl>
          <div><dt>Technical code</dt><dd>{record.event_code}</dd></div>
          <div><dt>Feature</dt><dd>{record.feature}</dd></div>
          <div><dt>Stage</dt><dd>{record.stage ?? "unspecified"}</dd></div>
          <div><dt>Outcome</dt><dd>{record.outcome ?? "unspecified"}</dd></div>
          <div><dt>Retryable</dt><dd>{record.retryable === true ? "Yes" : record.retryable === false ? "No" : "Not classified"}</dd></div>
          {record.duration_ms !== undefined && <div><dt>Duration</dt><dd>{record.duration_ms} ms</dd></div>}
          <div><dt>Failure type</dt><dd>{record.exception_type ?? "not recorded"}</dd></div>
          {record.exception_chain?.length && <div><dt>Exception chain</dt><dd>{record.exception_chain.join(" → ")}</dd></div>}
          {correlations.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
          {record.source && <div><dt>Source</dt><dd>{record.source}</dd></div>}
          {record.application_version && <div><dt>Application version</dt><dd>{record.application_version}</dd></div>}
        </dl>
        {record.stack_frames?.length ? (
          <div className="diagnostic-stack"><small>Sanitized stack</small><ol>{record.stack_frames.map((frame, index) => <li key={`${frame.module}-${frame.function}-${frame.line}-${index}`}><code>{frame.module}.{frame.function}:{frame.line}</code></li>)}</ol></div>
        ) : null}
        {record.metadata && Object.keys(record.metadata).length ? (
          <div className="diagnostic-metadata"><small>Sanitized metadata</small><pre>{JSON.stringify(record.metadata, null, 2)}</pre></div>
        ) : null}
        <button className="button quiet" type="button" onClick={() => void copyDetails()}><Copy size={13} /> {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy unavailable" : "Copy technical details"}</button>
      </details>
    </article>
  );
}

export function DiagnosticsAvailabilityBanner() {
  const [available, setAvailable] = useState(isDiagnosticsAvailable);

  useEffect(() => {
    const update = (event: Event) => {
      const detail = (event as CustomEvent<{ available: boolean }>).detail;
      setAvailable(detail.available);
    };
    window.addEventListener("nebula-diagnostics-health", update);
    return () => window.removeEventListener("nebula-diagnostics-health", update);
  }, []);

  if (available) return null;
  return (
    <div className="diagnostics-unavailable" role="status">
      <ShieldAlert size={16} />
      <span><strong>Local diagnostics are unavailable.</strong> New failures are being retained in memory for this session.</span>
      <a href="/settings#diagnostics-settings">Diagnostics</a>
    </div>
  );
}

export function DiagnosticsPanel({ hidden = false }: { hidden?: boolean } = {}) {
  const {
    api,
    coreError,
    health: workspaceHealth,
    setupStatus: workspaceSetup,
    workspaceState,
  } = useWorkspace();
  const confirm = useConfirmation();
  const [settings, setSettings] = useState<DiagnosticSettings>();
  const [draft, setDraft] = useState<DiagnosticSettings>();
  const [status, setStatus] = useState<DiagnosticStatus>();
  const [liveHealth, setLiveHealth] = useState<HealthResponse>();
  const [liveSetup, setLiveSetup] = useState<SetupStatus>();
  const [files, setFiles] = useState<DiagnosticFile[]>([]);
  const [errors, setErrors] = useState<DiagnosticRecord[]>([]);
  const [errorsLoaded, setErrorsLoaded] = useState(false);
  const [loadFailures, setLoadFailures] = useState<string[]>([]);
  const [feature, setFeature] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>();
  const native = isNative();
  const targetReference = useMemo(
    () => new URLSearchParams(window.location.search).get("diagnostic") ?? "",
    [],
  );

  const refresh = useCallback(async () => {
    setBusy(true);
    setFailure(undefined);
    setLoadFailures([]);
    setErrorsLoaded(false);
    const unavailable: string[] = [];
    const caught: unknown[] = [];
    const noteFailure = (label: string, reason: unknown) => {
      unavailable.push(label);
      caught.push(reason);
    };

    try {
      if (native) {
        const [settingsResult, statusResult, filesResult, errorsResult] = await Promise.allSettled([
          nativeDiagnosticSettings(),
          nativeDiagnosticStatus(),
          nativeDiagnosticFiles(),
          nativeRecentErrors(feature || undefined, undefined, 100),
        ]);
        if (settingsResult.status === "fulfilled") {
          const normalized = normalizeDiagnosticSettings(settingsResult.value);
          setSettings(normalized);
          setDraft(normalized);
        } else noteFailure("logging preferences", settingsResult.reason);
        if (statusResult.status === "fulfilled") setStatus(statusResult.value);
        else noteFailure("logger health", statusResult.reason);
        let nextFiles = filesResult.status === "fulfilled" && Array.isArray(filesResult.value) ? filesResult.value : [];
        if (filesResult.status === "rejected") noteFailure("local file inventory", filesResult.reason);
        const recent = errorsResult.status === "fulfilled" && Array.isArray(errorsResult.value) ? errorsResult.value : [];
        if (errorsResult.status === "rejected") noteFailure("recent failures", errorsResult.reason);
        setErrors(mergeErrors(recent, diagnosticsFallbackErrors()));
        setErrorsLoaded(true);

        if (api && workspaceState !== "failed") {
          const [coreFilesResult, healthResult, setupResult] = await Promise.allSettled([
            api.diagnosticsFiles(),
            api.health(),
            api.setupStatus(),
          ]);
          if (coreFilesResult.status === "fulfilled") {
            const coreFiles = Array.isArray(coreFilesResult.value?.files) ? coreFilesResult.value.files : [];
            nextFiles = [...new Map([...nextFiles, ...coreFiles].map((file) => [file.name, file])).values()]
              .sort((left, right) => left.name.localeCompare(right.name));
            if (coreFilesResult.value?.health) {
              const coreStatus = coreFilesResult.value.health;
              setStatus((current) => current ? ({
                ...current,
                writable: current.writable && coreStatus.writable,
                degraded: current.degraded || coreStatus.degraded,
                disk_usage_bytes: Math.max(current.disk_usage_bytes, coreStatus.disk_usage_bytes),
                dropped_record_count: current.dropped_record_count + coreStatus.dropped_record_count,
                queued_record_count: coreStatus.queued_record_count,
                last_rotation: [current.last_rotation, coreStatus.last_rotation]
                  .filter((value): value is string => Boolean(value)).sort().at(-1),
                last_failure: coreStatus.degraded ? coreStatus.last_failure : current.last_failure,
              }) : coreStatus);
            }
          } else noteFailure("Core diagnostic health", coreFilesResult.reason);
          if (healthResult.status === "fulfilled") setLiveHealth(healthResult.value);
          else noteFailure("Core status", healthResult.reason);
          if (setupResult.status === "fulfilled") setLiveSetup(setupResult.value);
          else noteFailure("runtime status", setupResult.reason);
        }
        setFiles(nextFiles);
      } else if (api) {
        const [settingsResult, filesResult, errorsResult, healthResult, setupResult] = await Promise.allSettled([
          api.diagnosticsSettings(),
          api.diagnosticsFiles(),
          api.diagnosticErrors(feature || undefined),
          api.health(),
          api.setupStatus(),
        ]);
        if (settingsResult.status === "fulfilled") {
          const normalized = normalizeDiagnosticSettings(settingsResult.value);
          setDiagnosticSettings(normalized);
          setSettings(normalized);
          setDraft(normalized);
        } else noteFailure("logging preferences", settingsResult.reason);
        if (filesResult.status === "fulfilled") {
          setStatus(filesResult.value?.health);
          setFiles(Array.isArray(filesResult.value?.files) ? filesResult.value.files : []);
        } else noteFailure("logger health and files", filesResult.reason);
        const recent = errorsResult.status === "fulfilled" && Array.isArray(errorsResult.value) ? errorsResult.value : [];
        if (errorsResult.status === "rejected") noteFailure("recent failures", errorsResult.reason);
        setErrors(mergeErrors(recent, diagnosticsFallbackErrors()));
        setErrorsLoaded(true);
        if (healthResult.status === "fulfilled") setLiveHealth(healthResult.value);
        else noteFailure("Core status", healthResult.reason);
        if (setupResult.status === "fulfilled") setLiveSetup(setupResult.value);
        else noteFailure("runtime status", setupResult.reason);
      } else {
        noteFailure("Core connection", new Error("Nebula Core is not connected."));
        setErrors(mergeErrors(diagnosticsFallbackErrors()));
        setErrorsLoaded(true);
      }
    } finally {
      setLoadFailures([...new Set(unavailable)]);
      if (caught.length) {
        void logDiagnostic({
          level: "error",
          eventCode: "interface.diagnostics.viewer_load_failed",
          message: "Part of the Diagnostics viewer could not load local diagnostic state.",
          outcome: "degraded",
          stage: "viewer-read",
          retryable: true,
          safeFailureCause: "One or more independent diagnostic data sources were unavailable.",
          exception: caught[0],
          metadata: { count: caught.length },
        });
      }
      setBusy(false);
    }
  }, [api, feature, native, workspaceState]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const targetedRecord = useMemo(
    () => targetReference ? errors.find((record) => diagnosticRecordMatchesReference(record, targetReference)) : undefined,
    [errors, targetReference],
  );

  useEffect(() => {
    if (!targetedRecord) return;
    window.requestAnimationFrame(() => {
      const element = document.querySelector<HTMLElement>(".diagnostic-failure-card.targeted");
      element?.scrollIntoView?.({ block: "center" });
      element?.focus();
    });
  }, [targetedRecord]);

  const changed = useMemo(
    () => JSON.stringify(settings) !== JSON.stringify(draft),
    [draft, settings],
  );

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setFailure(undefined);
    try {
      const updated = native
        ? await updateNativeDiagnosticSettings(draft)
        : await api?.updateDiagnosticsSettings(draft);
      if (!updated) throw new Error("Nebula Core is not connected.");
      const normalizedSettings = normalizeDiagnosticSettings(updated);
      setDiagnosticSettings(normalizedSettings);
      setSettings(normalizedSettings);
      setDraft(normalizedSettings);
    } catch (error) {
      setFailure(error);
      void logDiagnostic({
        level: "error",
        eventCode: "interface.diagnostics.settings_save_failed",
        message: "Diagnostics preferences could not be saved.",
        outcome: "failure",
        stage: "settings-write",
        retryable: true,
        exception: error,
      });
    } finally {
      setBusy(false);
    }
  };

  const reveal = async () => {
    try {
      await revealNativeLogs();
      void logDiagnostic({
        level: "info",
        eventCode: "interface.diagnostics.folder_revealed",
        message: "The fixed diagnostics directory was revealed.",
        outcome: "success",
        stage: "open-folder",
      });
    } catch (error) {
      setFailure(error);
      void logDiagnostic({
        level: "error",
        eventCode: "interface.diagnostics.folder_reveal_failed",
        message: "The diagnostics directory could not be revealed.",
        outcome: "failure",
        stage: "open-folder",
        retryable: true,
        exception: error,
      });
    }
  };

  const exportBundle = async () => {
    if (!api) {
      setFailure(new Error("Nebula Core must be connected to create a sanitized support bundle."));
      return;
    }
    const approved = await confirm({
      title: "Export local diagnostics?",
      message: "The ZIP is sanitized again before export and contains logs, build/platform metadata, logger health, settings, and a SHA-256 manifest. It excludes project databases, workspaces, evidence, terminal results, provider configuration, and credentials.",
      confirmLabel: "Export diagnostics",
    });
    if (!approved) return;
    setBusy(true);
    try {
      const blob = await api.exportDiagnostics();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "nebula-diagnostics.zip";
      link.click();
      URL.revokeObjectURL(url);
      void logDiagnostic({
        level: "info",
        eventCode: "interface.diagnostics.export_downloaded",
        message: "A sanitized diagnostics export was downloaded.",
        outcome: "success",
        stage: "export",
      });
    } catch (error) {
      setFailure(error);
      void logDiagnostic({
        level: "error",
        eventCode: "interface.diagnostics.export_failed",
        message: "A sanitized diagnostics export could not be downloaded.",
        outcome: "failure",
        stage: "export",
        retryable: true,
        exception: error,
      });
    } finally {
      setBusy(false);
    }
  };

  const currentHealth = liveHealth ?? workspaceHealth;
  const currentSetup = liveSetup ?? workspaceSetup;
  const coreStatus: { tone: StatusTone; title: string; detail: string } = workspaceState === "failed"
    ? { tone: "unavailable", title: "Core is unavailable", detail: coreError ?? "Nebula Core could not be reached." }
    : workspaceState === "starting"
      ? { tone: "checking", title: "Checking Core", detail: "Waiting for the local service to respond." }
      : loadFailures.includes("Core status")
        ? { tone: "unavailable", title: "Core health was not confirmed", detail: "The latest Core health check did not complete." }
      : currentHealth?.status === "degraded" || (currentSetup !== undefined && currentSetup.core.status !== "ready")
        ? { tone: "attention", title: "Core needs attention", detail: currentSetup?.core.detail ?? "Some local capabilities are limited." }
        : { tone: "healthy", title: "Core is responding", detail: "The local API and database health check completed." };
  const runtimeStatus: { tone: StatusTone; title: string; detail: string } = workspaceState === "failed"
    ? { tone: "unavailable", title: "Runtime was not checked", detail: "Reconnect Core to check Terminal readiness." }
    : loadFailures.includes("runtime status")
      ? { tone: "unavailable", title: "Runtime health was not confirmed", detail: "The latest Terminal readiness check did not complete." }
    : !currentSetup
      ? { tone: "checking", title: "Runtime status pending", detail: "Terminal readiness has not been reported yet." }
      : currentSetup.terminal.status === "ready"
        ? { tone: "healthy", title: "Terminal runtime is ready", detail: currentSetup.terminal.detail ?? "A verified local container runtime is available." }
        : ["detecting_runner", "preparing_image"].includes(currentSetup.terminal.status)
          ? { tone: "checking", title: humanizeDiagnosticValue(currentSetup.terminal.status), detail: currentSetup.terminal.detail ?? "Runtime setup is still in progress." }
          : { tone: "attention", title: "Terminal runtime needs attention", detail: currentSetup.terminal.detail ?? `Runtime is ${humanizeDiagnosticValue(currentSetup.terminal.status).toLowerCase()}.` };
  const loggerStatus: { tone: StatusTone; title: string; detail: string } = !status
    ? { tone: loadFailures.some((item) => item.includes("logger")) ? "unavailable" : "checking", title: "Logger status unavailable", detail: "Nebula could not confirm local diagnostic storage." }
    : status.writable && !status.degraded
      ? { tone: "healthy", title: "Local logging is healthy", detail: "Failure records can be written to local diagnostic storage." }
      : { tone: "attention", title: "Local logging needs attention", detail: typeof status.last_failure === "string" ? status.last_failure : status.last_failure?.message ?? "The local diagnostic sink is degraded." };

  return (
    <section className="settings-section diagnostics-panel" id="diagnostics-settings" hidden={hidden}>
      <div className="section-heading">
        <div><h2>Diagnostics</h2><p>See what is unhealthy now, then inspect privacy-preserving failure details.</p></div>
        <button className="button secondary" type="button" disabled={busy} onClick={() => void refresh()}><RefreshCw className={busy ? "spin" : undefined} size={15} /> Refresh</button>
      </div>
      {Boolean(failure) && <DiagnosticErrorNotice error={failure} fallback="Diagnostics could not complete the requested operation." />}
      {loadFailures.length > 0 && (
        <div className="diagnostics-partial-warning" role="status"><ShieldAlert size={16} /><span><strong>Some diagnostic details are unavailable.</strong><small>Could not load: {loadFailures.join(", ")}. Other available results are shown below.</small></span></div>
      )}

      <section className="diagnostics-current" aria-labelledby="diagnostics-current-title">
        <header className="panel-header compact"><div><h3 id="diagnostics-current-title">Current status</h3><p>Live checks only. Past failures are listed separately.</p></div></header>
        <div className="diagnostics-status-grid">
          <StatusCard label="Nebula Core" {...coreStatus} />
          <StatusCard label="Terminal runtime" {...runtimeStatus} />
          <StatusCard label="Diagnostic storage" {...loggerStatus} />
        </div>
      </section>

      <article className="panel diagnostics-errors-card">
        <header className="panel-header compact"><div><h3>Recent recorded failures</h3><p>Historical records do not mean the issue is still active.</p></div><label>Feature<select aria-label="Filter diagnostic errors by feature" value={feature} onChange={(event) => setFeature(event.target.value)}><option value="">All features</option>{diagnosticFeatures.map((item) => <option value={item} key={item}>{humanizeDiagnosticValue(item)}</option>)}</select></label></header>
        {targetReference && errorsLoaded && targetedRecord && <div className="diagnostic-target-notice" role="status">Showing requested failure <code>{targetReference}</code>.</div>}
        {targetReference && errorsLoaded && !targetedRecord && <div className="diagnostic-target-notice missing" role="status">The referenced failure <code>{targetReference}</code> is no longer in recent diagnostics.</div>}
        {errors.length ? <div className="diagnostics-error-list">{errors.slice().reverse().map((record, index) => <FailureCard record={record} targeted={record === targetedRecord} key={`${record.error_id ?? record.timestamp}-${index}`} />)}</div> : <div className="empty-state compact"><CheckCircle2 size={22} /><strong>No matching recorded failures</strong><p>No retained error record matches this filter. Check Current status for live health.</p></div>}
      </article>

      <details className="diagnostics-advanced">
        <summary>Advanced diagnostics and logging</summary>
        <p>Configure diagnostic detail, inspect logger storage, or prepare a sanitized support bundle.</p>
        <div className="diagnostics-grid">
          <article className="panel diagnostics-settings-card">
            <header className="panel-header compact"><div><h3>Log levels</h3><p>Changes apply live and persist across restarts.</p></div></header>
            <label>Global level
              <select value={draft?.global_level ?? "error"} disabled={!draft || busy} onChange={(event) => setDraft((current) => current && ({ ...current, global_level: event.target.value as DiagnosticLevel }))}>
                {levels.map((level) => <option value={level.value} key={level.value}>{level.label}</option>)}
              </select>
            </label>
            <p className="diagnostics-level-help">{levels.find((level) => level.value === (draft?.global_level ?? "error"))?.description}</p>
            <details className="diagnostics-overrides">
              <summary>Per-feature overrides ({Object.keys(draft?.feature_levels ?? {}).length})</summary>
              <div>{diagnosticFeatures.map((item) => <label key={item}><span>{item}</span><select value={draft?.feature_levels?.[item] ?? "inherit"} disabled={!draft || busy} onChange={(event) => setDraft((current) => {
                if (!current) return current;
                const featureLevels = { ...(current.feature_levels ?? {}) };
                if (event.target.value === "inherit") delete featureLevels[item];
                else featureLevels[item] = event.target.value as DiagnosticLevel;
                return { ...current, feature_levels: featureLevels };
              })}><option value="inherit">Use global</option>{levels.map((level) => <option value={level.value} key={level.value}>{level.label}</option>)}</select></label>)}</div>
            </details>
            <button className="button primary" type="button" disabled={!changed || busy} onClick={() => void save()}>Save logging levels</button>
          </article>
          <article className="panel diagnostics-health-card">
            <header className="panel-header compact"><div><h3>Logger storage</h3><p>{status?.writable ? "Writable" : "Unavailable"}</p></div><span className={`status-dot ${status?.writable ? "healthy" : "unavailable"}`} /></header>
            <dl><div><dt>Disk usage</dt><dd>{formatBytes(status?.disk_usage_bytes ?? 0)}</dd></div><div><dt>Dropped lower-level records</dt><dd>{status?.dropped_record_count ?? 0}</dd></div><div><dt>Last rotation</dt><dd>{status?.last_rotation ? new Date(status.last_rotation).toLocaleString() : "Not yet"}</dd></div></dl>
            <div className="diagnostics-actions">
              {native && <button className="button secondary" type="button" onClick={() => void reveal()}><FolderOpen size={15} /> Open logs folder</button>}
              <button className="button secondary" type="button" disabled={!api || busy} onClick={() => void exportBundle()}><Download size={15} /> Export sanitized ZIP</button>
            </div>
          </article>
        </div>
        <article className="panel diagnostics-files-card">
          <header className="panel-header compact"><div><h3>Local files</h3><p>JSON-lines logs with two retained rotations.</p></div><FileText size={18} /></header>
          <div>{files.map((file) => <span key={file.name}><code>{file.name}</code><small>{formatBytes(file.size_bytes)} · {new Date(file.modified_at).toLocaleString()}</small></span>)}</div>
        </article>
      </details>
    </section>
  );
}
