import { invoke } from "@tauri-apps/api/core";
import type {
  DiagnosticFile,
  DiagnosticInput,
  DiagnosticLevel,
  DiagnosticRecord,
  DiagnosticSettings,
  DiagnosticStatus,
} from "./types";
import { diagnosticFeatures } from "./types";

const DEFAULT_SETTINGS: DiagnosticSettings = {
  schema: "nebula.diagnostics-settings/v1",
  global_level: "error",
  feature_levels: {},
};
const levelValues: Record<DiagnosticLevel, number> = {
  debug: 10,
  info: 20,
  warning: 30,
  error: 40,
  critical: 50,
};
const allowedMetadata = new Set([
  "action",
  "adapter",
  "attempt",
  "available",
  "backend",
  "batch_count",
  "byte_count",
  "capability",
  "category",
  "chunk_count",
  "code",
  "collection_count",
  "component",
  "connection_state",
  "count",
  "current_revision",
  "decision",
  "digest",
  "direction",
  "disk_bytes",
  "dropped_count",
  "entity_count",
  "entity_id",
  "entity_type",
  "expected_revision",
  "feature",
  "fingerprint",
  "format",
  "health",
  "http_status",
  "image_digest",
  "installed",
  "item_count",
  "kind",
  "limit",
  "method",
  "mode",
  "model_id",
  "operation",
  "origin",
  "policy",
  "port_class",
  "provider",
  "queue_depth",
  "reason_code",
  "record_count",
  "recovered_count",
  "result",
  "retry_count",
  "revision",
  "route",
  "runner",
  "sequence_end",
  "sequence_start",
  "size_class",
  "state",
  "status",
  "step",
  "target_fingerprint",
  "task_count",
  "timeout_seconds",
  "tool_id",
  "transport",
  "truncated",
  "validation",
  "vendor_request_id",
  "version",
  "warning_count",
]);
const deniedMetadata = /secret|credential|authorization|cookie|header|body|prompt|content|source|command|argv|stdout|stderr|document|terminal_(?:bytes|output)|evidence_bytes|private_key|password|passwd|api_key|access_token|refresh_token|filename|file_path|path|query|sql|payload|selected_text/i;
const maxFallbackRecords = 250;
const maxErrorPresentations = 250;

interface DiagnosticErrorPresentation {
  retryable?: boolean;
  code?: string;
}

interface BrowserSink {
  baseUrl: string;
  token?: string;
}

let settings = DEFAULT_SETTINGS;
let browserSink: BrowserSink | undefined;
let fallback: DiagnosticRecord[] = [];
let flushing = false;
let globalHandlersInstalled = false;
let diagnosticsAvailable = true;
let lastDropNotice = 0;
const errorPresentations = new Map<string, DiagnosticErrorPresentation>();

function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function identifier(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

export function newOperationId(): string {
  return identifier("op");
}

export function rememberDiagnosticErrorPresentation(
  reference: string | undefined,
  presentation: DiagnosticErrorPresentation,
): void {
  if (!reference || !/^(?:err|req)_[A-Za-z0-9._:-]{1,160}$/.test(reference)) return;
  errorPresentations.delete(reference);
  errorPresentations.set(reference, {
    retryable: presentation.retryable,
    code: presentation.code && /^[a-z][a-z0-9._-]{1,159}$/.test(presentation.code)
      ? presentation.code
      : undefined,
  });
  while (errorPresentations.size > maxErrorPresentations) {
    const oldest = errorPresentations.keys().next().value;
    if (typeof oldest !== "string") break;
    errorPresentations.delete(oldest);
  }
}

export function diagnosticErrorPresentation(
  reference: string | undefined,
): DiagnosticErrorPresentation | undefined {
  return reference ? errorPresentations.get(reference) : undefined;
}

function safeText(value: string, limit = 2_048): string {
  let result = value.replaceAll("\0", "�");
  if (/-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/.test(result)) return "[REDACTED PRIVATE KEY]";
  result = result
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]{12,}/gi, "Bearer [REDACTED]")
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, "[REDACTED JWT]")
    .replace(/\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b/g, "[REDACTED TOKEN]")
    .replace(/(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\s*[:=]\s*[^\s,;]{8,}/gi, "$1=[REDACTED]");
  return result.length <= limit ? result : `${result.slice(0, limit - 1)}…`;
}

function sanitizeValue(value: unknown, depth: number): unknown {
  if (depth > 5) return "[MAX_DEPTH]";
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
  if (typeof value === "string") return safeText(value);
  if (Array.isArray(value)) return value.slice(0, 64).map((item) => sanitizeValue(item, depth + 1));
  if (value && typeof value === "object") return sanitizeMetadata(value as Record<string, unknown>, depth + 1);
  return `[${typeof value}]`;
}

function sanitizeMetadata(value?: Record<string, unknown>, depth = 0): Record<string, unknown> {
  if (!value || depth > 5) return {};
  return Object.fromEntries(
    Object.entries(value)
      .slice(0, 64)
      .map(([key, item]) => [key.toLowerCase().replaceAll("-", "_"), item] as const)
      .filter(([key, item]) => item !== undefined && allowedMetadata.has(key) && !deniedMetadata.test(key))
      .map(([key, item]) => [key, sanitizeValue(item, depth + 1)]),
  );
}

function exceptionType(value: unknown): string | undefined {
  if (value instanceof Error) return safeText(value.name || "Error", 128);
  if (value === undefined || value === null) return undefined;
  return "NonErrorRejection";
}

function caughtFailureCause(value: unknown, status: number | undefined, cancelled: boolean): string {
  if (cancelled) return "The operation ended through expected cancellation.";
  if (status !== undefined && status < 500) return "The request was rejected safely.";
  if (value instanceof Error && value.name === "TimeoutError") {
    return "The operation exceeded its bounded time limit.";
  }
  if (value instanceof Error) {
    return `The interface operation raised ${safeText(value.name || "Error", 128)}.`;
  }
  return "The interface operation failed with a non-Error rejection.";
}

function stackFrames(value: unknown): DiagnosticRecord["stack_frames"] {
  if (!(value instanceof Error) || !value.stack) return undefined;
  const frames = value.stack.split("\n").slice(1, 33).flatMap((raw) => {
    const lineMatch = raw.match(/:(\d+)(?::\d+)?\)?$/);
    if (!lineMatch) return [];
    const rawFunction = raw
      .trim()
      .replace(/^at\s+/, "")
      .split(/\s+\(|@/u, 1)[0]
      ?.trim();
    const functionName = rawFunction
      && !rawFunction.includes("/")
      && !rawFunction.includes("\\")
      && /^[A-Za-z0-9_$<>.:[\] -]+$/u.test(rawFunction)
      ? safeText(rawFunction, 128)
      : "anonymous";
    return [{ module: "interface", function: functionName, line: Number(lineMatch[1]) }];
  });
  return frames.length ? frames : undefined;
}

function enabled(level: DiagnosticLevel): boolean {
  const threshold = settings.feature_levels?.interface ?? settings.global_level ?? "error";
  return levelValues[level] >= levelValues[threshold];
}

export function normalizeDiagnosticSettings(value: unknown): DiagnosticSettings {
  if (!value || typeof value !== "object") return { ...DEFAULT_SETTINGS, feature_levels: {} };
  const candidate = value as {
    global_level?: unknown;
    feature_levels?: unknown;
  };
  const globalLevel = typeof candidate.global_level === "string"
    && candidate.global_level in levelValues
    ? candidate.global_level as DiagnosticLevel
    : "error";
  const featureLevels: DiagnosticSettings["feature_levels"] = {};
  if (candidate.feature_levels && typeof candidate.feature_levels === "object") {
    for (const feature of diagnosticFeatures) {
      const level = (candidate.feature_levels as Record<string, unknown>)[feature];
      if (typeof level === "string" && level in levelValues) {
        featureLevels[feature] = level as DiagnosticLevel;
      }
    }
  }
  return {
    schema: "nebula.diagnostics-settings/v1",
    global_level: globalLevel,
    feature_levels: featureLevels,
  };
}

function setAvailability(available: boolean, reason?: string): void {
  diagnosticsAvailable = available;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: { available, reason: reason ? safeText(reason, 256) : undefined },
    }));
  }
}

function remember(record: DiagnosticRecord): void {
  const error = ["error", "critical", "ERROR", "CRITICAL"].includes(record.level);
  if (!error && fallback.length >= maxFallbackRecords) {
    const lowerIndex = fallback.findIndex((item) => !["error", "critical", "ERROR", "CRITICAL"].includes(item.level));
    if (lowerIndex >= 0) fallback.splice(lowerIndex, 1);
    else {
      const now = Date.now();
      if (now - lastDropNotice >= 60_000) {
        lastDropNotice = now;
        fallback.push({
          schema: "nebula.diagnostic/v1",
          level: "error",
          feature: "interface",
          event_code: "interface.diagnostics.records_dropped",
          message: "Lower-level interface diagnostics were dropped while the local sink was unavailable.",
          error_id: identifier("err"),
          outcome: "degraded",
          stage: "fallback-queue",
          retryable: true,
          safe_failure_cause: "The bounded interface diagnostics fallback reached capacity.",
        });
      }
      setAvailability(false, "The interface diagnostics fallback is full.");
      return;
    }
  }
  fallback.push(record);
  setAvailability(false, "The local diagnostics sink is temporarily unavailable.");
}

function wireRecord(input: DiagnosticInput): DiagnosticRecord {
  const isError = input.level === "error" || input.level === "critical";
  return {
    schema: "nebula.diagnostic/v1",
    level: input.level,
    feature: "interface",
    event_code: input.eventCode,
    message: safeText(input.message),
    request_id: input.requestId,
    operation_id: input.operationId,
    parent_operation_id: input.parentOperationId,
    error_id: input.errorId ?? (isError ? identifier("err") : undefined),
    project_id: input.projectId,
    run_id: input.runId,
    execution_id: input.executionId,
    session_id: input.sessionId,
    outcome: input.outcome ? safeText(input.outcome, 64) : undefined,
    stage: input.stage ? safeText(input.stage, 128) : undefined,
    duration_ms: Number.isFinite(input.durationMs)
      && Number(input.durationMs) >= 0
      && Number(input.durationMs) <= 86_400_000
      ? input.durationMs
      : undefined,
    retryable: input.retryable,
    safe_failure_cause: input.safeFailureCause ? safeText(input.safeFailureCause) : undefined,
    exception_type: exceptionType(input.exception),
    stack_frames: stackFrames(input.exception),
    metadata: sanitizeMetadata(input.metadata),
  };
}

async function sendNative(record: DiagnosticRecord): Promise<string | undefined> {
  return (await invoke<string | null>("diagnostics_log_frontend", { record })) ?? undefined;
}

async function sendBrowser(records: DiagnosticRecord[]): Promise<string[]> {
  if (!browserSink) throw new Error("browser diagnostics sink is not configured");
  const response = await fetch(`${browserSink.baseUrl.replace(/\/+$/, "")}/diagnostics/events`, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(browserSink.token ? { Authorization: `Bearer ${browserSink.token}` } : {}),
    },
    credentials: "same-origin",
    body: JSON.stringify({ events: records }),
  });
  if (!response.ok) throw new Error("browser diagnostics sink rejected a batch");
  const result = await response.json() as { error_ids?: string[] };
  return result.error_ids ?? [];
}

async function flushFallback(): Promise<void> {
  if (flushing || fallback.length === 0) return;
  flushing = true;
  const pending = fallback;
  fallback = [];
  let cursor = 0;
  let completed = false;
  try {
    if (isTauri()) {
      while (cursor < pending.length) {
        await sendNative(pending[cursor]);
        cursor += 1;
      }
    } else {
      while (cursor < pending.length) {
        const batch = pending.slice(cursor, cursor + 100);
        await sendBrowser(batch);
        cursor += batch.length;
      }
    }
    completed = true;
    setAvailability(fallback.length === 0);
  } catch {
    // diagnostic-expected: sink failure is retained in the no-error-drop fallback.
    const restored = [...pending.slice(cursor), ...fallback];
    const retainedErrors = restored.filter((record) => ["error", "critical", "ERROR", "CRITICAL"].includes(record.level));
    const retainedLower = restored
      .filter((record) => !["error", "critical", "ERROR", "CRITICAL"].includes(record.level))
      .slice(-maxFallbackRecords);
    fallback = [...retainedErrors, ...retainedLower];
    setAvailability(false, "Buffered diagnostics could not be flushed.");
  } finally {
    flushing = false;
    if (completed && fallback.length > 0) void flushFallback();
  }
}

export async function logDiagnostic(input: DiagnosticInput): Promise<string | undefined> {
  if (!enabled(input.level)) return undefined;
  const record = wireRecord(input);
  try {
    const errorId = isTauri()
      ? await sendNative(record)
      : (await sendBrowser([record]))[0];
    setAvailability(true);
    void flushFallback();
    return errorId;
  } catch {
    // diagnostic-expected: remember() preserves the record and marks health degraded.
    remember(record);
    return undefined;
  }
}

export function logCaughtDiagnostic(
  eventCode: string,
  message: string,
  error: unknown,
  stage = "handled-error",
): void {
  const classified = error && typeof error === "object"
    ? error as {
      name?: unknown;
      status?: unknown;
      errorId?: unknown;
      requestId?: unknown;
      retryable?: unknown;
      code?: unknown;
    }
    : undefined;
  const cancelled = classified?.name === "AbortError";
  const status = typeof classified?.status === "number" ? classified.status : undefined;
  const alreadyRecordedByCore = typeof classified?.errorId === "string";
  const retryable = typeof classified?.retryable === "boolean"
    ? classified.retryable
    : cancelled
      ? false
      : status !== undefined && [408, 409, 425, 429].includes(status)
        ? true
        : classified?.name === "TimeoutError"
          ? true
          : undefined;
  const level: DiagnosticLevel = cancelled || alreadyRecordedByCore
    ? "debug"
    : status !== undefined && status < 500
      ? "warning"
      : "error";
  const localErrorId = level === "error" ? identifier("err") : undefined;
  if (localErrorId && error instanceof Error && Object.isExtensible(error)) {
    Object.defineProperty(error, "errorId", {
      configurable: true,
      enumerable: false,
      value: localErrorId,
    });
    if (!/\bReference:\s*(?:err|req)_/i.test(error.message)) {
      error.message = `${error.message} Reference: ${localErrorId}.`;
    }
  }
  const reference = typeof classified?.errorId === "string"
    ? classified.errorId
    : localErrorId ?? (typeof classified?.requestId === "string" ? classified.requestId : undefined);
  rememberDiagnosticErrorPresentation(reference, {
    retryable,
    code: typeof classified?.code === "string" ? classified.code : undefined,
  });
  void logDiagnostic({
    level,
    eventCode,
    message: cancelled ? "An interface operation was cancelled." : message,
    outcome: cancelled ? "cancelled" : "failure",
    stage,
    retryable,
    safeFailureCause: caughtFailureCause(error, status, cancelled),
    exception: error,
    requestId: typeof classified?.requestId === "string" ? classified.requestId : undefined,
    errorId: typeof classified?.errorId === "string" ? classified.errorId : localErrorId,
    metadata: {
      kind: alreadyRecordedByCore ? "core-error-handled" : cancelled ? "expected-cancellation" : "interface-error",
      http_status: status,
    },
  });
}

export function configureBrowserDiagnostics(baseUrl: string, token?: string): void {
  browserSink = { baseUrl, token };
  void flushFallback();
}

export function setDiagnosticSettings(next: DiagnosticSettings): void {
  settings = normalizeDiagnosticSettings(next);
}

export function diagnosticsFallbackErrors(): DiagnosticRecord[] {
  return fallback.filter((record) => ["error", "critical", "ERROR", "CRITICAL"].includes(record.level));
}

export function isDiagnosticsAvailable(): boolean {
  return diagnosticsAvailable;
}

export function setDiagnosticsAvailability(available: boolean, reason?: string): void {
  setAvailability(available, reason);
}

export function installGlobalDiagnosticHandlers(): void {
  if (globalHandlersInstalled || typeof window === "undefined") return;
  globalHandlersInstalled = true;
  window.addEventListener("error", (event) => {
    void logDiagnostic({
      level: "error",
      eventCode: "interface.window.unhandled_error",
      message: "The interface encountered an unhandled runtime error.",
      outcome: "failure",
      stage: "window",
      retryable: false,
      exception: event.error instanceof Error ? event.error : undefined,
    });
  });
  window.addEventListener("unhandledrejection", (event) => {
    void logDiagnostic({
      level: "error",
      eventCode: "interface.promise.unhandled_rejection",
      message: "An interface operation failed without a rejection handler.",
      outcome: "failure",
      stage: "promise",
      retryable: false,
      exception: event.reason instanceof Error ? event.reason : undefined,
    });
  });
}

export async function nativeDiagnosticSettings(): Promise<DiagnosticSettings> {
  const value = normalizeDiagnosticSettings(await invoke<DiagnosticSettings>("diagnostics_get_settings"));
  setDiagnosticSettings(value);
  return value;
}

export async function updateNativeDiagnosticSettings(value: DiagnosticSettings): Promise<DiagnosticSettings> {
  const updated = normalizeDiagnosticSettings(
    await invoke<DiagnosticSettings>("diagnostics_update_settings", { settings: value }),
  );
  setDiagnosticSettings(updated);
  return updated;
}

export function nativeDiagnosticStatus(): Promise<DiagnosticStatus> {
  return invoke<DiagnosticStatus>("diagnostics_status");
}

export function nativeDiagnosticFiles(): Promise<DiagnosticFile[]> {
  return invoke<DiagnosticFile[]>("diagnostics_files");
}

export function nativeRecentErrors(feature?: string, after?: string, limit = 100): Promise<DiagnosticRecord[]> {
  return invoke<DiagnosticRecord[]>("diagnostics_recent_errors", { feature, after, limit });
}

export function revealNativeLogs(): Promise<void> {
  return invoke<void>("diagnostics_reveal_logs");
}
