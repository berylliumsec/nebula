import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, FileText, FolderOpen, RefreshCw, ShieldAlert } from "lucide-react";
import { useConfirmation } from "../components/DialogSystem";
import { useWorkspace } from "../state/WorkspaceContext";
import {
  diagnosticsFallbackErrors,
  isDiagnosticsAvailable,
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
  const { api, workspaceState } = useWorkspace();
  const confirm = useConfirmation();
  const [settings, setSettings] = useState<DiagnosticSettings>();
  const [draft, setDraft] = useState<DiagnosticSettings>();
  const [status, setStatus] = useState<DiagnosticStatus>();
  const [files, setFiles] = useState<DiagnosticFile[]>([]);
  const [errors, setErrors] = useState<DiagnosticRecord[]>([]);
  const [feature, setFeature] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<unknown>();
  const native = isNative();

  const refresh = useCallback(async () => {
    setBusy(true);
    setFailure(undefined);
    try {
      if (native) {
        const [nextSettings, nextStatus, nativeFiles, recent] = await Promise.all([
          nativeDiagnosticSettings(),
          nativeDiagnosticStatus(),
          nativeDiagnosticFiles(),
          nativeRecentErrors(feature || undefined, undefined, 100),
        ]);
        const safeNativeFiles = Array.isArray(nativeFiles) ? nativeFiles : [];
        const safeRecent = Array.isArray(recent) ? recent : [];
        let allFiles = safeNativeFiles;
        let combinedStatus = nextStatus;
        if (api && workspaceState !== "failed") {
          try {
            const core = await api.diagnosticsFiles();
            const coreFiles = Array.isArray(core?.files) ? core.files : [];
            const byName = new Map([...safeNativeFiles, ...coreFiles].map((file) => [file.name, file]));
            allFiles = [...byName.values()].sort((left, right) => left.name.localeCompare(right.name));
            if (core?.health) {
              combinedStatus = {
                ...nextStatus,
                writable: nextStatus.writable && core.health.writable,
                degraded: nextStatus.degraded || core.health.degraded,
                disk_usage_bytes: Math.max(nextStatus.disk_usage_bytes, core.health.disk_usage_bytes),
                dropped_record_count:
                  nextStatus.dropped_record_count + core.health.dropped_record_count,
                queued_record_count: core.health.queued_record_count,
                last_rotation: [nextStatus.last_rotation, core.health.last_rotation]
                  .filter((value): value is string => Boolean(value))
                  .sort()
                  .at(-1),
                last_failure: core.health.degraded
                  ? core.health.last_failure
                  : nextStatus.last_failure,
              };
            }
          } catch (error) {
            void logDiagnostic({
              level: "warning",
              eventCode: "interface.diagnostics.core_files_unavailable",
              message: "Core diagnostic file details were unavailable; native details remain visible.",
              outcome: "fallback",
              stage: "viewer-read",
              retryable: true,
              exception: error,
            });
          }
        }
        const normalizedSettings = normalizeDiagnosticSettings(nextSettings);
        setSettings(normalizedSettings);
        setDraft(normalizedSettings);
        setStatus(combinedStatus);
        setFiles(allFiles);
        setErrors([...safeRecent, ...diagnosticsFallbackErrors()]);
      } else {
        if (!api) throw new Error("Nebula Core is not connected.");
        const [nextSettings, fileResult, recent] = await Promise.all([
          api.diagnosticsSettings(),
          api.diagnosticsFiles(),
          api.diagnosticErrors(feature || undefined),
        ]);
        const normalizedSettings = normalizeDiagnosticSettings(nextSettings);
        setDiagnosticSettings(normalizedSettings);
        setSettings(normalizedSettings);
        setDraft(normalizedSettings);
        setStatus(fileResult?.health);
        setFiles(Array.isArray(fileResult?.files) ? fileResult.files : []);
        setErrors([
          ...(Array.isArray(recent) ? recent : []),
          ...diagnosticsFallbackErrors(),
        ]);
      }
    } catch (error) {
      setFailure(error);
      void logDiagnostic({
        level: "error",
        eventCode: "interface.diagnostics.viewer_load_failed",
        message: "The Diagnostics viewer could not load local diagnostic state.",
        outcome: "failure",
        stage: "viewer-read",
        retryable: true,
        exception: error,
      });
    } finally {
      setBusy(false);
    }
  }, [api, feature, native, workspaceState]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

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

  return (
    <section className="settings-section diagnostics-panel" id="diagnostics-settings" hidden={hidden}>
      <div className="section-heading">
        <div><h2>Diagnostics</h2><p>Privacy-preserving local logs. Errors are enabled by default; higher detail never includes project payloads.</p></div>
        <button className="button secondary" type="button" disabled={busy} onClick={() => void refresh()}><RefreshCw className={busy ? "spin" : undefined} size={15} /> Refresh</button>
      </div>
      {Boolean(failure) && <DiagnosticErrorNotice error={failure} fallback="Diagnostics could not complete the requested operation." />}
      {status?.degraded && <div className="diagnostics-health degraded" role="alert"><ShieldAlert size={16} /><span><strong>Diagnostics are degraded</strong><small>{typeof status.last_failure === "string" ? status.last_failure : status.last_failure?.message ?? "The logger reported a local storage failure."}</small></span></div>}
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
          <header className="panel-header compact"><div><h3>Logger health</h3><p>{status?.writable ? "Writable" : "Unavailable"}</p></div><span className={`status-dot ${status?.writable ? "healthy" : "unavailable"}`} /></header>
          <dl><div><dt>Disk usage</dt><dd>{formatBytes(status?.disk_usage_bytes ?? 0)}</dd></div><div><dt>Dropped lower-level records</dt><dd>{status?.dropped_record_count ?? 0}</dd></div><div><dt>Last rotation</dt><dd>{status?.last_rotation ? new Date(status.last_rotation).toLocaleString() : "Not yet"}</dd></div></dl>
          <div className="diagnostics-actions">
            {native && <button className="button secondary" type="button" onClick={() => void reveal()}><FolderOpen size={15} /> Open logs folder</button>}
            <button className="button secondary" type="button" disabled={!api || busy} onClick={() => void exportBundle()}><Download size={15} /> Export sanitized ZIP</button>
          </div>
        </article>
      </div>
      <article className="panel diagnostics-errors-card">
        <header className="panel-header compact"><div><h3>Recent errors</h3><p>Safe reasons and correlation references from the aggregate error log.</p></div><label>Feature<select aria-label="Filter diagnostic errors by feature" value={feature} onChange={(event) => setFeature(event.target.value)}><option value="">All features</option>{diagnosticFeatures.map((item) => <option value={item} key={item}>{item}</option>)}</select></label></header>
        {errors.length ? <div className="diagnostics-error-list">{errors.slice().reverse().map((record, index) => <details key={`${record.error_id ?? record.timestamp}-${index}`}><summary><span className={`diagnostic-level ${String(record.level).toLowerCase()}`}>{record.level}</span><strong>{record.message}</strong><time>{record.timestamp ? new Date(record.timestamp).toLocaleString() : "This session"}</time></summary><dl><div><dt>Feature</dt><dd>{record.feature}</dd></div><div><dt>Stage</dt><dd>{record.stage ?? "unspecified"}</dd></div><div><dt>Error reference</dt><dd>{record.error_id ?? "unavailable"}</dd></div><div><dt>Request reference</dt><dd>{record.request_id ?? "not an API request"}</dd></div><div><dt>Retryable</dt><dd>{record.retryable === true ? "Yes" : record.retryable === false ? "No" : "Not classified"}</dd></div><div><dt>Failure type</dt><dd>{record.exception_type ?? record.event_code}</dd></div></dl></details>)}</div> : <div className="empty-state compact"><ShieldAlert size={22} /><strong>No matching errors</strong><p>No local error record matches this filter.</p></div>}
      </article>
      <article className="panel diagnostics-files-card">
        <header className="panel-header compact"><div><h3>Local files</h3><p>JSON-lines logs with two retained rotations.</p></div><FileText size={18} /></header>
        <div>{files.map((file) => <span key={file.name}><code>{file.name}</code><small>{formatBytes(file.size_bytes)} · {new Date(file.modified_at).toLocaleString()}</small></span>)}</div>
      </article>
    </section>
  );
}
