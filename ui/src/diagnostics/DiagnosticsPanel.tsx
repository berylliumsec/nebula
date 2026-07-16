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
  nativeSensitiveDiagnosticDetail,
  normalizeDiagnosticSettings,
  revealNativeLogs,
  setDiagnosticSettings,
  updateNativeDiagnosticSettings,
} from "./logger";
import {
  diagnosticIncidentMatchesReference,
  diagnosticFailurePresentation,
  diagnosticTechnicalDetails,
  humanizeDiagnosticValue,
  resolveDiagnosticIncidents,
} from "./presentation";
import {
  diagnosticFeatures,
  type DiagnosticFile,
  type DiagnosticAction,
  type DiagnosticIncident,
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
    const key = [
      record.source ?? "unknown",
      record.launch_id ?? "session",
      record.sequence ?? 0,
      record.event_code,
      record.error_id ?? record.request_id ?? record.timestamp ?? "unreferenced",
    ].join(":");
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

function FailureCard({
  incident,
  targeted,
  onAction,
  onSensitiveDetail,
}: {
  incident: DiagnosticIncident;
  targeted: boolean;
  onAction: (incident: DiagnosticIncident, action: DiagnosticAction) => Promise<void>;
  onSensitiveDetail: (incident: DiagnosticIncident, action: "reveal" | "copy") => Promise<string | undefined>;
}) {
  const record = incident.primary;
  const presentation = diagnosticFailurePresentation(record);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [sensitiveDetail, setSensitiveDetail] = useState<string>();
  const [sensitiveBusy, setSensitiveBusy] = useState(false);
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

  const accessSensitiveDetail = async (action: "reveal" | "copy") => {
    setSensitiveBusy(true);
    try {
      const detail = await onSensitiveDetail(incident, action);
      if (!detail) return;
      if (action === "copy") {
        await navigator.clipboard.writeText(detail);
        setCopyState("copied");
      } else {
        setSensitiveDetail(detail);
      }
    } catch (error) {
      setCopyState("failed");
      void logCaughtDiagnostic(
        "interface.diagnostics.sensitive_detail_failed",
        "Protected diagnostic detail could not be accessed.",
        error,
        "sensitive-detail",
      );
    } finally {
      setSensitiveBusy(false);
    }
  };

  return (
    <article className={`diagnostic-failure-card${targeted ? " targeted" : ""}`} tabIndex={targeted ? -1 : undefined}>
      <header>
        <span className={`diagnostic-level ${String(record.level).toLowerCase()}`}>{record.level}</span>
        <div>
          <small>{presentation.operationLabel}</small>
          <h4>{incident.guidance.title}</h4>
          <p>{record.message}</p>
        </div>
        <time dateTime={record.timestamp}>{record.timestamp ? new Date(record.timestamp).toLocaleString() : "This session"}</time>
      </header>
      <div className="diagnostic-failure-explanation">
        <span><small>Exact sanitized cause</small><p>{incident.guidance.cause}</p></span>
        <span><small>Operational impact</small><p>{incident.guidance.impact}</p></span>
        <span><small>Confirmed safe state</small><p>{incident.guidance.confirmed_safe_state}</p></span>
      </div>
      <div className="diagnostic-remediation">
        <small>How to fix</small>
        <ol>{incident.guidance.steps.map((step) => <li key={step}>{step}</li>)}</ol>
        <small>How to verify recovery</small>
        <p>{incident.guidance.verification}</p>
      </div>
      <div className="diagnostic-failure-actions">
        {incident.actions.map((action) => action.kind === "navigate" && action.destination ? (
          <a className="button secondary" href={action.destination} key={action.id}>{action.label} <ExternalLink size={13} /></a>
        ) : (
          <button
            className="button secondary"
            type="button"
            key={action.id}
            disabled={!action.enabled}
            title={!action.enabled ? action.disabled_reason ?? undefined : undefined}
            onClick={() => void onAction(incident, action)}
          >{action.label}</button>
        ))}
      </div>
      {incident.actions.some((action) => !action.enabled && action.disabled_reason) && (
        <ul className="diagnostic-disabled-reasons">
          {incident.actions.filter((action) => !action.enabled && action.disabled_reason).map((action) => (
            <li key={action.id}><strong>{action.label}:</strong> {action.disabled_reason}</li>
          ))}
        </ul>
      )}
      {incident.sensitive_detail_available ? (
        <div className="diagnostic-sensitive-detail">
          <small>Protected detail expires {incident.sensitive_detail_expires_at ? new Date(incident.sensitive_detail_expires_at).toLocaleString() : "within 24 hours"}. Confirmation is required every time it is revealed or copied.</small>
          <span>
            <button className="button quiet" type="button" disabled={sensitiveBusy} onClick={() => void accessSensitiveDetail("reveal")}>Reveal sensitive detail</button>
            <button className="button quiet" type="button" disabled={sensitiveBusy} onClick={() => void accessSensitiveDetail("copy")}>Copy sensitive detail</button>
          </span>
          {sensitiveDetail && <pre aria-label="Sensitive diagnostic detail">{sensitiveDetail}</pre>}
        </div>
      ) : (
        <p className="diagnostic-sensitive-unavailable">Protected source detail was not captured for this incident. Historical incidents cannot recover it.</p>
      )}
      <details className="diagnostic-technical-details" open={targeted || undefined}>
        <summary>Technical details</summary>
        <dl>
          <div><dt>Technical code</dt><dd>{record.event_code}</dd></div>
          <div><dt>Reason family</dt><dd>{record.reason_code ?? "unavailable for this historical record"}</dd></div>
          <div><dt>Remediation</dt><dd>{incident.guidance.remediation_id}</dd></div>
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
          {incident.related_records.length > 0 && <div><dt>Correlated records</dt><dd>{incident.related_records.length + 1}</dd></div>}
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
  const [incidents, setIncidents] = useState<DiagnosticIncident[]>([]);
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
        let nextErrors = mergeErrors(recent, diagnosticsFallbackErrors());
        setErrors(nextErrors);
        setErrorsLoaded(true);

        if (api && workspaceState !== "failed") {
          const [coreFilesResult, coreErrorsResult, healthResult, setupResult] = await Promise.allSettled([
            api.diagnosticsFiles(),
            api.diagnosticErrors(feature || undefined),
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
          if (coreErrorsResult.status === "fulfilled") {
            nextErrors = mergeErrors(nextErrors, coreErrorsResult.value);
            setErrors(nextErrors);
          } else noteFailure("Core recent failures", coreErrorsResult.reason);
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

  useEffect(() => {
    let active = true;
    const local = resolveDiagnosticIncidents(errors);
    setIncidents(local);
    const resolver = api && "resolveDiagnosticIncidents" in api
      ? api.resolveDiagnosticIncidents(errors)
      : undefined;
    void resolver?.then((resolved) => {
      if (active) setIncidents(resolved);
    }).catch((error: unknown) => {
      void logCaughtDiagnostic(
        "interface.diagnostics.incident_resolution_failed",
        "Core incident enrichment was unavailable; bundled offline guidance is shown.",
        error,
        "incident-resolution",
      );
    });
    return () => { active = false; };
  }, [api, errors]);

  const displayedIncidents = useMemo(
    () => incidents.length ? incidents : resolveDiagnosticIncidents(errors),
    [errors, incidents],
  );

  const targetedIncident = useMemo(
    () => targetReference ? displayedIncidents.find((incident) => diagnosticIncidentMatchesReference(incident, targetReference)) : undefined,
    [displayedIncidents, targetReference],
  );

  useEffect(() => {
    if (!targetedIncident) return;
    window.requestAnimationFrame(() => {
      const element = document.querySelector<HTMLElement>(".diagnostic-failure-card.targeted");
      element?.scrollIntoView?.({ block: "center" });
      element?.focus();
    });
  }, [targetedIncident]);

  const runIncidentAction = async (incident: DiagnosticIncident, action: DiagnosticAction) => {
    if (!api) {
      setFailure(new Error(action.disabled_reason ?? "Nebula Core must be connected to run this action."));
      return;
    }
    if (action.confirmation_required) {
      const approved = await confirm({
        title: `${action.label}?`,
        message: action.kind === "retry"
          ? "This creates a linked replacement operation and does not mutate the failed history."
          : "This runs a bounded, allowlisted health check and does not change configuration.",
        confirmLabel: action.label,
      });
      if (!approved) return;
    }
    setBusy(true);
    setFailure(undefined);
    try {
      await api.runDiagnosticAction(incident.error_id, action.id, action.confirmation_required);
      await refresh();
    } catch (error) {
      setFailure(error);
      void logCaughtDiagnostic(
        "interface.diagnostics.incident_action_failed",
        "An allowlisted incident action failed.",
        error,
        "incident-action",
      );
    } finally {
      setBusy(false);
    }
  };

  const accessSensitiveDetail = async (
    incident: DiagnosticIncident,
    action: "reveal" | "copy",
  ): Promise<string | undefined> => {
    const approved = await confirm({
      title: `${action === "copy" ? "Copy" : "Reveal"} sensitive detail?`,
      message: "This detail may contain local technical identifiers. Access is audited, it is never included in support exports, and confirmation is required for every reveal or copy.",
      confirmLabel: action === "copy" ? "Copy detail" : "Reveal detail",
    });
    if (!approved) return undefined;
    const nativeOwned = native && incident.primary.source === "desktop";
    if (nativeOwned) {
      const response = await nativeSensitiveDiagnosticDetail(incident.error_id, action);
      return response.detail;
    }
    if (!api) {
      setFailure(new Error("Nebula Core must be connected to access protected diagnostic detail."));
      return undefined;
    }
    const response = await api.diagnosticSensitiveDetail(incident.error_id, action);
    return response.detail;
  };

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
      setFailure(new Error("Nebula Core must be connected to create a diagnostics support bundle."));
      return;
    }
    const approved = await confirm({
      title: "Export local diagnostics?",
      message: "The ZIP contains unredacted logs, build/platform metadata, logger health, settings, and a SHA-256 manifest. It excludes project databases, workspaces, evidence, terminal results, and provider configuration. Treat the exported logs as sensitive data.",
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
        message: "A diagnostics export was downloaded.",
        outcome: "success",
        stage: "export",
      });
    } catch (error) {
      setFailure(error);
      void logDiagnostic({
        level: "error",
        eventCode: "interface.diagnostics.export_failed",
        message: "A diagnostics export could not be downloaded.",
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
        {targetReference && errorsLoaded && targetedIncident && <div className="diagnostic-target-notice" role="status">Showing requested failure <code>{targetReference}</code>.</div>}
        {targetReference && errorsLoaded && !targetedIncident && <div className="diagnostic-target-notice missing" role="status">The referenced failure <code>{targetReference}</code> is no longer in recent diagnostics.</div>}
        {displayedIncidents.length ? <div className="diagnostics-error-list">{displayedIncidents.map((incident) => <FailureCard incident={incident} targeted={incident === targetedIncident} onAction={runIncidentAction} onSensitiveDetail={accessSensitiveDetail} key={incident.error_id} />)}</div> : <div className="empty-state compact"><CheckCircle2 size={22} /><strong>No matching recorded failures</strong><p>No retained error record matches this filter. Check Current status for live health.</p></div>}
      </article>

      <details className="diagnostics-advanced">
        <summary>Advanced diagnostics and logging</summary>
        <p>Configure diagnostic detail, inspect logger storage, or prepare a diagnostics support bundle.</p>
        <div className="diagnostics-grid">
          <article className="panel diagnostics-settings-card">
            <header className="panel-header compact"><div><h3>Log levels</h3><p>Changes apply live and persist across restarts.</p></div></header>
            <label>Global level
              <select value={draft?.global_level ?? "error"} disabled={!draft || busy} onChange={(event) => setDraft((current) => current && ({ ...current, global_level: event.target.value as DiagnosticLevel }))}>
                {levels.map((level) => <option value={level.value} key={level.value}>{level.label}</option>)}
              </select>
            </label>
            <p className="diagnostics-level-help">{levels.find((level) => level.value === (draft?.global_level ?? "error"))?.description}</p>
            <label className="diagnostics-sensitive-setting">
              <input
                type="checkbox"
                checked={draft?.sensitive_detail_capture === true}
                disabled={!draft || busy}
                onChange={(event) => setDraft((current) => current && ({ ...current, sensitive_detail_capture: event.target.checked }))}
              />
              <span><strong>Capture encrypted sensitive error detail for 24 hours</strong><small>Off by default. Uses the OS credential vault when available, falls back to session memory, and never enters support exports.</small></span>
            </label>
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
              <button className="button secondary" type="button" disabled={!api || busy} onClick={() => void exportBundle()}><Download size={15} /> Export diagnostics ZIP</button>
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
