export const diagnosticFeatures = [
  "desktop",
  "interface",
  "api",
  "setup",
  "storage",
  "projects",
  "terminal",
  "terminal-audit",
  "workspace",
  "notes",
  "capture",
  "providers",
  "chat",
  "knowledge",
  "harnesses",
  "missions",
  "runtime",
  "sandbox",
  "executions",
  "findings",
  "evidence",
  "reports",
  "diagnostics",
] as const;

export type DiagnosticFeature = (typeof diagnosticFeatures)[number];
export type DiagnosticLevel = "debug" | "info" | "warning" | "error" | "critical";
export type DiagnosticReasonCode =
  | "transport_closed"
  | "protocol_invalid"
  | "dependency_unavailable"
  | "authentication_failed"
  | "timeout"
  | "rate_limited"
  | "permission_denied"
  | "storage_write_failed"
  | "integrity_failed"
  | "stale_state"
  | "invalid_input"
  | "cancelled"
  | "unknown_internal_fault";

export interface DiagnosticSettings {
  schema: "nebula.diagnostics-settings/v1";
  global_level: DiagnosticLevel;
  feature_levels: Partial<Record<DiagnosticFeature, DiagnosticLevel>>;
  sensitive_detail_capture: boolean;
}

export interface DiagnosticRecord {
  schema: "nebula.diagnostic/v1";
  timestamp?: string;
  sequence?: number;
  level: Uppercase<DiagnosticLevel> | DiagnosticLevel;
  feature: DiagnosticFeature;
  source?: string;
  event_code: string;
  message: string;
  application_version?: string;
  launch_id?: string;
  request_id?: string;
  operation_id?: string;
  parent_operation_id?: string;
  error_id?: string;
  project_id?: string;
  run_id?: string;
  execution_id?: string;
  session_id?: string;
  outcome?: string;
  stage?: string;
  duration_ms?: number;
  retryable?: boolean;
  safe_failure_cause?: string;
  reason_code?: DiagnosticReasonCode | string;
  operator_detail?: string;
  impact?: string;
  remediation_id?: string;
  sensitive_detail_available?: boolean;
  sensitive_detail_expires_at?: string;
  exception_type?: string;
  exception_chain?: string[];
  stack_frames?: Array<{ module: string; function: string; line: number }>;
  metadata?: Record<string, unknown>;
}

export interface DiagnosticFile {
  name: string;
  size_bytes: number;
  modified_at: string;
}

export interface DiagnosticStatus {
  schema: "nebula.diagnostics-status/v1";
  writable: boolean;
  degraded: boolean;
  global_level: DiagnosticLevel;
  feature_levels: Partial<Record<DiagnosticFeature, DiagnosticLevel>>;
  process_override?: DiagnosticLevel | null;
  disk_usage_bytes: number;
  last_rotation?: string | null;
  dropped_record_count: number;
  queued_record_count?: number;
  last_failure?: { timestamp?: string; message: string; exception_type?: string } | string | null;
  sensitive_detail_capture?: boolean;
  sensitive_detail_persistence?: "disabled" | "encrypted-vault" | "session-memory" | string;
}

export interface DiagnosticContext {
  requestId?: string;
  operationId?: string;
  parentOperationId?: string;
  errorId?: string;
  projectId?: string;
  runId?: string;
  executionId?: string;
  sessionId?: string;
}

export interface DiagnosticInput extends DiagnosticContext {
  level: DiagnosticLevel;
  eventCode: string;
  message: string;
  outcome?: string;
  stage?: string;
  durationMs?: number;
  retryable?: boolean;
  safeFailureCause?: string;
  reasonCode?: DiagnosticReasonCode | string;
  operatorDetail?: string;
  impact?: string;
  remediationId?: string;
  exception?: unknown;
  metadata?: Record<string, unknown>;
}

export interface DiagnosticGuidance {
  remediation_id: string;
  title: string;
  affected_operation: string;
  cause: string;
  impact: string;
  confirmed_safe_state: string;
  steps: string[];
  verification: string;
  help_article?: string | null;
}

export interface DiagnosticAction {
  id: string;
  label: string;
  kind: "navigate" | "health_check" | "retry";
  confirmation_required: boolean;
  enabled: boolean;
  disabled_reason?: string | null;
  destination?: string | null;
}

export interface DiagnosticIncident {
  schema: "nebula.diagnostic-incident/v1";
  error_id: string;
  status: "active" | "historical" | "resolved";
  primary: DiagnosticRecord;
  related_records: DiagnosticRecord[];
  guidance: DiagnosticGuidance;
  actions: DiagnosticAction[];
  facts: Record<string, string | number | boolean>;
  sensitive_detail_available: boolean;
  sensitive_detail_expires_at?: string | null;
}

export interface DiagnosticActionResult {
  error_id: string;
  action_id: string;
  result: Record<string, unknown>;
}
