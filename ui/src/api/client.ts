import type {
  AgentRunSummary,
  ApprovalDecisionRequest,
  ApprovalSummary,
  AssetSummary,
  AssetCreateRequest,
  ChatCitation,
  ChatCompletionRequest,
  ChatCompletionResponse,
  ChatSessionRenameRequest,
  ChatSessionSummary,
  ChatStreamEvent,
  ChatTurn,
  ContainerTerminalCapacity,
  ContainerTerminalCapabilities,
  ContainerTerminalPreflight,
  ContainerTerminalRequest,
  ContainerTerminalRecovery,
  ContainerTerminalRecoveryList,
  ContainerTerminalSession,
  TerminalCommandHistoryStatus,
  TerminalCommandPage,
  TerminalCommandRecord,
  TerminalRecordingTools,
  ContextMemory,
  ContextSnapshot,
  ContextSourceReference,
  ContextStatus,
  CredentialStatus,
  EngagementSummary,
  EngagementCreateRequest,
  ExecutionCapabilities,
  ExecutionChatAttachment,
  ExecutionOutputPage,
  ExecutionPreflight,
  ExecutionRequest,
  EvidenceSummary,
  EvidenceUploadRequest,
  EngagementScopePolicy,
  EngagementScopeUpdateRequest,
  ScopeImport,
  ScopeImportApplyResult,
  ScopeImportCreateRequest,
  FindingCreateRequest,
  FindingSummary,
  FindingUpdateRequest,
  GeneratedDraft,
  GeneratedDraftContent,
  HealthResponse,
  HarnessProfile,
  HarnessActivityEvent,
  HarnessActivityEventPage,
  HarnessDetailedUsage,
  HarnessInteraction,
  HarnessTurnDetail,
  HarnessSessionActivity,
  HarnessSessionSummary,
  KnowledgeIngestRequest,
  KnowledgeSource,
  MissionCreateRequest,
  McpServerProfile,
  OperatorExecution,
  ObservationSummary,
  ObservationCreateRequest,
  ObservationUpdateRequest,
  OperatorProfile,
  OperatorProfileCreateRequest,
  OperatorProfileUpdateRequest,
  Page,
  PersistedChatMessage,
  LocalProviderDetection,
  ProviderCatalogEntry,
  ProviderCreateRequest,
  ProviderHealth,
  ProviderRuntimeHealth,
  ProviderUpdateRequest,
  ReportCreateRequest,
  ReportNoteTransform,
  ReportRender,
  ReportSummary,
  ReportUpdateRequest,
  RunStopRequest,
  RunnerProfile,
  RunnerProfileUpdateRequest,
  SetupControlResponse,
  SetupStatus,
  ToolArtifactReference,
  ToolOutputReadResult,
  ToolOutputSearchResult,
  WorkspaceListing,
  WorkspacePreview,
  WorkspaceResetResult,
  WorkspaceUploadResult,
  WritingTransformRequest,
  WritingTransformResponse,
} from "./types";
import { websocketAuthProtocol } from "./events";
import {
  logDiagnostic,
  newOperationId,
  rememberDiagnosticErrorPresentation,
  type DiagnosticFile,
  type DiagnosticActionResult,
  type DiagnosticIncident,
  type DiagnosticRecord,
  type DiagnosticSettings,
  type DiagnosticStatus,
} from "../diagnostics";
import { logCaughtDiagnostic } from "../diagnostics";

type JsonObject = Record<string, unknown>;

interface WireEntity extends JsonObject {
  id: string;
  created_at: string;
  updated_at: string;
  revision: number;
}

interface WireSetupStatus {
  application_stage?: SetupStatus["applicationStage"];
  stage_detail?: string | null;
  stage_started_at?: string | null;
  retryable?: boolean;
  recovery_actions?: Array<{ id: string; label: string; destination?: string | null }>;
  core: { status: SetupStatus["core"]["status"]; detail?: string | null };
  scratch_project_id?: string | null;
  terminal: {
    status: SetupStatus["terminal"]["status"];
    runner_profile_id?: string | null;
    candidates?: Array<{
      candidate_id?: string | null;
      runner_profile_id?: string | null;
      source: "configured" | "detected";
      name: string;
      runtime: "podman" | "docker";
      executable: string;
      context?: string | null;
      platform: "linux/amd64" | "linux/arm64";
      isolation: "rootless" | "podman_machine" | "docker_desktop_vm";
      healthy: boolean;
      detail?: string | null;
    }>;
    image_preparation?: {
      phase: SetupStatus["terminal"]["imagePreparation"]["phase"];
      operation_id?: string | null;
      project_id?: string | null;
      progress_percent?: number | null;
      progress_indeterminate?: boolean;
      can_cancel?: boolean;
      can_retry?: boolean;
      image_digest?: string | null;
      started_at?: string | null;
      completed_at?: string | null;
      detail?: string | null;
    };
    detail?: string | null;
  };
  assistant: {
    status: SetupStatus["assistant"]["status"];
    provider_profile_id?: string | null;
    detail?: string | null;
  };
}

interface WireTerminalRecordingTools {
  engagement_id: string;
  inventory_status: "verified" | "unavailable";
  runtime_image_digest?: string | null;
  manifest_sha256?: string | null;
  default_tools: string[];
  custom_tools: string[];
  disabled_tools: string[];
  effective_tools: string[];
  revision: number;
  updated_at?: string | null;
}

interface WireSetupControlResponse {
  operation: SetupControlResponse["operation"];
  accepted: boolean;
  idempotent: boolean;
  operation_id?: string | null;
  setup: WireSetupStatus;
}

interface WireEngagement extends WireEntity {
  name: string;
  description?: string;
  client_name?: string | null;
  status: EngagementSummary["status"];
  tags?: string[];
  metadata?: JsonObject;
}

interface WireAgentRun extends WireEntity {
  engagement_id: string;
  objective: string;
  status: AgentRunSummary["status"];
  started_at?: string | null;
  completed_at?: string | null;
  metadata?: JsonObject;
  backend?: "native" | "harness";
  harness_profile_id?: string | null;
  harness_session_id?: string | null;
}

interface WireApproval extends WireEntity {
  engagement_id: string;
  run_id: string;
  origin?: "mission" | "chat";
  status: string;
  risk_class: string;
  exact_request: JsonObject;
  target?: string | null;
  credential_class?: string | null;
  expected_effects?: string[];
  policy_rationale: string;
  requested_by: string;
  requested_at: string;
  expires_at?: string | null;
}

interface WireAsset extends WireEntity {
  engagement_id: string;
  asset_type?: string;
  name: string;
  address?: string | null;
  hostname?: string | null;
  criticality?: AssetSummary["criticality"];
  exposed?: boolean | null;
  tags?: string[];
  metadata?: JsonObject;
}

interface WireFinding extends WireEntity {
  engagement_id: string;
  title: string;
  description?: string;
  severity: FindingSummary["severity"];
  severity_rationale?: string;
  status: string;
  asset_ids?: string[];
  evidence_ids?: string[];
  cve_ids?: string[];
  cwe_ids?: string[];
  verifier_id?: string | null;
  verified_at?: string | null;
}

interface WireReport extends WireEntity {
  revision: number;
  engagement_id: string;
  title: string;
  status: string;
  executive_summary?: string;
  finding_ids?: string[];
  observation_ids?: string[];
  note_transforms?: WireReportNoteTransform[];
  artifact_ids?: string[];
  executive_summary_provenance?: WireAIWritingProvenance | null;
  signed_off_by?: string | null;
  signed_off_at?: string | null;
  metadata?: JsonObject;
}

interface WireAIWritingProvenance extends JsonObject {
  provider_profile_id: string;
  model: string;
  prompt_version: string;
  source_sha256: string;
  instruction: string;
  generated_at: string;
  provider_request_id?: string | null;
}

interface WireReportNoteTransform extends JsonObject {
  observation_id: string;
  source_revision: number;
  title: string;
  body: string;
  provenance: WireAIWritingProvenance;
}

interface WireWritingTransformResponse extends JsonObject {
  content: string;
  provenance: WireAIWritingProvenance;
  usage: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
  };
}

interface WireObservation extends WireEntity {
  engagement_id: string;
  observation_type: string;
  title: string;
  body?: string;
  asset_ids?: string[];
  service_ids?: string[];
  evidence_ids?: string[];
  source?: string | null;
  confidence?: number;
  metadata?: JsonObject;
}

interface WireReportRender extends WireEntity {
  engagement_id: string;
  report_id: string;
  report_revision: number;
  input_fingerprint: string;
  template_version: string;
  renderer_version: string;
  status: ReportRender["status"];
  warnings?: string[];
  generated_at?: string | null;
  error_detail?: string | null;
}

interface WireGeneratedDraft extends WireEntity {
  engagement_id: string;
  execution_id: string;
  provider_profile_id: string;
  model: string;
  prompt_version: string;
  context_fingerprint: string;
  status: GeneratedDraft["status"];
  content?: {
    title: string;
    summary?: string;
    observations?: string[];
    potential_findings?: Array<{ title: string; rationale?: string }>;
    evidence_ids?: string[];
  } | null;
  observation_id?: string | null;
  provider_request_id?: string | null;
  error_detail?: string | null;
  metadata?: JsonObject;
}

interface WireExecutionChatAttachment extends JsonObject {
  session: { id: string };
  context_fingerprint: string;
  categories: string[];
}

interface WireEvidence extends WireEntity {
  engagement_id: string;
  evidence_type: string;
  title: string;
  description?: string;
  artifact_id?: string | null;
  finding_id?: string | null;
  execution_id?: string | null;
  asset_ids?: string[];
  sha256?: string | null;
  captured_at: string;
  captured_by?: string | null;
  source_version?: string | null;
  metadata?: JsonObject;
}

interface WireOperatorProfile extends WireEntity {
  revision: number;
  display_name: string;
  email?: string | null;
  role?: string | null;
  active: boolean;
  activated_at?: string | null;
  metadata?: JsonObject;
}

interface WireProvider extends WireEntity {
  name: string;
  provider_type: string;
  endpoint?: string | null;
  enabled?: boolean;
  is_local?: boolean;
  secret_ref?: string | null;
  model_allowlist?: string[];
  capabilities?: Record<string, boolean>;
  capability_verifications?: Record<
    string,
    {
      model: string;
      status: "verified" | "failed";
      checked_at: string;
      contract_version: string;
      failure_detail?: string | null;
    }
  >;
  privacy?: {
    local_only?: boolean;
    retention?: string | null;
    residency?: string[];
    permits_sensitive_data?: boolean;
  };
  metadata?: JsonObject;
}

interface WireProviderRuntimeHealth extends JsonObject {
  provider_id: string;
  healthy: boolean;
  models?: string[];
  detail?: string | null;
}

interface WireProviderVerificationResponse extends JsonObject {
  provider_id: string;
  provider_revision: number;
  verification: {
    model: string;
    status: "verified" | "failed";
    checked_at: string;
    contract_version: string;
    failure_detail?: string | null;
  };
}

interface WireProviderCatalogEntry extends JsonObject {
  flavor: string;
  adapter: string;
  display_name: string;
  local: boolean;
  default_base_url?: string | null;
  suggested_key_env?: string | null;
  support_tier: ProviderCatalogEntry["supportTier"];
  notes?: string | null;
}

interface WireLocalProviderDetection extends JsonObject {
  flavor: string;
  display_name: string;
  endpoint: string;
  models?: string[];
}

interface WireKnowledgeSource extends WireEntity {
  engagement_id: string;
  name: string;
  source_type: string;
  artifact_id?: string | null;
  status: string;
  citation?: string | null;
  document_count?: number;
  metadata?: JsonObject;
}

interface WireChatCitation extends JsonObject {
  source_id: string;
  name: string;
  citation?: string | null;
  artifact_id?: string | null;
  chunk_id: string;
  page?: number | null;
  excerpt: string;
}

interface WireChatCompletion extends JsonObject {
  turn_id?: string | null;
  session_id?: string | null;
  backend?: "provider" | "harness";
  provider_id?: string | null;
  harness_profile_id?: string | null;
  harness_session_id?: string | null;
  harness_turn_id?: string | null;
  model: string;
  message: { id?: string | null; role: "assistant"; content: string };
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  context_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
  finish_reason?: string | null;
  provider_request_id?: string | null;
  citations?: WireChatCitation[];
}

interface WireContextSourceReference extends JsonObject {
  source_kind: string;
  source_id: string;
  sequence?: number | null;
}

interface WireContextMemoryItem extends JsonObject {
  text: string;
  sources?: WireContextSourceReference[];
}

interface WireContextMemory extends JsonObject {
  objective?: string | null;
  summary: string;
  confirmed_facts?: WireContextMemoryItem[];
  decisions?: WireContextMemoryItem[];
  constraints?: WireContextMemoryItem[];
  corrections?: WireContextMemoryItem[];
  open_questions?: WireContextMemoryItem[];
  evidence_ids?: string[];
  artifact_ids?: string[];
}

interface WireContextSnapshot extends WireEntity {
  owner_type: "chat_session" | "agent_run";
  owner_id: string;
  version: number;
  status: "ready" | "failed";
  compacted_through: number;
  memory?: WireContextMemory | null;
  source_references?: WireContextSourceReference[];
  provider_profile_id: string;
  model: string;
  prompt_version: string;
  usage?: WireChatCompletion["usage"];
  cost_usd?: number;
  error?: string | null;
}

interface WireContextStatus extends JsonObject {
  owner_type: "chat_session" | "agent_run";
  owner_id: string;
  status: "not_needed" | "ready" | "stale" | "failed" | "runtime_managed";
  context_window: number;
  max_output_tokens: number;
  target_input_tokens: number;
  estimated_input_tokens?: number;
  compacted_through?: number;
  source_references?: WireContextSourceReference[];
  compaction_usage?: WireChatCompletion["usage"];
  compaction_cost_usd?: number;
  snapshot?: WireContextSnapshot | null;
}

interface WireChatStreamEvent extends JsonObject {
  type:
    | "started"
    | "delta"
    | "message_delta"
    | "item_started"
    | "item_completed"
    | "usage"
    | "interrupted"
    | "completed"
    | "tool_started"
    | "tool_completed"
    | "approval_required"
    | "status"
    | "turn_status"
    | "item_upsert"
    | "output_delta"
    | "approval"
    | "interaction"
    | "checkpoint"
    | "notice"
    | "done"
    | "error";
  schema_version?: "nebula.harness-activity/v1";
  id?: string;
  sequence?: number;
  vendor?: "codex_app_server" | "claude_agent_sdk";
  occurred_at?: string;
  external_session_id?: string;
  external_turn_id?: string;
  item_id?: string;
  parent_item_id?: string;
  item_kind?: HarnessActivityEvent["itemKind"];
  item_status?: string;
  title?: string;
  stream?: string;
  message?: WireChatCompletion["message"] | string;
  session_id?: string;
  backend?: "provider" | "harness";
  harness_profile_id?: string;
  citations?: WireChatCitation[];
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  context_usage?: WireChatCompletion["context_usage"];
  finish_reason?: string;
  provider_request_id?: string;
  detailed_usage?: JsonObject;
  artifact_ids?: string[];
  turn_id?: string;
  tool_call_id?: string;
  capability?: string;
  arguments?: JsonObject;
  status?: string;
  summary?: string;
  evidence_ids?: string[];
  result_artifact_id?: string | null;
  artifacts?: Array<{
    artifact_id: string;
    kind: ToolArtifactReference["kind"];
    filename?: string | null;
    media_type: string;
    byte_count: number;
    observed_byte_count: number;
    sha256: string;
    searchable: boolean;
    truncated: boolean;
  }>;
  receipt?: JsonObject;
  step?: number;
  approval?: JsonObject;
  approval_id?: string;
  tool_name?: string;
  provider_id?: string;
  model?: string;
  delta?: string;
  detail?: string;
  payload?: JsonObject;
  harness_session_id?: string;
  harness_turn_id?: string;
}

interface WireHarnessActivityEventPage extends JsonObject {
  events: WireChatStreamEvent[];
  next_sequence: number;
}

interface WireHarnessTurn extends WireEntity {
  status: HarnessTurnDetail["status"];
  origin: HarnessTurnDetail["origin"];
  harness_session_id: string;
  chat_session_id?: string | null;
  run_id?: string | null;
  error?: string | null;
  metadata?: JsonObject;
}

interface WireChatSession extends WireEntity {
  engagement_id: string;
  title: string;
  backend?: "provider" | "harness";
  provider_profile_id?: string | null;
  harness_profile_id?: string | null;
  harness_session_id?: string | null;
  model?: string | null;
  metadata?: JsonObject;
}

interface WireHarnessProfile extends WireEntity {
  name: string;
  kind: HarnessProfile["kind"];
  connection_mode: HarnessProfile["connectionMode"];
  transport: HarnessProfile["transport"];
  executable?: string | null;
  endpoint?: string | null;
  auth_mode: HarnessProfile["authMode"];
  secret_ref?: string | null;
  default_model?: string | null;
  enabled: boolean;
  privacy?: { local_only?: boolean; permits_sensitive_data?: boolean };
  native_capabilities?: {
    workspace_access?: "none" | "read" | "write";
    shell?: boolean;
    web_search?: boolean;
    web_fetch?: boolean;
    browser?: boolean;
    computer_use?: boolean;
    image_generation?: boolean;
    skills?: boolean;
    subagents?: boolean;
  };
  capabilities?: {
    checked_at?: string | null;
    harness_version?: string | null;
    protocol_version?: string | null;
    detail?: string | null;
    models?: string[];
    activity_replay?: boolean;
    reasoning_summaries?: boolean;
    plans?: boolean;
    live_command_output?: boolean;
    file_diffs?: boolean;
    detailed_usage?: boolean;
    interactions?: boolean;
    hooks?: boolean;
    subagent_activity?: boolean;
    subagent_control?: boolean;
    checkpoint_rewind?: boolean;
    steering?: boolean;
    interruption?: boolean;
  };
}

interface WireHarnessInteraction extends WireEntity {
  harness_turn_id: string;
  status: HarnessInteraction["status"];
  kind: HarnessInteraction["kind"];
  prompt: string;
  questions?: JsonObject[];
  response_schema?: JsonObject;
  contains_secret?: boolean;
}

interface WireMcpServerProfile extends WireEntity {
  name: string;
  transport: McpServerProfile["transport"];
  command?: string | null;
  arguments?: string[];
  url?: string | null;
  auth_mode: McpServerProfile["authMode"];
  enabled: boolean;
  required: boolean;
  trusted_stdio: boolean;
  default_approval: McpServerProfile["defaultApproval"];
  tool_overrides?: Record<string, McpServerProfile["defaultApproval"]>;
  capabilities?: {
    checked_at?: string | null;
    detail?: string | null;
    tools?: Array<{
      name: string;
      description?: string;
      read_only?: boolean;
      destructive?: boolean;
      open_world?: boolean;
      credentialed?: boolean | null;
    }>;
  };
}

interface WireHarnessSession extends WireEntity {
  engagement_id: string;
  harness_profile_id: string;
  model: string;
  status: HarnessSessionSummary["status"];
  mcp_server_ids?: string[];
  last_activity_at: string;
}

interface WireHarnessSessionActivity extends JsonObject {
  session_id: string;
  session_status: HarnessSessionSummary["status"];
  busy: boolean;
  live: boolean;
  turn_id?: string | null;
  turn_status?: HarnessSessionActivity["turnStatus"] | null;
  turn_origin?: HarnessSessionActivity["turnOrigin"] | null;
  started_at?: string | null;
  last_activity_at: string;
  detail: string;
}

interface WireChatTurn extends WireEntity {
  session_id: string;
  status: ChatTurn["status"];
  approval_id?: string | null;
  harness_turn_id?: string | null;
  tool_call_ids?: string[];
}

interface WirePersistedChatMessage extends WireEntity {
  engagement_id: string;
  session_id: string;
  sequence: number;
  role: "user" | "assistant";
  content: string;
  provider_profile_id?: string | null;
  model?: string | null;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  } | null;
  finish_reason?: string | null;
  provider_request_id?: string | null;
  citations?: WireChatCitation[];
  metadata?: JsonObject;
}

interface WireExecutionLimits extends JsonObject {
  cpu_count: number;
  memory_mb: number;
  pids: number;
  timeout_seconds: number;
  output_bytes_per_stream: number;
}

interface WireExecutionRuntime extends JsonObject {
  language: "bash" | "sh" | "python";
  interpreter: string;
  arguments?: string[];
  runtime_digest: string;
  image: string;
  runner_profile_id: string;
  runner_profile_revision: number;
  runner_runtime: "docker" | "podman";
  runner_isolation: string;
  runner_executable: string;
  runner_platform: string;
  runner_context?: string | null;
  runner_socket?: string | null;
}

interface WireExecutionNetwork extends JsonObject {
  mode: "none" | "scoped";
  target?: string | null;
  ports?: number[];
  resolved_addresses?: string[];
  scope_policy_id?: string | null;
  scope_policy_revision?: number | null;
}

interface WireExecutionOrigin extends JsonObject {
  kind: "assistant_message" | "rerun" | "selection";
  message_id?: string | null;
  block_ordinal?: number | null;
  block_sha256?: string | null;
  selection_start_byte?: number | null;
  selection_end_byte?: number | null;
  execution_id?: string | null;
  source_kind?: string | null;
  source_id?: string | null;
  source_label?: string | null;
  source_sha256?: string | null;
}

interface WireOperatorExecution extends WireEntity {
  engagement_id: string;
  operator_id: string;
  origin: WireExecutionOrigin;
  language: "bash" | "sh" | "python";
  source_sha256: string;
  source_artifact_id: string;
  source_preview?: string;
  runtime: WireExecutionRuntime;
  network: WireExecutionNetwork;
  limits: WireExecutionLimits;
  workspace: "/workspace";
  policy_decision: string;
  status: OperatorExecution["status"];
  error_code?: string | null;
  error_detail?: string | null;
  queued_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  exit_code?: number | null;
  output_truncated?: boolean;
  evidence_id?: string | null;
  workspace_changes?: Array<{
    path: string;
    change: "added" | "modified" | "deleted";
    size?: number | null;
  }>;
}

interface WireExecutionPreflight extends JsonObject {
  allowed: boolean;
  error_code?: string | null;
  detail: string;
  canonical_language?: "bash" | "sh" | "python" | null;
  source_sha256?: string | null;
  runtime?: WireExecutionRuntime | null;
  network?: WireExecutionNetwork | null;
  limits: WireExecutionLimits;
  workspace: "/workspace";
  policy_rule?: string | null;
  preview_fingerprint?: string | null;
  preview_token?: string | null;
  expires_at?: string | null;
}

interface WireExecutionCapabilities extends JsonObject {
  engagement_id: string;
  ready: boolean;
  runtimes: Array<{
    language: "bash" | "sh" | "python";
    aliases: string[];
    offline: boolean;
    scoped_network: boolean;
    detail?: string | null;
  }>;
  limits: WireExecutionLimits;
  workspace: "/workspace";
}

interface WireContainerTerminalCapabilities extends JsonObject {
  engagement_id: string;
  ready: boolean;
  detail?: string | null;
  source_image: string;
  installed_packages: string[];
  network: WireContainerTerminalNetwork;
  security: WireContainerTerminalSecurity;
  workspace: "/workspace";
  limits: WireExecutionLimits;
  idle_timeout_seconds: number;
  fresh_container: true;
}

interface WireContainerTerminalRuntime extends JsonObject {
  source_image: string;
  base_image: string;
  base_image_digest: string;
  image: string;
  image_digest: string;
  installed_packages: string[];
  interpreter: string;
  arguments: string[];
  runner_profile_id: string;
  runner_profile_revision: number;
  runner_runtime: "docker" | "podman";
  runner_isolation: string;
  runner_executable: string;
  runner_platform: string;
  runner_context?: string | null;
}

interface WireContainerTerminalNetwork extends JsonObject {
  mode: "unrestricted";
  runtime_network: "bridge";
  published_ports: number[];
}

interface WireContainerTerminalSecurity extends JsonObject {
  container_user: "root";
  root_filesystem: "writable";
  linux_capabilities: string[];
  no_new_privileges: boolean;
  host_network: boolean;
  runtime_socket: boolean;
  host_shell: boolean;
}

interface WireContainerTerminalPreflight extends JsonObject {
  allowed: boolean;
  error_code?: string | null;
  detail: string;
  runtime?: WireContainerTerminalRuntime | null;
  network: WireContainerTerminalNetwork;
  security: WireContainerTerminalSecurity;
  limits: WireExecutionLimits;
  workspace: "/workspace";
  policy_rule?: string | null;
  preview_fingerprint?: string | null;
  preview_token?: string | null;
  expires_at?: string | null;
  idle_timeout_seconds: number;
  fresh_container: true;
}

interface WireContainerTerminalSession extends JsonObject {
  session_id: string;
  created_at: string;
  websocket_ticket: string;
  ticket_expires_at: string;
  websocket_path: string;
  reconnect_grace_seconds: number;
  replay_max_bytes: number;
  last_sequence: number;
}

interface WireContainerTerminalRecovery extends JsonObject {
  active: boolean;
  session?: WireContainerTerminalSession | null;
  runtime?: WireContainerTerminalRuntime | null;
}

interface WireContainerTerminalRecoveryList extends JsonObject {
  sessions: Array<{
    session: WireContainerTerminalSession;
    runtime: WireContainerTerminalRuntime;
  }>;
}

interface WireContainerTerminalCapacity extends JsonObject {
  active_sessions: number;
  available_sessions: number;
  max_active_sessions: number;
}

interface WireWorkspaceListing extends JsonObject {
  engagement_id: string;
  path: string;
  entries: Array<{
    path: string;
    name: string;
    kind: "file" | "directory" | "symlink" | "other";
    size: number;
    modified_at: string;
  }>;
  offset: number;
  next_offset?: number | null;
  total: number;
}

interface WireWorkspacePreview extends JsonObject {
  engagement_id: string;
  path: string;
  text: string;
  bytes_returned: number;
  truncated: boolean;
  preview_sha256: string;
}

interface WireRunnerProfile extends JsonObject {
  id: string;
  name: string;
  runtime_type?: RunnerProfile["runtimeType"];
  runtime?: RunnerProfile["runtimeType"];
  executable: string;
  context?: string | null;
  socket?: string | null;
  platform?: string;
  isolation_mode?: RunnerProfile["isolationMode"];
  isolation?: RunnerProfile["isolationMode"];
  state?: RunnerProfile["state"];
  enabled?: boolean;
  healthy?: boolean;
  last_checked_at?: string | null;
  last_health_at?: string | null;
  detail?: string | null;
  last_health_detail?: string | null;
  seccomp_profile?: string | null;
  revision?: number;
}

interface WireEngagementScope extends JsonObject {
  id?: string;
  engagement_id: string;
  allowed_cidrs?: string[];
  allowed_domains?: string[];
  allowed_urls?: string[];
  allowed_ports?: number[];
  not_before?: string | null;
  not_after?: string | null;
  prohibited_actions?: string[];
  local_only?: boolean;
  max_concurrency?: number;
  grants?: Array<{
    risk_classes?: string[];
    tool_names?: string[];
    targets?: string[];
    granted_at?: string;
    expires_at?: string;
    granted_by?: string;
  }>;
  revision?: number;
}

interface WireScopeImport extends WireEntity {
  engagement_id: string;
  artifact_id: string;
  filename: string;
  source_type: string;
  source_sha256: string;
  base_scope_revision: number;
  status: ScopeImport["status"];
  candidates?: Array<{
    id: string;
    target_type: "cidr" | "domain" | "url";
    classification: "allowed" | "excluded" | "ambiguous";
    raw_value: string;
    normalized_value?: string | null;
    source_location?: string;
    source_excerpt?: string;
    warnings?: string[];
  }>;
  warnings?: string[];
  provenance?: {
    provider_profile_id: string;
    model: string;
    prompt_version: string;
    source_sha256: string;
    generated_at: string;
    provider_request_ids?: string[];
  } | null;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  error_detail?: string | null;
  applied_candidate_ids?: string[];
  applied_scope_policy_id?: string | null;
  applied_scope_revision?: number | null;
}

export interface ApiClientOptions {
  baseUrl?: string;
  token?: string | (() => string | undefined);
  fetch?: typeof globalThis.fetch;
}

export class ApiError extends Error {
  readonly status: number;
  readonly requestId?: string;
  readonly details?: unknown;
  readonly errorId?: string;
  readonly code?: string;
  readonly feature?: string;
  readonly retryable?: boolean;
  readonly helpArticle?: string;
  readonly operationId?: string;
  readonly reasonCode?: string;
  readonly operatorDetail?: string;
  readonly impact?: string;
  readonly remediationId?: string;
  readonly recoveryAction?: string;
  readonly recoveryDestination?: string;

  constructor(
    message: string,
    status: number,
    requestId?: string,
    details?: unknown,
  ) {
    const envelope =
      details && typeof details === "object"
        ? (details as Record<string, unknown>)
        : undefined;
    const errorId = stringField(envelope?.error_id);
    const correlatedRequestId = requestId ?? stringField(envelope?.request_id);
    const reference = errorId ?? correlatedRequestId;
    super(reference ? `${message} Reference: ${reference}.` : message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = correlatedRequestId;
    this.details = details;
    this.errorId = errorId;
    this.code = stringField(envelope?.code);
    this.feature = stringField(envelope?.feature);
    this.retryable =
      typeof envelope?.retryable === "boolean" ? envelope.retryable : undefined;
    this.helpArticle = stringField(envelope?.help_article);
    this.operationId = stringField(envelope?.operation_id);
    this.reasonCode = stringField(envelope?.reason_code);
    this.operatorDetail = stringField(envelope?.operator_detail);
    this.impact = stringField(envelope?.impact);
    this.remediationId = stringField(envelope?.remediation_id);
    this.recoveryAction = stringField(envelope?.recovery_action);
    this.recoveryDestination = stringField(envelope?.recovery_destination);
    rememberDiagnosticErrorPresentation(reference, {
      retryable: this.retryable,
      code: this.code,
      reasonCode: this.reasonCode,
      operatorDetail: this.operatorDetail,
      impact: this.impact,
      remediationId: this.remediationId,
    });
  }
}

function normalizeBaseUrl(value?: string): string {
  const origin =
    value?.trim() || globalThis.location?.origin || "http://127.0.0.1";
  const withoutSlash = origin.replace(/\/+$/, "");
  return withoutSlash.endsWith("/api/v1")
    ? withoutSlash
    : `${withoutSlash}/api/v1`;
}

function numberField(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function stringField(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function objectOptions(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return { ...(value as Record<string, unknown>) };
}

function configuredDefaultModel(value?: string): string | undefined {
  return value?.trim() || undefined;
}

function configuredModelAllowlist(
  values: string[] | undefined,
  defaultModel?: string,
): string[] {
  const selected = [
    ...new Set((values ?? []).map((value) => value.trim()).filter(Boolean)),
  ];
  return selected.length && defaultModel
    ? [...new Set([defaultModel, ...selected])]
    : selected;
}

function normalizedIdentifiers(values?: string[]): string[] {
  return [
    ...new Set(
      (values ?? []).map((value) => value.trim().toUpperCase()).filter(Boolean),
    ),
  ];
}

function page<T>(items: T[]): Page<T> {
  return { items, total: items.length };
}

const MAX_LIST_LIMIT = 1_000;

function engagementQuery(engagementId: string, offset: number): string {
  return `engagement_id=${encodeURIComponent(engagementId)}&limit=${MAX_LIST_LIMIT}&offset=${offset}`;
}

function globalListPath(resource: string, offset: number): string {
  return `${resource}?limit=${MAX_LIST_LIMIT}&offset=${offset}`;
}

function mapEngagement(value: WireEngagement): EngagementSummary {
  return {
    id: value.id,
    name: value.name,
    description: value.description ?? "",
    clientName: value.client_name ?? undefined,
    status: value.status,
    tags: value.tags ?? [],
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    scopeAssetCount: numberField(value.metadata?.scope_asset_count),
  };
}

function mapTerminalRecordingTools(
  value: WireTerminalRecordingTools,
): TerminalRecordingTools {
  return {
    engagementId: value.engagement_id,
    inventoryStatus: value.inventory_status,
    runtimeImageDigest: value.runtime_image_digest ?? undefined,
    manifestSha256: value.manifest_sha256 ?? undefined,
    defaultTools: value.default_tools,
    customTools: value.custom_tools,
    disabledTools: value.disabled_tools,
    effectiveTools: value.effective_tools,
    revision: value.revision,
    updatedAt: value.updated_at ?? undefined,
  };
}

function mapRun(value: WireAgentRun): AgentRunSummary {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    title: value.objective,
    status: value.status,
    startedAt: value.started_at ?? undefined,
    updatedAt: value.updated_at,
    completedTasks: numberField(value.metadata?.completed_tasks),
    totalTasks: numberField(value.metadata?.total_tasks),
    spentUsd:
      typeof value.metadata?.spent_usd === "number"
        ? value.metadata.spent_usd
        : undefined,
    backend: value.backend ?? "native",
    harnessProfileId: value.harness_profile_id ?? undefined,
    harnessSessionId: value.harness_session_id ?? undefined,
  };
}

function mapApprovalStatus(value: string): ApprovalSummary["status"] {
  if (value === "edited") return "approved";
  if (
    ["pending", "approved", "rejected", "expired", "cancelled"].includes(value)
  ) {
    return value as ApprovalSummary["status"];
  }
  return "cancelled";
}

function mapApproval(value: WireApproval): ApprovalSummary {
  const request = value.exact_request ?? {};
  const command =
    Array.isArray(request.argv) &&
    request.argv.every((item) => typeof item === "string")
      ? (request.argv as string[])
      : undefined;
  return {
    id: value.id,
    runId: value.run_id,
    engagementId: value.engagement_id,
    origin: value.origin ?? "mission",
    status: mapApprovalStatus(value.status),
    risk: mapRiskClass(value.risk_class),
    toolName: stringField(request.tool_name) ?? "Tool request",
    agentName: value.requested_by,
    target: value.target ?? "No network target",
    rationale: value.policy_rationale,
    expectedEffects:
      (value.expected_effects ?? []).join("; ") || "No effects declared",
    arguments:
      request.arguments && typeof request.arguments === "object"
        ? (request.arguments as JsonObject)
        : {},
    command,
    image: stringField(request.image),
    runtimeDigest: stringField(request.runtime_digest),
    credentialClass: value.credential_class ?? undefined,
    expiresAt: value.expires_at ?? undefined,
    createdAt: value.requested_at ?? value.created_at,
    argumentEditing: request.argument_editing !== false,
  };
}

function mapRiskClass(value: string): ApprovalSummary["risk"] {
  if (value === "credential_use") return "credentialed";
  if (["exploitation", "persistence"].includes(value)) return "exploit";
  if (value === "destructive") return "destructive";
  if (["active_scan", "workspace_write", "scope_change"].includes(value))
    return "active";
  return "passive";
}

const assetKinds = new Set<AssetSummary["kind"]>([
  "host",
  "domain",
  "url",
  "cloud",
  "repository",
  "other",
]);

function mapAsset(value: WireAsset): AssetSummary {
  const kind = assetKinds.has(value.asset_type as AssetSummary["kind"])
    ? (value.asset_type as AssetSummary["kind"])
    : "other";
  return {
    id: value.id,
    engagementId: value.engagement_id,
    displayName: value.name || value.hostname || value.address || value.id,
    kind,
    address: value.address ?? undefined,
    hostname: value.hostname ?? undefined,
    criticality: value.criticality ?? "medium",
    exposure:
      value.exposed === true
        ? "external"
        : value.exposed === false
          ? "internal"
          : "unknown",
    tags: value.tags ?? [],
    serviceCount:
      typeof value.metadata?.service_count === "number"
        ? value.metadata.service_count
        : undefined,
    findingCount:
      typeof value.metadata?.finding_count === "number"
        ? value.metadata.finding_count
        : undefined,
    lastSeenAt: stringField(value.metadata?.last_seen_at),
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

const findingStatuses = new Set<FindingSummary["status"]>([
  "candidate",
  "validated",
  "confirmed",
  "accepted_risk",
  "false_positive",
  "remediated",
  "retest_passed",
  "retest_failed",
]);

function mapFinding(value: WireFinding): FindingSummary {
  const normalizedStatus = value.status.replaceAll(
    "-",
    "_",
  ) as FindingSummary["status"];
  return {
    id: value.id,
    engagementId: value.engagement_id,
    title: value.title,
    description: value.description ?? "",
    severity: value.severity,
    severityRationale: value.severity_rationale ?? "",
    status: findingStatuses.has(normalizedStatus)
      ? normalizedStatus
      : "candidate",
    assetIds: value.asset_ids ?? [],
    evidenceIds: value.evidence_ids ?? [],
    affectedAssetCount: value.asset_ids?.length ?? 0,
    evidenceCount: value.evidence_ids?.length ?? 0,
    cveIds: value.cve_ids ?? [],
    cweIds: value.cwe_ids ?? [],
    verifierId: value.verifier_id ?? undefined,
    verifiedAt: value.verified_at ?? undefined,
    updatedAt: value.updated_at,
    revision: value.revision,
  };
}

function mapReport(value: WireReport): ReportSummary {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    title: value.title,
    status: value.status,
    executiveSummary: value.executive_summary ?? "",
    findingIds: value.finding_ids ?? [],
    observationIds: value.observation_ids ?? [],
    noteTransforms: (value.note_transforms ?? []).map(mapReportNoteTransform),
    artifactIds: value.artifact_ids ?? [],
    executiveSummaryProvenance: value.executive_summary_provenance
      ? mapAIWritingProvenance(value.executive_summary_provenance)
      : undefined,
    signedOffBy: value.signed_off_by ?? undefined,
    signedOffAt: value.signed_off_at ?? undefined,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    revision: value.revision,
  };
}

function mapAIWritingProvenance(value: WireAIWritingProvenance) {
  return {
    providerProfileId: value.provider_profile_id,
    model: value.model,
    promptVersion: value.prompt_version,
    sourceSha256: value.source_sha256,
    instruction: value.instruction,
    generatedAt: value.generated_at,
    providerRequestId: value.provider_request_id ?? undefined,
  };
}

function mapReportNoteTransform(
  value: WireReportNoteTransform,
): ReportNoteTransform {
  return {
    observationId: value.observation_id,
    sourceRevision: value.source_revision,
    title: value.title,
    body: value.body,
    provenance: mapAIWritingProvenance(value.provenance),
  };
}

function writingProvenanceBody(
  value: ReportNoteTransform["provenance"],
): JsonObject {
  return {
    provider_profile_id: value.providerProfileId,
    model: value.model,
    prompt_version: value.promptVersion,
    source_sha256: value.sourceSha256,
    instruction: value.instruction,
    generated_at: value.generatedAt,
    provider_request_id: value.providerRequestId,
  };
}

function reportNoteTransformBody(value: ReportNoteTransform): JsonObject {
  return {
    observation_id: value.observationId,
    source_revision: value.sourceRevision,
    title: value.title,
    body: value.body,
    provenance: writingProvenanceBody(value.provenance),
  };
}

function mapObservation(value: WireObservation): ObservationSummary {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    observationType: value.observation_type,
    title: value.title,
    body: value.body ?? "",
    assetIds: value.asset_ids ?? [],
    serviceIds: value.service_ids ?? [],
    evidenceIds: value.evidence_ids ?? [],
    source: value.source ?? undefined,
    confidence: value.confidence ?? 1,
    metadata: value.metadata ?? {},
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    revision: value.revision,
  };
}

function mapReportRender(value: WireReportRender): ReportRender {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    reportId: value.report_id,
    reportRevision: value.report_revision,
    inputFingerprint: value.input_fingerprint,
    templateVersion: value.template_version,
    rendererVersion: value.renderer_version,
    status: value.status,
    warnings: value.warnings ?? [],
    generatedAt: value.generated_at ?? undefined,
    errorDetail: value.error_detail ?? undefined,
    revision: value.revision,
  };
}

function mapGeneratedDraft(value: WireGeneratedDraft): GeneratedDraft {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    executionId: value.execution_id,
    providerProfileId: value.provider_profile_id,
    model: value.model,
    promptVersion: value.prompt_version,
    contextFingerprint: value.context_fingerprint,
    status: value.status,
    content: value.content
      ? {
          title: value.content.title,
          summary: value.content.summary ?? "",
          observations: value.content.observations ?? [],
          potentialFindings: (value.content.potential_findings ?? []).map(
            (item) => ({
              title: item.title,
              rationale: item.rationale ?? "",
            }),
          ),
          evidenceIds: value.content.evidence_ids ?? [],
        }
      : undefined,
    observationId: value.observation_id ?? undefined,
    providerRequestId: value.provider_request_id ?? undefined,
    errorDetail: value.error_detail ?? undefined,
    metadata: value.metadata ?? {},
    revision: value.revision,
  };
}

function wireDraftContent(content: GeneratedDraftContent): JsonObject {
  return {
    title: content.title,
    summary: content.summary,
    observations: content.observations,
    potential_findings: content.potentialFindings.map((item) => ({
      title: item.title,
      rationale: item.rationale,
    })),
    evidence_ids: content.evidenceIds,
  };
}

function mapEvidence(value: WireEvidence): EvidenceSummary {
  const metadata = value.metadata ?? {};
  return {
    id: value.id,
    engagementId: value.engagement_id,
    evidenceType: value.evidence_type,
    title: value.title,
    description: value.description ?? "",
    artifactId: value.artifact_id ?? undefined,
    findingId: value.finding_id ?? undefined,
    executionId: value.execution_id ?? undefined,
    assetIds: value.asset_ids ?? [],
    sha256: value.sha256 ?? undefined,
    capturedAt: value.captured_at,
    capturedBy: value.captured_by ?? undefined,
    sourceVersion: value.source_version ?? undefined,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    metadata: {
      ...metadata,
      filename: stringField(metadata.filename),
      mediaType: stringField(metadata.media_type),
      size: typeof metadata.size === "number" ? metadata.size : undefined,
      source: stringField(metadata.source),
    },
  };
}

function mapOperatorProfile(value: WireOperatorProfile): OperatorProfile {
  return {
    id: value.id,
    displayName: value.display_name,
    email: value.email ?? undefined,
    role: value.role ?? undefined,
    active: value.active,
    activatedAt: value.activated_at ?? undefined,
    metadata: value.metadata ?? {},
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    revision: value.revision,
  };
}

function mapProvider(value: WireProvider): ProviderHealth {
  const capabilities = Object.entries(value.capabilities ?? {})
    .filter(([, supported]) => supported)
    .map(([name]) => name.replaceAll("_", " "));
  const isGateway = ["gateway", "openrouter", "litellm"].some((name) =>
    value.provider_type.toLowerCase().includes(name),
  );
  const metadata = value.metadata ?? {};
  const defaultModel = stringField(metadata.default_model);
  const effectiveDefaultModel = defaultModel ?? value.model_allowlist?.[0];
  const state: ProviderHealth["state"] =
    value.enabled === false ? "offline" : "unchecked";
  return {
    id: value.id,
    revision: value.revision,
    name: value.name,
    providerType: value.provider_type,
    kind: value.is_local ? "local" : isGateway ? "gateway" : "commercial",
    local: value.is_local === true,
    state,
    enabled: value.enabled !== false,
    endpoint: value.endpoint ?? undefined,
    models: value.model_allowlist ?? [],
    availableModels: value.model_allowlist ?? [],
    modelAllowlist: value.model_allowlist ?? [],
    defaultModel,
    effectiveDefaultModel,
    credentialEnv: value.secret_ref?.startsWith("env:")
      ? value.secret_ref.slice(4)
      : undefined,
    credentialRef: value.secret_ref ?? undefined,
    permitsSensitiveData: value.privacy?.permits_sensitive_data === true,
    retention: value.privacy?.retention ?? undefined,
    residency: value.privacy?.residency ?? [],
    options: objectOptions(metadata.options),
    metadata,
    modelCount: value.model_allowlist?.length ?? 0,
    privacy: value.privacy?.local_only
      ? "local_only"
      : value.privacy?.residency?.length
        ? "regional"
        : "cloud",
    capabilities,
    capabilityVerifications: Object.fromEntries(
      Object.entries(value.capability_verifications ?? {}).map(
        ([model, result]) => [
          model,
          {
            model: result.model,
            status: result.status,
            checkedAt: result.checked_at,
            contractVersion: result.contract_version,
            failureDetail: result.failure_detail ?? undefined,
          },
        ],
      ),
    ),
    message:
      value.enabled === false
        ? "Provider profile is disabled."
        : "Profile loaded; run a health check to discover available models.",
  };
}

function mapProviderRuntimeHealth(
  value: WireProviderRuntimeHealth,
): ProviderRuntimeHealth {
  return {
    providerId: value.provider_id,
    healthy: value.healthy,
    models: value.models ?? [],
    detail: value.detail ?? undefined,
  };
}

function mapProviderCatalog(
  value: WireProviderCatalogEntry,
): ProviderCatalogEntry {
  return {
    flavor: value.flavor,
    adapter: value.adapter,
    displayName: value.display_name,
    local: value.local,
    defaultBaseUrl: value.default_base_url ?? undefined,
    suggestedKeyEnv: value.suggested_key_env ?? undefined,
    supportTier: value.support_tier,
    notes: value.notes ?? undefined,
  };
}

function mapLocalProviderDetection(
  value: WireLocalProviderDetection,
): LocalProviderDetection {
  return {
    flavor: value.flavor,
    displayName: value.display_name,
    endpoint: value.endpoint,
    models: value.models ?? [],
  };
}

function mapKnowledgeSource(value: WireKnowledgeSource): KnowledgeSource {
  const metadata = value.metadata ?? {};
  return {
    id: value.id,
    engagementId: value.engagement_id,
    name: value.name,
    sourceType: value.source_type,
    artifactId: value.artifact_id ?? undefined,
    status: value.status,
    citation: value.citation ?? undefined,
    documentCount: numberField(value.document_count),
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    metadata: {
      ...metadata,
      filename: stringField(metadata.filename),
      mediaType: stringField(metadata.media_type),
      size: typeof metadata.size === "number" ? metadata.size : undefined,
      sha256: stringField(metadata.sha256),
      chunkCount:
        typeof metadata.chunk_count === "number"
          ? metadata.chunk_count
          : undefined,
      indexedAt: stringField(metadata.indexed_at),
    },
  };
}

function mapChatCitation(value: WireChatCitation): ChatCitation {
  return {
    sourceId: value.source_id,
    name: value.name,
    citation: value.citation ?? undefined,
    artifactId: value.artifact_id ?? undefined,
    chunkId: value.chunk_id,
    page: value.page ?? undefined,
    excerpt: value.excerpt,
  };
}

function mapChatCompletion(value: WireChatCompletion): ChatCompletionResponse {
  const inputTokens = numberField(value.usage?.input_tokens);
  const outputTokens = numberField(value.usage?.output_tokens);
  const contextInputTokens = numberField(value.context_usage?.input_tokens);
  const contextOutputTokens = numberField(value.context_usage?.output_tokens);
  return {
    turnId: value.turn_id ?? undefined,
    sessionId: value.session_id ?? undefined,
    backend: value.backend ?? "provider",
    providerId: value.provider_id ?? undefined,
    harnessProfileId: value.harness_profile_id ?? undefined,
    harnessSessionId: value.harness_session_id ?? undefined,
    harnessTurnId: value.harness_turn_id ?? undefined,
    model: value.model,
    message: {
      id: value.message.id ?? undefined,
      role: value.message.role,
      content: value.message.content,
    },
    usage: {
      inputTokens,
      outputTokens,
      totalTokens:
        typeof value.usage?.total_tokens === "number"
          ? value.usage.total_tokens
          : inputTokens + outputTokens,
    },
    contextUsage: value.context_usage
      ? {
          inputTokens: contextInputTokens,
          outputTokens: contextOutputTokens,
          totalTokens:
            typeof value.context_usage.total_tokens === "number"
              ? value.context_usage.total_tokens
              : contextInputTokens + contextOutputTokens,
        }
      : undefined,
    finishReason: value.finish_reason ?? undefined,
    providerRequestId: value.provider_request_id ?? undefined,
    citations: (value.citations ?? []).map(mapChatCitation),
  };
}

function mapContextSource(
  value: WireContextSourceReference,
): ContextSourceReference {
  return {
    sourceKind: value.source_kind,
    sourceId: value.source_id,
    sequence: value.sequence ?? undefined,
  };
}

function mapContextMemory(value: WireContextMemory): ContextMemory {
  const items = (values?: WireContextMemoryItem[]) =>
    (values ?? []).map((item) => ({
      text: item.text,
      sources: (item.sources ?? []).map(mapContextSource),
    }));
  return {
    objective: value.objective ?? undefined,
    summary: value.summary,
    confirmedFacts: items(value.confirmed_facts),
    decisions: items(value.decisions),
    constraints: items(value.constraints),
    corrections: items(value.corrections),
    openQuestions: items(value.open_questions),
    evidenceIds: value.evidence_ids ?? [],
    artifactIds: value.artifact_ids ?? [],
  };
}

function mapContextSnapshot(value: WireContextSnapshot): ContextSnapshot {
  const inputTokens = numberField(value.usage?.input_tokens);
  const outputTokens = numberField(value.usage?.output_tokens);
  return {
    id: value.id,
    ownerType: value.owner_type,
    ownerId: value.owner_id,
    version: value.version,
    status: value.status,
    compactedThrough: value.compacted_through,
    memory: value.memory ? mapContextMemory(value.memory) : undefined,
    sourceReferences: (value.source_references ?? []).map(mapContextSource),
    providerId: value.provider_profile_id,
    model: value.model,
    promptVersion: value.prompt_version,
    usage: {
      inputTokens,
      outputTokens,
      totalTokens:
        typeof value.usage?.total_tokens === "number"
          ? value.usage.total_tokens
          : inputTokens + outputTokens,
    },
    costUsd: numberField(value.cost_usd),
    error: value.error ?? undefined,
    createdAt: value.created_at,
  };
}

function mapContextStatus(value: WireContextStatus): ContextStatus {
  const compactionInputTokens = numberField(
    value.compaction_usage?.input_tokens,
  );
  const compactionOutputTokens = numberField(
    value.compaction_usage?.output_tokens,
  );
  return {
    ownerType: value.owner_type,
    ownerId: value.owner_id,
    status: value.status,
    contextWindow: value.context_window,
    maxOutputTokens: value.max_output_tokens,
    targetInputTokens: value.target_input_tokens,
    estimatedInputTokens: numberField(value.estimated_input_tokens),
    compactedThrough: numberField(value.compacted_through),
    sourceReferences: (value.source_references ?? []).map(mapContextSource),
    compactionUsage: {
      inputTokens: compactionInputTokens,
      outputTokens: compactionOutputTokens,
      totalTokens:
        typeof value.compaction_usage?.total_tokens === "number"
          ? value.compaction_usage.total_tokens
          : compactionInputTokens + compactionOutputTokens,
    },
    compactionCostUsd: numberField(value.compaction_cost_usd),
    snapshot: value.snapshot ? mapContextSnapshot(value.snapshot) : undefined,
  };
}

function mapExecutionLimits(value: WireExecutionLimits) {
  return {
    cpuCount: value.cpu_count,
    memoryMb: value.memory_mb,
    pids: value.pids,
    timeoutSeconds: value.timeout_seconds,
    outputBytesPerStream: value.output_bytes_per_stream,
  };
}

function mapExecutionRuntime(value: WireExecutionRuntime) {
  return {
    language: value.language,
    interpreter: value.interpreter,
    arguments: value.arguments ?? [],
    runtimeDigest: value.runtime_digest,
    image: value.image,
    runnerProfileId: value.runner_profile_id,
    runnerProfileRevision: value.runner_profile_revision,
    runnerRuntime: value.runner_runtime,
    runnerIsolation: value.runner_isolation,
    runnerExecutable: value.runner_executable,
    runnerPlatform: value.runner_platform,
    runnerContext: value.runner_context ?? undefined,
    runnerSocket: value.runner_socket ?? undefined,
  };
}

function mapExecutionNetwork(value: WireExecutionNetwork) {
  return {
    mode: value.mode,
    target: value.target ?? undefined,
    ports: value.ports ?? [],
    resolvedAddresses: value.resolved_addresses ?? [],
    scopePolicyId: value.scope_policy_id ?? undefined,
    scopePolicyRevision: value.scope_policy_revision ?? undefined,
  };
}

function mapExecutionOrigin(value: WireExecutionOrigin) {
  return {
    kind: value.kind,
    messageId: value.message_id ?? undefined,
    blockOrdinal: value.block_ordinal ?? undefined,
    blockSha256: value.block_sha256 ?? undefined,
    selectionStartByte: value.selection_start_byte ?? undefined,
    selectionEndByte: value.selection_end_byte ?? undefined,
    executionId: value.execution_id ?? undefined,
    sourceKind: value.source_kind ?? undefined,
    sourceId: value.source_id ?? undefined,
    sourceLabel: value.source_label ?? undefined,
    sourceSha256: value.source_sha256 ?? undefined,
  };
}

function mapOperatorExecution(value: WireOperatorExecution): OperatorExecution {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    operatorId: value.operator_id,
    origin: mapExecutionOrigin(value.origin),
    language: value.language,
    sourceSha256: value.source_sha256,
    sourceArtifactId: value.source_artifact_id,
    sourcePreview: value.source_preview ?? "",
    runtime: mapExecutionRuntime(value.runtime),
    network: mapExecutionNetwork(value.network),
    limits: mapExecutionLimits(value.limits),
    workspace: value.workspace,
    policyDecision: value.policy_decision,
    status: value.status,
    errorCode: value.error_code ?? undefined,
    errorDetail: value.error_detail ?? undefined,
    queuedAt: value.queued_at,
    startedAt: value.started_at ?? undefined,
    completedAt: value.completed_at ?? undefined,
    exitCode: value.exit_code ?? undefined,
    outputTruncated: value.output_truncated === true,
    evidenceId: value.evidence_id ?? undefined,
    workspaceChanges: (value.workspace_changes ?? []).map((change) => ({
      path: change.path,
      change: change.change,
      size: change.size ?? undefined,
    })),
  };
}

function mapExecutionPreflight(
  value: WireExecutionPreflight,
): ExecutionPreflight {
  return {
    allowed: value.allowed,
    errorCode: value.error_code ?? undefined,
    detail: value.detail,
    canonicalLanguage: value.canonical_language ?? undefined,
    sourceSha256: value.source_sha256 ?? undefined,
    runtime: value.runtime ? mapExecutionRuntime(value.runtime) : undefined,
    network: value.network ? mapExecutionNetwork(value.network) : undefined,
    limits: mapExecutionLimits(value.limits),
    workspace: value.workspace,
    policyRule: value.policy_rule ?? undefined,
    previewFingerprint: value.preview_fingerprint ?? undefined,
    previewToken: value.preview_token ?? undefined,
    expiresAt: value.expires_at ?? undefined,
  };
}

function terminalBody(value: ContainerTerminalRequest): JsonObject {
  return {
    engagement_id: value.engagementId,
    columns: value.columns,
    rows: value.rows,
  };
}

function mapContainerTerminalRuntime(value: WireContainerTerminalRuntime) {
  return {
    sourceImage: value.source_image,
    baseImage: value.base_image,
    baseImageDigest: value.base_image_digest,
    image: value.image,
    imageDigest: value.image_digest,
    installedPackages: value.installed_packages,
    interpreter: value.interpreter,
    arguments: value.arguments,
    runnerProfileId: value.runner_profile_id,
    runnerProfileRevision: value.runner_profile_revision,
    runnerRuntime: value.runner_runtime,
    runnerIsolation: value.runner_isolation,
    runnerExecutable: value.runner_executable,
    runnerPlatform: value.runner_platform,
    runnerContext: value.runner_context ?? undefined,
  };
}

function mapContainerTerminalNetwork(value: WireContainerTerminalNetwork) {
  return {
    mode: value.mode,
    runtimeNetwork: value.runtime_network,
    publishedPorts: value.published_ports,
  };
}

function mapContainerTerminalSecurity(value: WireContainerTerminalSecurity) {
  return {
    containerUser: value.container_user,
    rootFilesystem: value.root_filesystem,
    linuxCapabilities: value.linux_capabilities,
    noNewPrivileges: value.no_new_privileges,
    hostNetwork: value.host_network,
    runtimeSocket: value.runtime_socket,
    hostShell: value.host_shell,
  };
}

function mapContainerTerminalPreflight(
  value: WireContainerTerminalPreflight,
): ContainerTerminalPreflight {
  return {
    allowed: value.allowed,
    errorCode: value.error_code ?? undefined,
    detail: value.detail,
    runtime: value.runtime
      ? mapContainerTerminalRuntime(value.runtime)
      : undefined,
    network: mapContainerTerminalNetwork(value.network),
    security: mapContainerTerminalSecurity(value.security),
    limits: mapExecutionLimits(value.limits),
    workspace: value.workspace,
    policyRule: value.policy_rule ?? undefined,
    previewFingerprint: value.preview_fingerprint ?? undefined,
    previewToken: value.preview_token ?? undefined,
    expiresAt: value.expires_at ?? undefined,
    idleTimeoutSeconds: value.idle_timeout_seconds,
    freshContainer: value.fresh_container,
  };
}

function mapContainerTerminalSession(
  value: WireContainerTerminalSession,
): ContainerTerminalSession {
  return {
    sessionId: value.session_id,
    createdAt: value.created_at,
    websocketTicket: value.websocket_ticket,
    ticketExpiresAt: value.ticket_expires_at,
    websocketPath: value.websocket_path,
    reconnectGraceSeconds: value.reconnect_grace_seconds,
    replayMaxBytes: value.replay_max_bytes,
    lastSequence: value.last_sequence,
  };
}

function executionBody(value: ExecutionRequest): JsonObject {
  return {
    engagement_id: value.engagementId,
    language: value.language,
    source: value.source,
    origin: {
      kind: value.origin.kind,
      message_id: value.origin.messageId,
      block_ordinal: value.origin.blockOrdinal,
      block_sha256: value.origin.blockSha256,
      selection_start_byte: value.origin.selectionStartByte,
      selection_end_byte: value.origin.selectionEndByte,
      execution_id: value.origin.executionId,
      source_kind: value.origin.sourceKind,
      source_id: value.origin.sourceId,
      source_label: value.origin.sourceLabel,
      source_sha256: value.origin.sourceSha256,
    },
    network: {
      mode: value.network.mode,
      target: value.network.target,
      ports: value.network.ports,
    },
  };
}

function mapWorkspaceListing(value: WireWorkspaceListing): WorkspaceListing {
  return {
    engagementId: value.engagement_id,
    path: value.path,
    entries: value.entries.map((entry) => ({
      path: entry.path,
      name: entry.name,
      kind: entry.kind,
      size: entry.size,
      modifiedAt: entry.modified_at,
    })),
    offset: value.offset,
    nextOffset: value.next_offset ?? undefined,
    total: value.total,
  };
}

function mapWorkspacePreview(value: WireWorkspacePreview): WorkspacePreview {
  return {
    engagementId: value.engagement_id,
    path: value.path,
    text: value.text,
    bytesReturned: value.bytes_returned,
    truncated: value.truncated,
    previewSha256: value.preview_sha256,
  };
}

function chatRequestBody(
  body: ChatCompletionRequest,
  stream: boolean,
): JsonObject {
  return {
    backend: body.backend ?? "provider",
    provider_id: body.providerId,
    harness_profile_id: body.harnessProfileId,
    harness_session_id: body.harnessSessionId,
    mcp_server_ids: body.mcpServerIds ?? [],
    engagement_id: body.engagementId,
    session_id: body.sessionId,
    model: body.model || undefined,
    messages: body.messages,
    context_attachments: (body.contextAttachments ?? []).map((item) => ({
      source_kind: item.sourceKind,
      source_id: item.sourceId,
      source_label: item.sourceLabel,
      text: item.text,
      sha256: item.sha256,
      truncated: item.truncated,
    })),
    max_output_tokens: body.maxOutputTokens,
    temperature: body.temperature,
    include_knowledge: body.includeKnowledge ?? true,
    allow_cloud_knowledge: body.allowCloudKnowledge ?? false,
    tools_enabled: body.toolsEnabled ?? false,
    max_artifact_queries: body.maxArtifactQueries ?? 20,
    allow_cloud_tool_results: body.allowCloudToolResults ?? false,
    stream,
  };
}

function mapChatSession(value: WireChatSession): ChatSessionSummary {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    title: value.title,
    backend: value.backend ?? "provider",
    providerId: value.provider_profile_id ?? undefined,
    harnessProfileId: value.harness_profile_id ?? undefined,
    harnessSessionId: value.harness_session_id ?? undefined,
    model: value.model ?? undefined,
    toolsEnabled: value.metadata?.tools_enabled === true,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    revision: value.revision,
  };
}

function mapHarnessProfile(value: WireHarnessProfile): HarnessProfile {
  return {
    id: value.id,
    name: value.name,
    kind: value.kind,
    connectionMode: value.connection_mode,
    transport: value.transport,
    executable: value.executable ?? undefined,
    endpoint: value.endpoint ?? undefined,
    authMode: value.auth_mode,
    secretRef: value.secret_ref ?? undefined,
    defaultModel: value.default_model ?? undefined,
    models: value.capabilities?.models ?? [],
    enabled: value.enabled,
    localOnly: value.privacy?.local_only === true,
    permitsSensitiveData: value.privacy?.permits_sensitive_data === true,
    nativeCapabilities: {
      workspaceAccess: value.native_capabilities?.workspace_access ?? "none",
      shell: value.native_capabilities?.shell === true,
      webSearch: value.native_capabilities?.web_search === true,
      webFetch: value.native_capabilities?.web_fetch === true,
      browser: value.native_capabilities?.browser === true,
      computerUse: value.native_capabilities?.computer_use === true,
      imageGeneration: value.native_capabilities?.image_generation === true,
      skills: value.native_capabilities?.skills === true,
      subagents: value.native_capabilities?.subagents === true,
    },
    healthy: Boolean(
      value.capabilities?.checked_at && !value.capabilities?.detail,
    ),
    version:
      value.capabilities?.harness_version ??
      value.capabilities?.protocol_version ??
      undefined,
    detail: value.capabilities?.detail ?? undefined,
    capabilities: {
      activityReplay: value.capabilities?.activity_replay === true,
      reasoningSummaries: value.capabilities?.reasoning_summaries === true,
      plans: value.capabilities?.plans === true,
      liveCommandOutput: value.capabilities?.live_command_output === true,
      fileDiffs: value.capabilities?.file_diffs === true,
      detailedUsage: value.capabilities?.detailed_usage === true,
      interactions: value.capabilities?.interactions === true,
      hooks: value.capabilities?.hooks === true,
      subagentActivity: value.capabilities?.subagent_activity === true,
      subagentControl: value.capabilities?.subagent_control === true,
      checkpointRewind: value.capabilities?.checkpoint_rewind === true,
      steering: value.capabilities?.steering === true,
      interruption: value.capabilities?.interruption === true,
    },
    revision: value.revision,
  };
}

function mapHarnessInteraction(
  value: WireHarnessInteraction,
): HarnessInteraction {
  return {
    id: value.id,
    harnessTurnId: value.harness_turn_id,
    status: value.status,
    kind: value.kind,
    prompt: value.prompt,
    questions: value.questions ?? [],
    responseSchema: value.response_schema ?? {},
    containsSecret: value.contains_secret === true,
    createdAt: value.created_at,
  };
}

function mapMcpServer(value: WireMcpServerProfile): McpServerProfile {
  return {
    id: value.id,
    name: value.name,
    transport: value.transport,
    command: value.command ?? undefined,
    arguments: value.arguments ?? [],
    url: value.url ?? undefined,
    authMode: value.auth_mode,
    enabled: value.enabled,
    required: value.required,
    trustedStdio: value.trusted_stdio,
    defaultApproval: value.default_approval,
    toolOverrides: value.tool_overrides ?? {},
    tools: (value.capabilities?.tools ?? []).map((tool) => ({
      name: tool.name,
      description: tool.description ?? "",
      readOnly: tool.read_only === true,
      destructive: tool.destructive !== false,
      openWorld: tool.open_world !== false,
      credentialed: tool.credentialed ?? undefined,
      approval: value.tool_overrides?.[tool.name] ?? value.default_approval,
    })),
    checkedAt: value.capabilities?.checked_at ?? undefined,
    detail: value.capabilities?.detail ?? undefined,
    revision: value.revision,
  };
}

function mapHarnessSession(value: WireHarnessSession): HarnessSessionSummary {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    harnessProfileId: value.harness_profile_id,
    model: value.model,
    status: value.status,
    mcpServerIds: value.mcp_server_ids ?? [],
    lastActivityAt: value.last_activity_at,
  };
}

function mapHarnessSessionActivity(
  value: WireHarnessSessionActivity,
): HarnessSessionActivity {
  return {
    sessionId: value.session_id,
    sessionStatus: value.session_status,
    busy: value.busy,
    live: value.live,
    turnId: value.turn_id ?? undefined,
    turnStatus: value.turn_status ?? undefined,
    turnOrigin: value.turn_origin ?? undefined,
    startedAt: value.started_at ?? undefined,
    lastActivityAt: value.last_activity_at,
    detail: value.detail,
  };
}

function mapChatTurn(value: WireChatTurn): ChatTurn {
  return {
    id: value.id,
    sessionId: value.session_id,
    status: value.status,
    approvalId: value.approval_id ?? undefined,
    harnessTurnId: value.harness_turn_id ?? undefined,
    toolCallIds: value.tool_call_ids ?? [],
  };
}

function mapHarnessDetailedUsage(
  value?: JsonObject,
): HarnessDetailedUsage | undefined {
  if (!value) return undefined;
  const inputTokens = numberField(value.input_tokens);
  const outputTokens = numberField(value.output_tokens);
  return {
    inputTokens,
    outputTokens,
    totalTokens: numberField(value.total_tokens) || inputTokens + outputTokens,
    cachedInputTokens: numberField(value.cached_input_tokens),
    cacheCreationTokens: numberField(value.cache_creation_input_tokens),
    cacheReadTokens: numberField(value.cache_read_input_tokens),
    reasoningTokens: numberField(value.reasoning_output_tokens),
    costUsd: numberField(value.cost_usd),
    durationMs:
      typeof value.duration_ms === "number" ? value.duration_ms : undefined,
    apiDurationMs:
      typeof value.duration_api_ms === "number"
        ? value.duration_api_ms
        : undefined,
    turnCount: numberField(value.num_turns),
    contextUsedTokens:
      typeof value.context_used === "number" ? value.context_used : undefined,
    contextLimitTokens:
      typeof value.context_window === "number"
        ? value.context_window
        : undefined,
    modelUsage:
      value.model_usage &&
      typeof value.model_usage === "object" &&
      !Array.isArray(value.model_usage)
        ? (value.model_usage as Record<string, Record<string, unknown>>)
        : {},
    rateLimit:
      value.rate_limit &&
      typeof value.rate_limit === "object" &&
      !Array.isArray(value.rate_limit)
        ? (value.rate_limit as Record<string, unknown>)
        : {},
  };
}

function mapHarnessActivityEvent(
  value: WireChatStreamEvent,
): HarnessActivityEvent {
  const inputTokens = numberField(value.usage?.input_tokens);
  const outputTokens = numberField(value.usage?.output_tokens);
  return {
    schemaVersion: value.schema_version ?? "nebula.harness-activity/v1",
    id: value.id,
    sequence: value.sequence,
    type: value.type,
    vendor: value.vendor,
    harnessSessionId: value.harness_session_id,
    harnessTurnId: value.harness_turn_id,
    externalSessionId: value.external_session_id,
    externalTurnId: value.external_turn_id,
    itemId: value.item_id,
    parentItemId: value.parent_item_id,
    itemKind: value.item_kind,
    itemStatus: value.item_status,
    title: value.title,
    summary: value.summary,
    stream: value.stream,
    delta: value.delta,
    message: typeof value.message === "string" ? value.message : undefined,
    usage: value.usage
      ? {
          inputTokens,
          outputTokens,
          totalTokens:
            numberField(value.usage.total_tokens) || inputTokens + outputTokens,
        }
      : undefined,
    detailedUsage: mapHarnessDetailedUsage(value.detailed_usage),
    artifactIds: value.artifact_ids ?? [],
    payload: value.payload ?? {},
    occurredAt: value.occurred_at,
  };
}

function mapPersistedChatMessage(
  value: WirePersistedChatMessage,
): PersistedChatMessage {
  const inputTokens = numberField(value.usage?.input_tokens);
  const outputTokens = numberField(value.usage?.output_tokens);
  return {
    id: value.id,
    engagementId: value.engagement_id,
    sessionId: value.session_id,
    sequence: value.sequence,
    role: value.role,
    content: value.content,
    providerId: value.provider_profile_id ?? undefined,
    model: value.model ?? undefined,
    usage: value.usage
      ? {
          inputTokens,
          outputTokens,
          totalTokens:
            typeof value.usage.total_tokens === "number"
              ? value.usage.total_tokens
              : inputTokens + outputTokens,
        }
      : undefined,
    finishReason: value.finish_reason ?? undefined,
    providerRequestId: value.provider_request_id ?? undefined,
    citations: (value.citations ?? []).map(mapChatCitation),
    contextAttachments: Array.isArray(value.metadata?.context_attachments)
      ? value.metadata.context_attachments.flatMap((item) => {
          if (!item || typeof item !== "object" || Array.isArray(item))
            return [];
          const row = item as JsonObject;
          if (
            typeof row.source_kind !== "string" ||
            typeof row.source_label !== "string" ||
            typeof row.text !== "string" ||
            typeof row.sha256 !== "string"
          )
            return [];
          return [
            {
              sourceKind: row.source_kind,
              sourceId:
                typeof row.source_id === "string" ? row.source_id : undefined,
              sourceLabel: row.source_label,
              text: row.text,
              sha256: row.sha256,
              truncated: row.truncated === true,
            },
          ];
        })
      : [],
    harnessTurnId:
      typeof value.metadata?.harness_turn_id === "string"
        ? value.metadata.harness_turn_id
        : undefined,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

function wireItems<T>(value: T[] | { items?: T[]; entries?: T[] }): T[] {
  return Array.isArray(value) ? value : (value.items ?? value.entries ?? []);
}

function mapRunnerProfile(value: WireRunnerProfile): RunnerProfile {
  return {
    id: value.id,
    name: value.name,
    runtimeType: value.runtime_type ?? value.runtime ?? "podman",
    executable: value.executable,
    context: value.context ?? undefined,
    socket: value.socket ?? undefined,
    platform: value.platform ?? "unknown",
    isolationMode: value.isolation_mode ?? value.isolation ?? "unverified",
    state:
      value.state ??
      (value.healthy
        ? "ready"
        : value.enabled === false
          ? "unavailable"
          : value.last_health_at || value.last_checked_at
            ? "degraded"
            : "unchecked"),
    lastCheckedAt: value.last_checked_at ?? value.last_health_at ?? undefined,
    detail: value.detail ?? value.last_health_detail ?? undefined,
    seccompProfile: value.seccomp_profile ?? undefined,
    revision: numberField(value.revision),
  };
}

function mapEngagementScope(value: WireEngagementScope): EngagementScopePolicy {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    allowedCidrs: value.allowed_cidrs ?? [],
    allowedDomains: value.allowed_domains ?? [],
    allowedUrls: value.allowed_urls ?? [],
    allowedPorts: value.allowed_ports ?? [],
    notBefore: value.not_before ?? undefined,
    notAfter: value.not_after ?? undefined,
    prohibitedActions: value.prohibited_actions ?? [],
    localOnly: value.local_only !== false,
    maxConcurrency: numberField(value.max_concurrency) || 1,
    grants: (value.grants ?? []).map((grant) => ({
      riskClasses: grant.risk_classes ?? [],
      toolNames: grant.tool_names ?? [],
      targets: grant.targets ?? [],
      grantedAt: grant.granted_at ?? "",
      expiresAt: grant.expires_at ?? "",
      grantedBy: grant.granted_by ?? "",
    })),
    revision: numberField(value.revision),
  };
}

function mapScopeImport(value: WireScopeImport): ScopeImport {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    artifactId: value.artifact_id,
    filename: value.filename,
    sourceType: value.source_type,
    sourceSha256: value.source_sha256,
    baseScopeRevision: numberField(value.base_scope_revision),
    status: value.status,
    candidates: (value.candidates ?? []).map((candidate) => ({
      id: candidate.id,
      targetType: candidate.target_type,
      classification: candidate.classification,
      rawValue: candidate.raw_value,
      normalizedValue: candidate.normalized_value ?? undefined,
      sourceLocation: candidate.source_location ?? "document",
      sourceExcerpt: candidate.source_excerpt ?? "",
      warnings: candidate.warnings ?? [],
    })),
    warnings: value.warnings ?? [],
    provenance: value.provenance
      ? {
          providerProfileId: value.provenance.provider_profile_id,
          model: value.provenance.model,
          promptVersion: value.provenance.prompt_version,
          sourceSha256: value.provenance.source_sha256,
          generatedAt: value.provenance.generated_at,
          providerRequestIds: value.provenance.provider_request_ids ?? [],
        }
      : undefined,
    usage: {
      inputTokens: numberField(value.usage?.input_tokens),
      outputTokens: numberField(value.usage?.output_tokens),
      totalTokens: numberField(value.usage?.total_tokens),
    },
    errorDetail: value.error_detail ?? undefined,
    appliedCandidateIds: value.applied_candidate_ids ?? [],
    appliedScopePolicyId: value.applied_scope_policy_id ?? undefined,
    appliedScopeRevision: value.applied_scope_revision ?? undefined,
    revision: numberField(value.revision),
  };
}

async function responseError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let details: unknown = text;
  if (text) {
    try {
      details = JSON.parse(text);
    } catch (caughtError) {
      void logCaughtDiagnostic(
        "interface.client.caught_failure_01",
        "A handled interface operation failed.",
        caughtError,
        "client",
      );
      // Preserve a non-JSON Core/proxy response verbatim.
    }
  }
  const message =
    typeof details === "object" && details && "message" in details
      ? String(details.message)
      : typeof details === "object" && details && "detail" in details
        ? typeof details.detail === "string"
          ? details.detail
          : JSON.stringify(details.detail)
        : text || `Nebula API request failed (${response.status})`;
  return new ApiError(
    message,
    response.status,
    response.headers.get("x-request-id") ?? undefined,
    details,
  );
}

function mapSetupStatus(value: WireSetupStatus): SetupStatus {
  return {
    applicationStage: value.application_stage ?? "ready",
    stageDetail: value.stage_detail ?? "Nebula is ready.",
    stageStartedAt: value.stage_started_at ?? undefined,
    retryable: value.retryable ?? false,
    recoveryActions: (value.recovery_actions ?? []).map((action) => ({
      id: action.id,
      label: action.label,
      destination: action.destination ?? undefined,
    })),
    core: {
      status: value.core.status,
      detail: value.core.detail ?? undefined,
    },
    scratchProjectId: value.scratch_project_id ?? undefined,
    terminal: {
      status: value.terminal.status,
      runnerProfileId: value.terminal.runner_profile_id ?? undefined,
      candidates: (value.terminal.candidates ?? []).map((candidate) => ({
        candidateId: candidate.candidate_id ?? undefined,
        runnerProfileId: candidate.runner_profile_id ?? undefined,
        source: candidate.source,
        name: candidate.name,
        runtime: candidate.runtime,
        executable: candidate.executable,
        context: candidate.context ?? undefined,
        platform: candidate.platform,
        isolation: candidate.isolation,
        healthy: candidate.healthy,
        detail: candidate.detail ?? undefined,
      })),
      imagePreparation: {
        phase: value.terminal.image_preparation?.phase ?? "not_started",
        operationId:
          value.terminal.image_preparation?.operation_id ?? undefined,
        projectId: value.terminal.image_preparation?.project_id ?? undefined,
        progressPercent:
          value.terminal.image_preparation?.progress_percent ?? undefined,
        progressIndeterminate:
          value.terminal.image_preparation?.progress_indeterminate ?? false,
        canCancel: value.terminal.image_preparation?.can_cancel ?? false,
        canRetry: value.terminal.image_preparation?.can_retry ?? false,
        imageDigest:
          value.terminal.image_preparation?.image_digest ?? undefined,
        startedAt: value.terminal.image_preparation?.started_at ?? undefined,
        completedAt:
          value.terminal.image_preparation?.completed_at ?? undefined,
        detail: value.terminal.image_preparation?.detail ?? undefined,
      },
      detail: value.terminal.detail ?? undefined,
    },
    assistant: {
      status: value.assistant.status,
      providerProfileId: value.assistant.provider_profile_id ?? undefined,
      detail: value.assistant.detail ?? undefined,
    },
  };
}

function mapSetupControlResponse(
  value: WireSetupControlResponse,
): SetupControlResponse {
  return {
    operation: value.operation,
    accepted: value.accepted,
    idempotent: value.idempotent,
    operationId: value.operation_id ?? undefined,
    setup: mapSetupStatus(value.setup),
  };
}

export class ApiClient {
  readonly baseUrl: string;
  private readonly tokenSource?: ApiClientOptions["token"];
  private readonly fetchImpl: typeof globalThis.fetch;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = normalizeBaseUrl(options.baseUrl);
    this.tokenSource = options.token;
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  getToken(): string | undefined {
    return typeof this.tokenSource === "function"
      ? this.tokenSource()
      : this.tokenSource;
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = this.getToken();
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    if (!headers.has("X-Nebula-Operation-ID")) {
      headers.set("X-Nebula-Operation-ID", newOperationId());
    }

    let response: Response;
    try {
      response = await this.fetchImpl(
        `${this.baseUrl}/${path.replace(/^\//, "")}`,
        {
          ...init,
          headers,
          credentials: "same-origin",
        },
      );
    } catch (error) {
      void logDiagnostic({
        level: "error",
        eventCode: "interface.api.transport_failed",
        message: "The interface could not reach Nebula Core.",
        outcome: "failure",
        stage: "request",
        retryable: true,
        safeFailureCause: "The local API transport was unavailable.",
        exception: error,
        metadata: { method: init.method ?? "GET" },
      });
      throw error;
    }

    if (!response.ok) {
      const error = await responseError(response);
      void logDiagnostic({
        level: response.status >= 500 ? "error" : "warning",
        eventCode: "interface.api.request_failed",
        message: "A user interface API action could not complete.",
        outcome: response.status >= 500 ? "failure" : "denied",
        stage: "response",
        retryable: error.retryable,
        safeFailureCause:
          response.status >= 500
            ? "Nebula Core reported an operation failure."
            : "Nebula Core rejected the request safely.",
        exception: error,
        requestId: error.requestId,
        errorId: error.errorId,
        metadata: {
          method: init.method ?? "GET",
          http_status: response.status,
          code: error.code,
        },
      });
      throw error;
    }

    if (response.status === 204) {
      return undefined as T;
    }
    try {
      return (await response.json()) as T;
    } catch (error) {
      void logDiagnostic({
        level: "error",
        eventCode: "interface.api.response_parse_failed",
        message: "The interface could not parse a Nebula Core response.",
        outcome: "failure",
        stage: "response-parse",
        retryable: true,
        exception: error,
        metadata: {
          method: init.method ?? "GET",
          http_status: response.status,
        },
      });
      throw error;
    }
  }

  diagnosticsSettings(signal?: AbortSignal): Promise<DiagnosticSettings> {
    return this.request<DiagnosticSettings>("diagnostics/settings", { signal });
  }

  updateDiagnosticsSettings(
    settings: DiagnosticSettings,
    signal?: AbortSignal,
  ): Promise<DiagnosticSettings> {
    return this.request<DiagnosticSettings>("diagnostics/settings", {
      method: "PUT",
      body: JSON.stringify(settings),
      signal,
    });
  }

  diagnosticsFiles(
    signal?: AbortSignal,
  ): Promise<{ files: DiagnosticFile[]; health: DiagnosticStatus }> {
    return this.request<{ files: DiagnosticFile[]; health: DiagnosticStatus }>(
      "diagnostics/files",
      { signal },
    );
  }

  diagnosticErrors(
    feature?: string,
    after?: string,
    limit = 100,
    signal?: AbortSignal,
  ): Promise<DiagnosticRecord[]> {
    const parameters = new URLSearchParams({ limit: String(limit) });
    if (feature) parameters.set("feature", feature);
    if (after) parameters.set("after", after);
    return this.request<{ errors: DiagnosticRecord[] }>(
      `diagnostics/errors?${parameters}`,
      { signal },
    ).then((result) => result.errors);
  }

  resolveDiagnosticIncidents(
    records: DiagnosticRecord[],
    signal?: AbortSignal,
  ): Promise<DiagnosticIncident[]> {
    return this.request<DiagnosticIncident[]>("diagnostics/incidents/resolve", {
      method: "POST",
      body: JSON.stringify({ records }),
      signal,
    });
  }

  diagnosticIncident(
    errorId: string,
    signal?: AbortSignal,
  ): Promise<DiagnosticIncident> {
    return this.request<DiagnosticIncident>(
      `diagnostics/incidents/${encodeURIComponent(errorId)}`,
      { signal },
    );
  }

  runDiagnosticAction(
    errorId: string,
    actionId: string,
    confirmed: boolean,
    signal?: AbortSignal,
  ): Promise<DiagnosticActionResult> {
    return this.request<DiagnosticActionResult>(
      `diagnostics/incidents/${encodeURIComponent(errorId)}/actions/${encodeURIComponent(actionId)}`,
      {
        method: "POST",
        body: JSON.stringify({ confirmed, operator_id: "local-operator" }),
        signal,
      },
    );
  }

  diagnosticSensitiveDetail(
    errorId: string,
    action: "reveal" | "copy",
    signal?: AbortSignal,
  ): Promise<{ error_id: string; action: "reveal" | "copy"; detail: string }> {
    return this.request(
      `diagnostics/incidents/${encodeURIComponent(errorId)}/sensitive-detail`,
      {
        method: "POST",
        body: JSON.stringify({
          confirmed: true,
          action,
          operator_id: "local-operator",
        }),
        signal,
      },
    );
  }

  async exportDiagnostics(signal?: AbortSignal): Promise<Blob> {
    const token = this.getToken();
    const headers = new Headers({
      Accept: "application/zip",
      "X-Nebula-Operation-ID": newOperationId(),
    });
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      `${this.baseUrl}/diagnostics/export`,
      {
        method: "POST",
        headers,
        credentials: "same-origin",
        signal,
      },
    );
    if (!response.ok) throw await responseError(response);
    return response.blob();
  }

  private async listAll<T>(
    resource: string,
    signal?: AbortSignal,
    engagementId?: string,
  ): Promise<T[]> {
    const items: T[] = [];
    let offset = 0;
    while (true) {
      const path = engagementId
        ? `${resource}?${engagementQuery(engagementId, offset)}`
        : globalListPath(resource, offset);
      const batch = await this.request<T[]>(path, { signal });
      items.push(...batch);
      if (batch.length < MAX_LIST_LIMIT) return items;
      offset += batch.length;
    }
  }

  health(signal?: AbortSignal): Promise<HealthResponse> {
    return this.request<
      Partial<HealthResponse> & {
        api_version?: string;
        dialect?: string;
        container_terminal?: "configured" | "unavailable";
        diagnostics?: { degraded?: boolean };
      }
    >("health", { signal }).then((health) => ({
      status: health.status === "degraded" ? "degraded" : "ok",
      version: health.version ?? health.api_version ?? "unknown",
      mode:
        health.mode ??
        (health.dialect?.startsWith("postgres") ? "team" : "local"),
      runner: health.runner ?? "unavailable",
      containerTerminal: health.container_terminal ?? "unavailable",
      diagnosticsDegraded: health.diagnostics?.degraded === true,
    }));
  }

  setupStatus(signal?: AbortSignal): Promise<SetupStatus> {
    return this.request<WireSetupStatus>("setup/status", { signal }).then(
      mapSetupStatus,
    );
  }

  refreshSetupRuntime(signal?: AbortSignal): Promise<SetupStatus> {
    return this.request<WireSetupStatus>("setup/runtime/refresh", {
      method: "POST",
      signal,
    }).then(mapSetupStatus);
  }

  selectSetupRuntime(
    candidateId: string,
    signal?: AbortSignal,
  ): Promise<SetupControlResponse> {
    return this.request<WireSetupControlResponse>("setup/runtime/select", {
      method: "POST",
      body: JSON.stringify({ candidate_id: candidateId }),
      signal,
    }).then(mapSetupControlResponse);
  }

  prepareSetupImage(
    projectId?: string,
    signal?: AbortSignal,
  ): Promise<SetupControlResponse> {
    return this.setupImageOperation("prepare", projectId, signal);
  }

  retrySetupImage(
    projectId?: string,
    signal?: AbortSignal,
  ): Promise<SetupControlResponse> {
    return this.setupImageOperation("retry", projectId, signal);
  }

  cancelSetupImage(
    operationId: string,
    signal?: AbortSignal,
  ): Promise<SetupControlResponse> {
    return this.request<WireSetupControlResponse>("setup/image/cancel", {
      method: "POST",
      body: JSON.stringify({ operation_id: operationId }),
      signal,
    }).then(mapSetupControlResponse);
  }

  private setupImageOperation(
    operation: "prepare" | "retry",
    projectId?: string,
    signal?: AbortSignal,
  ): Promise<SetupControlResponse> {
    return this.request<WireSetupControlResponse>(`setup/image/${operation}`, {
      method: "POST",
      body: JSON.stringify(projectId ? { project_id: projectId } : {}),
      signal,
    }).then(mapSetupControlResponse);
  }

  createCredential(
    secret: string,
    persistence: "vault" | "session" = "vault",
  ): Promise<CredentialStatus> {
    return this.request<{
      reference: string;
      persistence: CredentialStatus["persistence"];
      available: boolean;
    }>("credentials", {
      method: "POST",
      body: JSON.stringify({ secret, persistence }),
    });
  }

  credentialStatus(
    reference: string,
    signal?: AbortSignal,
  ): Promise<CredentialStatus> {
    return this.request<CredentialStatus>(
      `credentials/${encodeURIComponent(reference)}/status`,
      { signal },
    );
  }

  async deleteCredential(reference: string): Promise<void> {
    await this.request<void>(`credentials/${encodeURIComponent(reference)}`, {
      method: "DELETE",
    });
  }

  listEngagements(signal?: AbortSignal): Promise<Page<EngagementSummary>> {
    return this.listAll<WireEngagement>("engagements", signal).then((items) =>
      page(items.map(mapEngagement)),
    );
  }

  createEngagement(body: EngagementCreateRequest): Promise<EngagementSummary> {
    return this.request<WireEngagement>("engagements", {
      method: "POST",
      body: JSON.stringify({
        name: body.name.trim(),
        description: body.description ?? "",
        client_name: body.clientName || null,
        status: body.status ?? "draft",
        tags: body.tags ?? [],
        metadata: {},
      }),
    }).then(mapEngagement);
  }

  listOperatorProfiles(signal?: AbortSignal): Promise<OperatorProfile[]> {
    return this.request<WireOperatorProfile[]>("operator-profiles", {
      signal,
    }).then((items) => items.map(mapOperatorProfile));
  }

  getActiveOperatorProfile(signal?: AbortSignal): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>("operator-profiles/active", {
      signal,
    }).then(mapOperatorProfile);
  }

  createOperatorProfile(
    body: OperatorProfileCreateRequest,
  ): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>("operator-profiles", {
      method: "POST",
      body: JSON.stringify({
        display_name: body.displayName,
        email: body.email || null,
        role: body.role || null,
        metadata: body.metadata ?? {},
      }),
    }).then(mapOperatorProfile);
  }

  updateOperatorProfile(
    id: string,
    body: OperatorProfileUpdateRequest,
  ): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>(
      `operator-profiles/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          ...(body.displayName === undefined
            ? {}
            : { display_name: body.displayName }),
          ...(body.email === undefined ? {} : { email: body.email || null }),
          ...(body.role === undefined ? {} : { role: body.role || null }),
          ...(body.metadata === undefined ? {} : { metadata: body.metadata }),
          expected_revision: body.expectedRevision,
        }),
      },
    ).then(mapOperatorProfile);
  }

  activateOperatorProfile(
    id: string,
    expectedRevision?: number,
  ): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>(
      `operator-profiles/${encodeURIComponent(id)}/activate`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
    ).then(mapOperatorProfile);
  }

  async deleteOperatorProfile(
    id: string,
    expectedRevision?: number,
  ): Promise<void> {
    const headers = new Headers();
    if (expectedRevision !== undefined)
      headers.set("If-Match", String(expectedRevision));
    await this.request<void>(`operator-profiles/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers,
    });
  }

  listRuns(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<AgentRunSummary>> {
    return this.listAll<WireAgentRun>("runs", signal, engagementId).then(
      (items) => page(items.map(mapRun)),
    );
  }

  createMission(body: MissionCreateRequest): Promise<AgentRunSummary> {
    return this.request<WireAgentRun>("missions", {
      method: "POST",
      body: JSON.stringify({
        engagement_id: body.engagementId,
        objective: body.objective,
        backend: body.backend ?? "native",
        provider_id: body.providerId,
        harness_profile_id: body.harnessProfileId,
        harness_session_id: body.harnessSessionId,
        mcp_server_ids: body.mcpServerIds ?? [],
        model: body.model,
        max_duration_seconds: body.maxDurationSeconds,
        max_tokens: body.maxTokens,
        max_cost_usd: body.maxCostUsd,
        max_retries: body.maxRetries,
        max_tool_calls: body.maxToolCalls ?? 0,
        max_artifact_queries: body.maxArtifactQueries ?? 200,
        max_concurrency: body.maxConcurrency ?? 1,
        allow_cloud_tool_results: body.allowCloudToolResults === true,
      }),
    }).then(mapRun);
  }

  steerRun(id: string, text: string): Promise<void> {
    return this.request<void>(`runs/${encodeURIComponent(id)}/steer`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
  }

  discussRun(id: string): Promise<ChatSessionSummary> {
    return this.request<WireChatSession>(
      `runs/${encodeURIComponent(id)}/discuss`,
      {
        method: "POST",
      },
    ).then(mapChatSession);
  }

  continueChatAsMission(
    sessionId: string,
    body: {
      objective?: string;
      maxDurationSeconds?: number;
      maxTokens?: number;
      maxCostUsd?: number;
      maxToolCalls?: number;
      allowCloudToolResults?: boolean;
    } = {},
  ): Promise<AgentRunSummary> {
    return this.request<WireAgentRun>(
      `chat/sessions/${encodeURIComponent(sessionId)}/continue-as-mission`,
      {
        method: "POST",
        body: JSON.stringify({
          objective: body.objective,
          max_duration_seconds: body.maxDurationSeconds,
          max_tokens: body.maxTokens,
          max_cost_usd: body.maxCostUsd,
          max_tool_calls: body.maxToolCalls,
          allow_cloud_tool_results: body.allowCloudToolResults === true,
        }),
      },
    ).then(mapRun);
  }

  listHarnesses(signal?: AbortSignal): Promise<HarnessProfile[]> {
    return this.listAll<WireHarnessProfile>("harnesses", signal).then((items) =>
      items.map(mapHarnessProfile),
    );
  }

  createHarness(body: Record<string, unknown>): Promise<HarnessProfile> {
    return this.request<WireHarnessProfile>("harnesses", {
      method: "POST",
      body: JSON.stringify(body),
    }).then(mapHarnessProfile);
  }

  updateHarness(
    id: string,
    changes: Record<string, unknown>,
    expectedRevision: number,
  ): Promise<HarnessProfile> {
    return this.request<WireHarnessProfile>(
      `harnesses/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ changes, expected_revision: expectedRevision }),
      },
    ).then(mapHarnessProfile);
  }

  checkHarness(id: string): Promise<HarnessProfile> {
    return this.request<Record<string, unknown>>(
      `harnesses/${encodeURIComponent(id)}/health`,
      {
        method: "POST",
      },
    )
      .then(() =>
        this.request<WireHarnessProfile>(`harnesses/${encodeURIComponent(id)}`),
      )
      .then(mapHarnessProfile);
  }

  async deleteHarness(id: string, expectedRevision: number): Promise<void> {
    await this.request<void>(`harnesses/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "If-Match": String(expectedRevision) },
    });
  }

  listHarnessSessions(
    engagementId?: string,
    signal?: AbortSignal,
  ): Promise<HarnessSessionSummary[]> {
    return this.listAll<WireHarnessSession>(
      "harness-sessions",
      signal,
      engagementId,
    ).then((items) => items.map(mapHarnessSession));
  }

  getHarnessSessionActivity(
    id: string,
    signal?: AbortSignal,
  ): Promise<HarnessSessionActivity> {
    return this.request<WireHarnessSessionActivity>(
      `harness-sessions/${encodeURIComponent(id)}/activity`,
      { signal },
    ).then(mapHarnessSessionActivity);
  }

  getHarnessTurnEvents(
    id: string,
    after = 0,
    signal?: AbortSignal,
  ): Promise<HarnessActivityEventPage> {
    return this.request<WireHarnessActivityEventPage>(
      `harness-turns/${encodeURIComponent(id)}/events?after=${after}&limit=10000`,
      { signal },
    ).then((value) => ({
      events: value.events.map(mapHarnessActivityEvent),
      nextSequence: value.next_sequence,
    }));
  }

  getHarnessTurn(id: string, signal?: AbortSignal): Promise<HarnessTurnDetail> {
    return this.request<WireHarnessTurn>(
      `harness-turns/${encodeURIComponent(id)}`,
      { signal },
    ).then((value) => ({
      id: value.id,
      status: value.status,
      origin: value.origin,
      harnessSessionId: value.harness_session_id,
      chatSessionId: value.chat_session_id ?? undefined,
      runId: value.run_id ?? undefined,
      error: value.error ?? undefined,
      retryOfTurnId:
        typeof value.metadata?.retry_of_turn_id === "string"
          ? value.metadata.retry_of_turn_id
          : undefined,
    }));
  }

  followHarnessTurnEvents(
    id: string,
    after: number,
    onEvent: (event: HarnessActivityEvent) => void,
    onComplete?: () => void,
    onError?: (error: Error) => void,
  ): () => void {
    const endpoint = new URL(
      `${this.baseUrl.replace(/\/$/, "")}/harness-turns/${encodeURIComponent(id)}/events/ws`,
      globalThis.location?.origin ?? "http://127.0.0.1",
    );
    endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
    endpoint.searchParams.set("after", String(after));
    const protocols = ["nebula.harness-activity.v1"];
    const token = this.getToken();
    if (token) protocols.push(websocketAuthProtocol(token));
    const socket = new WebSocket(endpoint, protocols);
    socket.addEventListener("message", (message) => {
      try {
        const frame = JSON.parse(String(message.data)) as {
          kind?: string;
          event?: WireChatStreamEvent;
        };
        if (frame.kind === "event" && frame.event) {
          onEvent(mapHarnessActivityEvent(frame.event));
        } else if (frame.kind === "complete") {
          onComplete?.();
        }
      } catch (error) {
        void logCaughtDiagnostic(
          "interface.client.harness_activity_frame",
          "A harness activity frame could not be decoded.",
          error,
          "client",
        );
        onError?.(
          error instanceof Error
            ? error
            : new Error("Malformed harness activity frame"),
        );
      }
    });
    socket.addEventListener("error", () =>
      onError?.(new Error("Harness activity connection failed")),
    );
    return () => socket.close(1000, "viewer detached");
  }

  listHarnessInteractions(
    id: string,
    signal?: AbortSignal,
  ): Promise<HarnessInteraction[]> {
    return this.request<WireHarnessInteraction[]>(
      `harness-turns/${encodeURIComponent(id)}/interactions`,
      { signal },
    ).then((items) => items.map(mapHarnessInteraction));
  }

  decideHarnessInteraction(
    id: string,
    action: "answer" | "decline" | "cancel",
    response: Record<string, unknown> = {},
  ): Promise<HarnessInteraction> {
    return this.request<WireHarnessInteraction>(
      `harness-interactions/${encodeURIComponent(id)}/decision`,
      { method: "POST", body: JSON.stringify({ action, response }) },
    ).then(mapHarnessInteraction);
  }

  stopHarnessTurn(id: string, reason = "Stopped by operator"): Promise<void> {
    return this.request(`harness-turns/${encodeURIComponent(id)}/stop`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }).then(() => undefined);
  }

  retryHarnessTurn(id: string): Promise<void> {
    return this.request(`harness-turns/${encodeURIComponent(id)}/retry`, {
      method: "POST",
    }).then(() => undefined);
  }

  steerHarnessTurn(id: string, text: string): Promise<void> {
    return this.request(`harness-turns/${encodeURIComponent(id)}/steer`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }).then(() => undefined);
  }

  stopHarnessSubagent(turnId: string, taskId: string): Promise<void> {
    return this.request(
      `harness-turns/${encodeURIComponent(turnId)}/tasks/${encodeURIComponent(taskId)}/stop`,
      { method: "POST" },
    ).then(() => undefined);
  }

  rewindHarnessCheckpoint(
    sessionId: string,
    checkpointId: string,
  ): Promise<void> {
    return this.request(
      `harness-sessions/${encodeURIComponent(sessionId)}/checkpoints/rewind`,
      { method: "POST", body: JSON.stringify({ checkpoint_id: checkpointId }) },
    ).then(() => undefined);
  }

  closeHarnessSession(id: string): Promise<HarnessSessionSummary> {
    return this.request<WireHarnessSession>(
      `harness-sessions/${encodeURIComponent(id)}/close`,
      {
        method: "POST",
      },
    ).then(mapHarnessSession);
  }

  getAutomationRuntime(signal?: AbortSignal): Promise<import("./types").AutomationRuntimeInfo> {
    return this.request<Record<string, unknown>>("automation/runtime", { signal }).then((value) => ({
      configured: value.configured === true,
      ready: value.ready === true,
      image: typeof value.image === "string" ? value.image : undefined,
      digest: typeof value.digest === "string" ? value.digest : undefined,
      runnerProfileId: typeof value.runner_profile_id === "string" ? value.runner_profile_id : undefined,
      detail: typeof value.detail === "string" ? value.detail : "Runtime status unavailable",
      inventory: Array.isArray(value.inventory) ? value.inventory.flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const entry = item as Record<string, unknown>;
        return typeof entry.name === "string" && typeof entry.version === "string" && typeof entry.path === "string"
          ? [{ name: entry.name, version: entry.version, path: entry.path }]
          : [];
      }) : [],
    }));
  }

  prepareAutomationRuntime(): Promise<import("./types").AutomationRuntimeInfo> {
    return this.request<Record<string, unknown>>("automation/runtime/prepare", { method: "POST" })
      .then(() => this.getAutomationRuntime());
  }

  getAutomationPolicy(engagementId: string): Promise<import("./types").AutomationProjectPolicy> {
    return this.request<Record<string, unknown>>(`engagements/${encodeURIComponent(engagementId)}/automation-policy`).then((value) => ({
      id: String(value.id),
      engagementId: String(value.engagement_id),
      approvalPolicy: value.approval_policy as "always" | "on_boundary" | "never",
      networkEnabled: value.network_enabled === true,
      runnerProfileId: typeof value.runner_profile_id === "string" ? value.runner_profile_id : undefined,
      maxTimeoutMs: Number(value.max_timeout_ms ?? 300000),
      revision: Number(value.revision ?? 1),
    }));
  }

  updateAutomationPolicy(
    engagementId: string,
    request: { approvalPolicy: "always" | "on_boundary" | "never"; networkEnabled: boolean; runnerProfileId?: string; maxTimeoutMs: number; expectedRevision: number },
  ): Promise<import("./types").AutomationProjectPolicy> {
    return this.request<Record<string, unknown>>(`engagements/${encodeURIComponent(engagementId)}/automation-policy`, {
      method: "PUT",
      body: JSON.stringify({
        approval_policy: request.approvalPolicy,
        network_enabled: request.networkEnabled,
        runner_profile_id: request.runnerProfileId ?? null,
        max_timeout_ms: request.maxTimeoutMs,
        expected_revision: request.expectedRevision,
      }),
    }).then(() => this.getAutomationPolicy(engagementId));
  }

  listMcpServers(signal?: AbortSignal): Promise<McpServerProfile[]> {
    return this.listAll<WireMcpServerProfile>("mcp-servers", signal).then(
      (items) => items.map(mapMcpServer),
    );
  }

  createMcpServer(body: Record<string, unknown>): Promise<McpServerProfile> {
    return this.request<WireMcpServerProfile>("mcp-servers", {
      method: "POST",
      body: JSON.stringify(body),
    }).then(mapMcpServer);
  }

  updateMcpServer(
    id: string,
    changes: Record<string, unknown>,
    expectedRevision: number,
  ): Promise<McpServerProfile> {
    return this.request<WireMcpServerProfile>(
      `mcp-servers/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({ changes, expected_revision: expectedRevision }),
      },
    ).then(mapMcpServer);
  }

  probeMcpServer(id: string, engagementId?: string): Promise<McpServerProfile> {
    return this.request<Record<string, unknown>>(
      `mcp-servers/${encodeURIComponent(id)}/probe`,
      {
        method: "POST",
        body: JSON.stringify({ engagement_id: engagementId }),
      },
    )
      .then(() =>
        this.request<WireMcpServerProfile>(
          `mcp-servers/${encodeURIComponent(id)}`,
        ),
      )
      .then(mapMcpServer);
  }

  async deleteMcpServer(id: string, expectedRevision: number): Promise<void> {
    await this.request<void>(`mcp-servers/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "If-Match": String(expectedRevision) },
    });
  }

  listToolCallArtifacts(toolCallId: string): Promise<ToolArtifactReference[]> {
    return this.request<
      Array<{
        id: string;
        sha256: string;
        size: number;
        filename?: string | null;
        media_type: string;
        metadata?: JsonObject;
      }>
    >(`tool-calls/${encodeURIComponent(toolCallId)}/artifacts`).then(
      (artifacts) =>
        artifacts.map((artifact) => {
          const metadata = artifact.metadata ?? {};
          return {
            artifactId: artifact.id,
            kind: String(
              metadata.kind ?? "generated_file",
            ) as ToolArtifactReference["kind"],
            filename: artifact.filename ?? undefined,
            mediaType: artifact.media_type,
            byteCount: artifact.size,
            observedByteCount:
              typeof metadata.observed_byte_count === "number"
                ? metadata.observed_byte_count
                : artifact.size,
            sha256: artifact.sha256,
            searchable: metadata.searchable === true,
            truncated: metadata.truncated === true,
          };
        }),
    );
  }

  searchToolOutput(
    toolCallId: string,
    query: string,
    options: {
      mode?: "literal" | "regex";
      caseSensitive?: boolean;
      contextLines?: number;
      matchLimit?: number;
      cursor?: string;
    } = {},
  ): Promise<ToolOutputSearchResult> {
    return this.request<{
      matches: Array<{
        artifact_id: string;
        filename?: string | null;
        line: number;
        context: Array<{
          line: number;
          text: string;
          line_truncated?: boolean;
        }>;
      }>;
      skipped?: Array<{ artifact_id: string; reason: string }>;
      truncated: boolean;
      continuation_cursor?: string | null;
    }>(`tool-calls/${encodeURIComponent(toolCallId)}/output/search`, {
      method: "POST",
      body: JSON.stringify({
        query,
        mode: options.mode ?? "literal",
        case_sensitive: options.caseSensitive ?? false,
        context_lines: options.contextLines ?? 1,
        match_limit: options.matchLimit ?? 20,
        cursor: options.cursor ?? null,
      }),
    }).then((value) => ({
      matches: value.matches.map((match) => ({
        artifactId: match.artifact_id,
        filename: match.filename ?? undefined,
        line: match.line,
        context: match.context.map((line) => ({
          line: line.line,
          text: line.text,
          lineTruncated: line.line_truncated,
        })),
      })),
      skipped: (value.skipped ?? []).map((item) => ({
        artifactId: item.artifact_id,
        reason: item.reason,
      })),
      truncated: value.truncated,
      continuationCursor: value.continuation_cursor ?? undefined,
    }));
  }

  readToolOutput(
    artifactId: string,
    startingLine = 1,
    lineCount = 100,
  ): Promise<ToolOutputReadResult> {
    return this.request<{
      artifact_id: string;
      filename?: string | null;
      searchable?: boolean;
      lines?: Array<{ line: number; text: string; line_truncated?: boolean }>;
      truncated?: boolean;
      continuation?: { starting_line?: number } | null;
    }>(`artifacts/${encodeURIComponent(artifactId)}/output/read`, {
      method: "POST",
      body: JSON.stringify({
        starting_line: startingLine,
        line_count: lineCount,
      }),
    }).then((value) => ({
      artifactId: value.artifact_id,
      filename: value.filename ?? undefined,
      searchable: value.searchable ?? false,
      lines: (value.lines ?? []).map((line) => ({
        line: line.line,
        text: line.text,
        lineTruncated: line.line_truncated,
      })),
      truncated: value.truncated ?? false,
      continuationStartingLine: value.continuation?.starting_line,
    }));
  }

  async downloadToolArtifact(
    artifactId: string,
  ): Promise<{ blob: Blob; filename?: string }> {
    const headers = new Headers({
      Accept: "application/octet-stream",
      "X-Nebula-Sensitive-Data-Acknowledged": "true",
      "X-Nebula-Operation-ID": newOperationId(),
    });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      `${this.baseUrl}/artifacts/${encodeURIComponent(artifactId)}/content`,
      {
        headers,
        credentials: "same-origin",
      },
    );
    if (!response.ok) throw await responseError(response);
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const filename = /filename="?([^";]+)"?/i.exec(disposition)?.[1];
    return { blob: await response.blob(), filename };
  }

  listRunnerProfiles(signal?: AbortSignal): Promise<RunnerProfile[]> {
    return this.request<WireRunnerProfile[] | { items?: WireRunnerProfile[] }>(
      "runner-profiles",
      { signal },
    ).then((value) => wireItems(value).map(mapRunnerProfile));
  }

  updateRunnerProfile(
    id: string,
    body: RunnerProfileUpdateRequest,
  ): Promise<RunnerProfile> {
    return this.request<WireRunnerProfile>(
      `runner-profiles/${encodeURIComponent(id)}`,
      {
        method: "PUT",
        body: JSON.stringify({
          name: body.name,
          runtime: body.runtimeType,
          executable: body.executable,
          context: body.context || null,
          socket: body.socket || null,
          platform: body.platform,
          isolation: body.isolationMode,
          ...(body.seccompProfile
            ? { seccomp_profile: body.seccompProfile }
            : {}),
          expected_revision: body.expectedRevision,
        }),
      },
    ).then(mapRunnerProfile);
  }

  getEngagementScope(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<EngagementScopePolicy> {
    return this.request<WireEngagementScope>(
      `engagements/${encodeURIComponent(engagementId)}/scope`,
      { signal },
    ).then(mapEngagementScope);
  }

  createScopeImport(
    body: ScopeImportCreateRequest,
    signal?: AbortSignal,
  ): Promise<ScopeImport> {
    return this.request<WireScopeImport>(
      `engagements/${encodeURIComponent(body.engagementId)}/scope-imports`,
      {
        method: "POST",
        signal,
        body: JSON.stringify({
          engagement_id: body.engagementId,
          provider_id: body.providerId,
          model: body.model,
          filename: body.filename,
          media_type: body.mediaType || null,
          content_base64: body.contentBase64,
          cloud_confirmed: body.cloudConfirmed,
        }),
      },
    ).then(mapScopeImport);
  }

  listScopeImports(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<ScopeImport[]> {
    return this.request<WireScopeImport[]>(
      `engagements/${encodeURIComponent(engagementId)}/scope-imports`,
      { signal },
    ).then((items) => items.map(mapScopeImport));
  }

  applyScopeImport(
    engagementId: string,
    scopeImportId: string,
    candidateIds: string[],
    expectedScopeRevision: number,
  ): Promise<ScopeImportApplyResult> {
    return this.request<{
      scope: WireEngagementScope;
      scope_import: WireScopeImport;
    }>(
      `engagements/${encodeURIComponent(engagementId)}/scope-imports/${encodeURIComponent(scopeImportId)}/apply`,
      {
        method: "POST",
        body: JSON.stringify({
          candidate_ids: candidateIds,
          expected_scope_revision: expectedScopeRevision,
        }),
      },
    ).then((value) => ({
      scope: mapEngagementScope(value.scope),
      scopeImport: mapScopeImport(value.scope_import),
    }));
  }

  discardScopeImport(
    engagementId: string,
    scopeImportId: string,
  ): Promise<ScopeImport> {
    return this.request<WireScopeImport>(
      `engagements/${encodeURIComponent(engagementId)}/scope-imports/${encodeURIComponent(scopeImportId)}/discard`,
      { method: "POST" },
    ).then(mapScopeImport);
  }

  updateEngagementScope(
    engagementId: string,
    body: EngagementScopeUpdateRequest,
  ): Promise<EngagementScopePolicy> {
    return this.request<WireEngagementScope>(
      `engagements/${encodeURIComponent(engagementId)}/scope`,
      {
        method: "PUT",
        body: JSON.stringify({
          allowed_cidrs: body.allowedCidrs,
          allowed_domains: body.allowedDomains,
          allowed_urls: body.allowedUrls,
          allowed_ports: body.allowedPorts,
          not_before: body.notBefore || null,
          not_after: body.notAfter || null,
          prohibited_actions: body.prohibitedActions,
          local_only: body.localOnly,
          max_concurrency: body.maxConcurrency,
          grants: body.grants.map((grant) => ({
            risk_classes: grant.riskClasses,
            tool_names: grant.toolNames,
            targets: grant.targets,
            granted_at: grant.grantedAt,
            expires_at: grant.expiresAt,
            granted_by: grant.grantedBy,
          })),
          expected_revision: body.expectedRevision || undefined,
        }),
      },
    ).then(mapEngagementScope);
  }

  stopRun(id: string, body: RunStopRequest = {}): Promise<AgentRunSummary> {
    return this.request<WireAgentRun>(`runs/${encodeURIComponent(id)}/stop`, {
      method: "POST",
      body: JSON.stringify({ reason: body.reason }),
    }).then(mapRun);
  }

  async deleteRun(id: string): Promise<void> {
    await this.request<void>(`runs/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  }

  listApprovals(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<ApprovalSummary>> {
    return this.listAll<WireApproval>("approvals", signal, engagementId).then(
      (items) =>
        page(
          items.map(mapApproval).filter((item) => item.status === "pending"),
        ),
    );
  }

  decideApproval(
    id: string,
    body: ApprovalDecisionRequest,
  ): Promise<ApprovalSummary> {
    return this.request<WireApproval>(
      `approvals/${encodeURIComponent(id)}/decision`,
      {
        method: "POST",
        body: JSON.stringify({
          decision: body.decision,
          reason: body.reason,
          edited_arguments: body.editedArguments,
        }),
      },
    ).then(mapApproval);
  }

  listAssets(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<AssetSummary>> {
    return this.listAll<WireAsset>("assets", signal, engagementId).then(
      (items) => page(items.map(mapAsset)),
    );
  }

  createAsset(body: AssetCreateRequest): Promise<AssetSummary> {
    const exposed =
      body.exposure === "external"
        ? true
        : body.exposure === "internal"
          ? false
          : null;
    return this.request<WireAsset>("assets", {
      method: "POST",
      body: JSON.stringify({
        engagement_id: body.engagementId,
        asset_type: body.kind,
        name: body.name.trim(),
        address: body.address || null,
        hostname: body.hostname || null,
        criticality: body.criticality ?? "medium",
        exposed,
        tags: body.tags ?? [],
        metadata: {},
      }),
    }).then(mapAsset);
  }

  listFindings(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<FindingSummary>> {
    return this.listAll<WireFinding>("findings", signal, engagementId).then(
      (items) => page(items.map(mapFinding)),
    );
  }

  createFinding(body: FindingCreateRequest): Promise<FindingSummary> {
    return this.request<WireFinding>("findings", {
      method: "POST",
      body: JSON.stringify({
        engagement_id: body.engagementId,
        title: body.title.trim(),
        description: body.description?.trim() ?? "",
        status: "candidate",
        severity: body.severity,
        severity_rationale: body.severityRationale?.trim() ?? "",
        asset_ids: [...new Set(body.assetIds ?? [])],
        cve_ids: normalizedIdentifiers(body.cveIds),
        cwe_ids: normalizedIdentifiers(body.cweIds),
        metadata: { origin: "manual_operator_entry" },
      }),
    }).then(mapFinding);
  }

  updateFinding(
    id: string,
    body: FindingUpdateRequest,
  ): Promise<FindingSummary> {
    return this.request<WireFinding>(`findings/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: body.expectedRevision,
        changes: {
          ...(body.title === undefined ? {} : { title: body.title.trim() }),
          ...(body.description === undefined
            ? {}
            : { description: body.description.trim() }),
          ...(body.severity === undefined ? {} : { severity: body.severity }),
          ...(body.severityRationale === undefined
            ? {}
            : { severity_rationale: body.severityRationale.trim() }),
          ...(body.assetIds === undefined
            ? {}
            : { asset_ids: [...new Set(body.assetIds)] }),
          ...(body.cveIds === undefined
            ? {}
            : { cve_ids: normalizedIdentifiers(body.cveIds) }),
          ...(body.cweIds === undefined
            ? {}
            : { cwe_ids: normalizedIdentifiers(body.cweIds) }),
          ...(body.status === undefined
            ? {}
            : { status: body.status.replaceAll("_", "-") }),
          ...(body.evidenceIds === undefined
            ? {}
            : { evidence_ids: [...new Set(body.evidenceIds)] }),
          ...(body.verifierId === undefined
            ? {}
            : { verifier_id: body.verifierId }),
          ...(body.verifiedAt === undefined
            ? {}
            : { verified_at: body.verifiedAt }),
        },
      }),
    }).then(mapFinding);
  }

  listEvidence(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<EvidenceSummary>> {
    return this.listAll<WireEvidence>("evidence", signal, engagementId).then(
      (items) => page(items.map(mapEvidence)),
    );
  }

  uploadEvidence(
    body: EvidenceUploadRequest,
    signal?: AbortSignal,
  ): Promise<EvidenceSummary> {
    return this.request<WireEvidence>("evidence/upload", {
      method: "POST",
      signal,
      body: JSON.stringify({
        engagement_id: body.engagementId,
        filename: body.filename,
        title: body.title.trim(),
        evidence_type: body.evidenceType,
        content_base64: body.contentBase64,
        media_type: body.mediaType,
        description: body.description ?? "",
        source: body.source,
        finding_id: body.findingId,
        asset_ids: body.assetIds ?? [],
        captured_by: body.capturedBy,
        source_version: body.sourceVersion,
        parent_artifact_id: body.parentArtifactId,
        source_context: body.sourceContext ?? {},
        edit_recipe: body.editRecipe,
        metadata: body.metadata ?? {},
      }),
    }).then(mapEvidence);
  }

  listReports(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<ReportSummary>> {
    return this.listAll<WireReport>("reports", signal, engagementId).then(
      (items) => page(items.map(mapReport)),
    );
  }

  createReport(body: ReportCreateRequest): Promise<ReportSummary> {
    return this.request<WireReport>("reports", {
      method: "POST",
      body: JSON.stringify({
        engagement_id: body.engagementId,
        title: body.title.trim(),
        status: body.status ?? "draft",
        executive_summary: body.executiveSummary ?? "",
        finding_ids: body.findingIds ?? [],
        observation_ids: body.observationIds ?? [],
        note_transforms: (body.noteTransforms ?? []).map(
          reportNoteTransformBody,
        ),
        artifact_ids: [],
        metadata: {},
      }),
    }).then(mapReport);
  }

  updateReport(id: string, body: ReportUpdateRequest): Promise<ReportSummary> {
    return this.request<WireReport>(`reports/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: body.expectedRevision,
        changes: {
          ...(body.title === undefined ? {} : { title: body.title.trim() }),
          ...(body.status === undefined ? {} : { status: body.status }),
          ...(body.executiveSummary === undefined
            ? {}
            : { executive_summary: body.executiveSummary }),
          ...(body.findingIds === undefined
            ? {}
            : { finding_ids: body.findingIds }),
          ...(body.observationIds === undefined
            ? {}
            : { observation_ids: body.observationIds }),
          ...(body.noteTransforms === undefined
            ? {}
            : {
                note_transforms: body.noteTransforms.map(
                  reportNoteTransformBody,
                ),
              }),
          ...(body.executiveSummaryProvenance === undefined
            ? {}
            : {
                executive_summary_provenance: body.executiveSummaryProvenance
                  ? writingProvenanceBody(body.executiveSummaryProvenance)
                  : null,
              }),
        },
      }),
    }).then(mapReport);
  }

  signOffReport(
    id: string,
    expectedRevision: number,
    operatorId: string,
    attestation?: string,
  ): Promise<ReportSummary> {
    return this.request<WireReport>(
      `reports/${encodeURIComponent(id)}/sign-off`,
      {
        method: "POST",
        body: JSON.stringify({
          expected_revision: expectedRevision,
          operator_id: operatorId,
          ...(attestation ? { attestation } : {}),
        }),
      },
    ).then(mapReport);
  }

  listObservations(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<ObservationSummary>> {
    return this.listAll<WireObservation>(
      "observations",
      signal,
      engagementId,
    ).then((items) => page(items.map(mapObservation)));
  }

  createObservation(
    body: ObservationCreateRequest,
  ): Promise<ObservationSummary> {
    return this.request<WireObservation>("observations", {
      method: "POST",
      body: JSON.stringify({
        engagement_id: body.engagementId,
        observation_type: body.observationType ?? "note",
        title: body.title.trim(),
        body: body.body ?? "",
        asset_ids: [...new Set(body.assetIds ?? [])],
        service_ids: [...new Set(body.serviceIds ?? [])],
        evidence_ids: [...new Set(body.evidenceIds ?? [])],
        source: body.source ?? "operator-note",
        confidence: body.confidence ?? 1,
        metadata: body.metadata ?? {},
      }),
    }).then(mapObservation);
  }

  updateObservation(
    id: string,
    body: ObservationUpdateRequest,
  ): Promise<ObservationSummary> {
    const changes: Record<string, unknown> = {};
    if (body.title !== undefined) changes.title = body.title.trim();
    if (body.body !== undefined) changes.body = body.body;
    if (body.assetIds !== undefined)
      changes.asset_ids = [...new Set(body.assetIds)];
    if (body.serviceIds !== undefined)
      changes.service_ids = [...new Set(body.serviceIds)];
    if (body.evidenceIds !== undefined)
      changes.evidence_ids = [...new Set(body.evidenceIds)];
    if (body.confidence !== undefined) changes.confidence = body.confidence;
    if (body.metadata !== undefined) changes.metadata = body.metadata;
    return this.request<WireObservation>(
      `observations/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          changes,
          expected_revision: body.expectedRevision,
        }),
      },
    ).then(mapObservation);
  }

  async deleteObservation(id: string, expectedRevision: number): Promise<void> {
    await this.request<void>(`observations/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "If-Match": String(expectedRevision) },
    });
  }

  transformWriting(
    body: WritingTransformRequest,
    signal?: AbortSignal,
  ): Promise<WritingTransformResponse> {
    return this.request<WireWritingTransformResponse>("writing/transform", {
      method: "POST",
      signal,
      body: JSON.stringify({
        engagement_id: body.engagementId,
        provider_id: body.providerId,
        model: body.model,
        purpose: body.purpose,
        instruction: body.instruction,
        source_text: body.sourceText,
        cloud_confirmed: body.cloudConfirmed ?? false,
      }),
    }).then((value) => ({
      content: value.content,
      provenance: mapAIWritingProvenance(value.provenance),
      usage: {
        inputTokens: value.usage.input_tokens,
        outputTokens: value.usage.output_tokens,
        totalTokens: value.usage.total_tokens,
      },
    }));
  }

  renderReport(id: string, reportRevision: number): Promise<ReportRender> {
    return this.request<WireReportRender>(
      `reports/${encodeURIComponent(id)}/renders`,
      {
        method: "POST",
        body: JSON.stringify({ report_revision: reportRevision }),
      },
    ).then(mapReportRender);
  }

  getReportRender(id: string, signal?: AbortSignal): Promise<ReportRender> {
    return this.request<WireReportRender>(
      `report-renders/${encodeURIComponent(id)}`,
      { signal },
    ).then(mapReportRender);
  }

  async downloadReportPdf(id: string, signal?: AbortSignal): Promise<Blob> {
    const headers = new Headers({ Accept: "application/pdf" });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      `${this.baseUrl}/report-renders/${encodeURIComponent(id)}/pdf`,
      { headers, signal, credentials: "same-origin" },
    );
    if (!response.ok) throw await responseError(response);
    return response.blob();
  }

  async exportEngagementBundle(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const headers = new Headers({
      Accept: "application/zip",
      "X-Nebula-Sensitive-Data-Acknowledged": "true",
    });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      `${this.baseUrl}/engagements/${encodeURIComponent(engagementId)}/export-bundle`,
      { method: "POST", headers, signal, credentials: "same-origin" },
    );
    if (!response.ok) throw await responseError(response);
    return response.blob();
  }

  listProviders(signal?: AbortSignal): Promise<Page<ProviderHealth>> {
    // Provider profiles are global in the current single-organization Core.
    // Applying engagement_id would correctly return no rows.
    return this.listAll<WireProvider>("providers", signal).then((items) =>
      page(items.map(mapProvider)),
    );
  }

  listProviderCatalog(signal?: AbortSignal): Promise<ProviderCatalogEntry[]> {
    return this.request<WireProviderCatalogEntry[]>("provider-catalog", {
      signal,
    }).then((items) => items.map(mapProviderCatalog));
  }

  discoverLocalProviders(
    signal?: AbortSignal,
  ): Promise<LocalProviderDetection[]> {
    return this.request<WireLocalProviderDetection[]>(
      "providers/discover-local",
      { signal },
    ).then((items) => items.map(mapLocalProviderDetection));
  }

  createProvider(body: ProviderCreateRequest): Promise<ProviderHealth> {
    const defaultModel = configuredDefaultModel(body.defaultModel);
    const modelAllowlist = configuredModelAllowlist(
      body.modelAllowlist,
      defaultModel,
    );
    const credentialEnv = body.credentialEnv?.trim().replace(/^env:/, "");
    return this.request<WireProvider>("providers", {
      method: "POST",
      body: JSON.stringify({
        name: body.name.trim(),
        provider_type: body.providerType,
        endpoint: body.endpoint?.trim() || null,
        enabled: true,
        is_local: body.local,
        secret_ref:
          body.credentialRef ?? (credentialEnv ? `env:${credentialEnv}` : null),
        model_allowlist: modelAllowlist,
        capabilities: { streaming: true },
        privacy: {
          local_only: body.local,
          permits_sensitive_data: body.permitsSensitiveData === true,
        },
        metadata: {
          ...(defaultModel ? { default_model: defaultModel } : {}),
          ...(body.options && Object.keys(body.options).length
            ? { options: body.options }
            : {}),
        },
      }),
    }).then(mapProvider);
  }

  updateProvider(
    id: string,
    body: ProviderUpdateRequest,
  ): Promise<ProviderHealth> {
    const defaultModel = configuredDefaultModel(body.defaultModel);
    const modelAllowlist = configuredModelAllowlist(
      body.modelAllowlist,
      defaultModel,
    );
    const credentialEnv = body.credentialEnv?.trim().replace(/^env:/, "");
    const metadata = { ...(body.metadata ?? {}) };
    delete metadata.default_model;
    delete metadata.options;
    if (defaultModel) metadata.default_model = defaultModel;
    if (body.options && Object.keys(body.options).length)
      metadata.options = body.options;
    return this.request<WireProvider>(`providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        changes: {
          name: body.name.trim(),
          endpoint: body.endpoint?.trim() || null,
          secret_ref:
            body.credentialRef ??
            (credentialEnv ? `env:${credentialEnv}` : null),
          model_allowlist: modelAllowlist,
          privacy: {
            local_only: body.local,
            retention: body.retention ?? null,
            residency: body.residency,
            permits_sensitive_data: body.permitsSensitiveData,
          },
          metadata,
        },
        expected_revision: body.expectedRevision,
      }),
    }).then(mapProvider);
  }

  setProviderEnabled(
    id: string,
    enabled: boolean,
    expectedRevision: number,
  ): Promise<ProviderHealth> {
    return this.request<WireProvider>(`providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        changes: { enabled },
        expected_revision: expectedRevision,
      }),
    }).then(mapProvider);
  }

  async deleteProvider(id: string, expectedRevision: number): Promise<void> {
    await this.request<void>(`providers/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "If-Match": String(expectedRevision) },
    });
  }

  refreshProviderHealth(
    id: string,
    signal?: AbortSignal,
  ): Promise<ProviderRuntimeHealth> {
    return this.request<WireProviderRuntimeHealth>(
      `providers/${encodeURIComponent(id)}/health`,
      { method: "POST", signal },
    ).then(mapProviderRuntimeHealth);
  }

  async verifyProviderCapabilities(
    id: string,
    model: string,
    expectedRevision: number,
    signal?: AbortSignal,
  ): Promise<ProviderHealth> {
    await this.request<WireProviderVerificationResponse>(
      `providers/${encodeURIComponent(id)}/capabilities/verify`,
      {
        method: "POST",
        signal,
        body: JSON.stringify({ model, expected_revision: expectedRevision }),
      },
    );
    return this.request<WireProvider>(
      `providers/${encodeURIComponent(id)}`,
    ).then(mapProvider);
  }

  listKnowledgeSources(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<KnowledgeSource>> {
    return this.listAll<WireKnowledgeSource>(
      "knowledge",
      signal,
      engagementId,
    ).then((items) => page(items.map(mapKnowledgeSource)));
  }

  ingestKnowledgeSource(
    body: KnowledgeIngestRequest,
    signal?: AbortSignal,
  ): Promise<KnowledgeSource> {
    return this.request<WireKnowledgeSource>("knowledge/ingest", {
      method: "POST",
      signal,
      body: JSON.stringify({
        engagement_id: body.engagementId,
        filename: body.filename,
        media_type: body.mediaType,
        content_base64: body.contentBase64,
      }),
    }).then(mapKnowledgeSource);
  }

  reindexKnowledgeSource(
    id: string,
    signal?: AbortSignal,
  ): Promise<KnowledgeSource> {
    return this.request<WireKnowledgeSource>(
      `knowledge/${encodeURIComponent(id)}/reindex`,
      {
        method: "POST",
        signal,
      },
    ).then(mapKnowledgeSource);
  }

  async deleteKnowledgeSource(id: string, signal?: AbortSignal): Promise<void> {
    await this.request<void>(`knowledge/${encodeURIComponent(id)}`, {
      method: "DELETE",
      signal,
    });
  }

  async getArtifactContent(id: string, signal?: AbortSignal): Promise<Blob> {
    const headers = new Headers({ Accept: "*/*" });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      `${this.baseUrl}/artifacts/${encodeURIComponent(id)}/content`,
      { headers, signal, credentials: "same-origin" },
    );
    if (!response.ok) throw await responseError(response);
    return response.blob();
  }

  containerTerminalCapabilities(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<ContainerTerminalCapabilities> {
    return this.request<WireContainerTerminalCapabilities>(
      `engagements/${encodeURIComponent(engagementId)}/container-terminal/capabilities`,
      { signal },
    ).then((value) => ({
      engagementId: value.engagement_id,
      ready: value.ready,
      detail: value.detail ?? undefined,
      sourceImage: value.source_image,
      installedPackages: value.installed_packages,
      network: mapContainerTerminalNetwork(value.network),
      security: mapContainerTerminalSecurity(value.security),
      workspace: value.workspace,
      limits: mapExecutionLimits(value.limits),
      idleTimeoutSeconds: value.idle_timeout_seconds,
      freshContainer: value.fresh_container,
    }));
  }

  terminalRecordingTools(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<TerminalRecordingTools> {
    return this.request<WireTerminalRecordingTools>(
      `engagements/${encodeURIComponent(engagementId)}/terminal/recording-tools`,
      { signal },
    ).then(mapTerminalRecordingTools);
  }

  updateTerminalRecordingTools(
    engagementId: string,
    update: {
      customTools: string[];
      disabledTools: string[];
      expectedRevision: number;
      expectedManifestSha256?: string;
    },
  ): Promise<TerminalRecordingTools> {
    return this.request<WireTerminalRecordingTools>(
      `engagements/${encodeURIComponent(engagementId)}/terminal/recording-tools`,
      {
        method: "PUT",
        body: JSON.stringify({
          custom_tools: update.customTools,
          disabled_tools: update.disabledTools,
          expected_revision: update.expectedRevision,
          expected_manifest_sha256: update.expectedManifestSha256,
        }),
      },
    ).then(mapTerminalRecordingTools);
  }

  terminalCommandHistoryStatus(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<TerminalCommandHistoryStatus> {
    return this.request<{
      engagement_id: string;
      enabled: boolean;
      capture_mode: "selected_tools";
      record_count: number;
      recorded_output_count: number;
      metadata_only_count: number;
      classification_failure_count: number;
      degraded_count: number;
      truncated_count: number;
      audit_gap_count: number;
      captured_output_bytes: number;
      retention_days?: number | null;
      max_records?: number | null;
      oldest_recorded_at?: string | null;
      newest_recorded_at?: string | null;
    }>(
      `engagements/${encodeURIComponent(engagementId)}/terminal/commands/status`,
      { signal },
    ).then((value) => ({
      engagementId: value.engagement_id,
      enabled: value.enabled,
      captureMode: value.capture_mode,
      recordCount: value.record_count,
      recordedOutputCount: value.recorded_output_count,
      metadataOnlyCount: value.metadata_only_count,
      classificationFailureCount: value.classification_failure_count,
      degradedCount: value.degraded_count,
      truncatedCount: value.truncated_count,
      auditGapCount: value.audit_gap_count,
      capturedOutputBytes: value.captured_output_bytes,
      retentionDays: value.retention_days ?? undefined,
      maxRecords: value.max_records ?? undefined,
      oldestRecordedAt: value.oldest_recorded_at ?? undefined,
      newestRecordedAt: value.newest_recorded_at ?? undefined,
    }));
  }

  listTerminalCommands(
    engagementId: string,
    search = "",
    offset = 0,
    limit = 100,
    signal?: AbortSignal,
  ): Promise<TerminalCommandPage> {
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(limit),
    });
    if (search) params.set("search", search);
    return this.request<{
      records: Array<{
        id: string;
        engagement_id: string;
        session_id: string;
        operator_id?: string | null;
        shell_sequence?: string | null;
        command: string;
        command_sha256?: string | null;
        cwd: string;
        status: TerminalCommandRecord["status"];
        exit_code?: number | null;
        started_at?: string | null;
        completed_at?: string | null;
        occurred_at: string;
        raw_output_available: boolean;
        redacted_output_available: boolean;
        observed_output_bytes: number;
        captured_output_bytes: number;
        output_sha256?: string | null;
        output_truncated: boolean;
        output_preview: string;
        capture_error?: string | null;
        capture_decision: TerminalCommandRecord["captureDecision"];
        matched_tools: string[];
        recording_policy_revision?: number | null;
        runtime_image_digest?: string | null;
      }>;
      total: number;
      offset: number;
      limit: number;
      next_offset?: number | null;
    }>(
      `engagements/${encodeURIComponent(engagementId)}/terminal/commands?${params}`,
      { signal },
    ).then((value) => ({
      records: value.records.map((record) => ({
        id: record.id,
        engagementId: record.engagement_id,
        sessionId: record.session_id,
        operatorId: record.operator_id ?? undefined,
        shellSequence: record.shell_sequence ?? undefined,
        command: record.command,
        commandSha256: record.command_sha256 ?? undefined,
        cwd: record.cwd,
        status: record.status,
        exitCode: record.exit_code ?? undefined,
        startedAt: record.started_at ?? undefined,
        completedAt: record.completed_at ?? undefined,
        occurredAt: record.occurred_at,
        rawOutputAvailable: record.raw_output_available,
        redactedOutputAvailable: record.redacted_output_available,
        observedOutputBytes: record.observed_output_bytes,
        capturedOutputBytes: record.captured_output_bytes,
        outputSha256: record.output_sha256 ?? undefined,
        outputTruncated: record.output_truncated,
        outputPreview: record.output_preview,
        captureError: record.capture_error ?? undefined,
        captureDecision: record.capture_decision,
        matchedTools: record.matched_tools,
        recordingPolicyRevision: record.recording_policy_revision ?? undefined,
        runtimeImageDigest: record.runtime_image_digest ?? undefined,
      })),
      total: value.total,
      offset: value.offset,
      limit: value.limit,
      nextOffset: value.next_offset ?? undefined,
    }));
  }

  setTerminalCommandHistoryEnabled(
    engagementId: string,
    enabled: boolean,
  ): Promise<TerminalCommandHistoryStatus> {
    return this.request<{
      engagement_id: string;
      enabled: boolean;
      capture_mode: "selected_tools";
      record_count: number;
      recorded_output_count: number;
      metadata_only_count: number;
      classification_failure_count: number;
      degraded_count: number;
      truncated_count: number;
      audit_gap_count: number;
      captured_output_bytes: number;
      retention_days?: number | null;
      max_records?: number | null;
      oldest_recorded_at?: string | null;
      newest_recorded_at?: string | null;
    }>(
      `engagements/${encodeURIComponent(engagementId)}/terminal/commands/status`,
      {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      },
    ).then((value) => ({
      engagementId: value.engagement_id,
      enabled: value.enabled,
      captureMode: value.capture_mode,
      recordCount: value.record_count,
      recordedOutputCount: value.recorded_output_count,
      metadataOnlyCount: value.metadata_only_count,
      classificationFailureCount: value.classification_failure_count,
      degradedCount: value.degraded_count,
      truncatedCount: value.truncated_count,
      auditGapCount: value.audit_gap_count,
      capturedOutputBytes: value.captured_output_bytes,
      retentionDays: value.retention_days ?? undefined,
      maxRecords: value.max_records ?? undefined,
      oldestRecordedAt: value.oldest_recorded_at ?? undefined,
      newestRecordedAt: value.newest_recorded_at ?? undefined,
    }));
  }

  async clearTerminalCommands(engagementId: string): Promise<number> {
    const result = await this.request<{
      engagement_id: string;
      cleared: number;
    }>(`engagements/${encodeURIComponent(engagementId)}/terminal/commands`, {
      method: "DELETE",
    });
    return result.cleared;
  }

  async terminalCommandOutput(
    engagementId: string,
    commandId: string,
    raw = false,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const headers = new Headers({
      Accept: raw ? "application/octet-stream" : "text/plain",
    });
    if (raw) headers.set("X-Nebula-Sensitive-Data-Acknowledged", "true");
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const chunks: ArrayBuffer[] = [];
    let offset = 0;
    while (true) {
      const response = await this.fetchImpl(
        `${this.baseUrl}/engagements/${encodeURIComponent(engagementId)}/terminal/commands/${encodeURIComponent(commandId)}/output?raw=${raw ? "true" : "false"}&offset=${offset}&limit=262144`,
        { headers, signal, credentials: "same-origin" },
      );
      if (!response.ok) throw await responseError(response);
      chunks.push(await response.arrayBuffer());
      const total = Number(response.headers.get("X-Nebula-Output-Total"));
      const next = Number(response.headers.get("X-Nebula-Output-Next"));
      if (
        !Number.isFinite(total) ||
        !Number.isFinite(next) ||
        next >= total ||
        next <= offset
      )
        break;
      offset = next;
    }
    return new Blob(chunks, {
      type: raw ? "application/octet-stream" : "text/plain;charset=utf-8",
    });
  }

  preflightContainerTerminal(
    body: ContainerTerminalRequest,
    signal?: AbortSignal,
  ): Promise<ContainerTerminalPreflight> {
    return this.request<WireContainerTerminalPreflight>(
      "container-terminal/preflight",
      {
        method: "POST",
        signal,
        body: JSON.stringify(terminalBody(body)),
      },
    ).then(mapContainerTerminalPreflight);
  }

  startContainerTerminal(
    body: ContainerTerminalRequest,
    preview: Pick<
      ContainerTerminalPreflight,
      "previewToken" | "previewFingerprint"
    >,
    clientIdempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<ContainerTerminalSession> {
    return this.request<WireContainerTerminalSession>(
      "container-terminal/sessions",
      {
        method: "POST",
        signal,
        body: JSON.stringify({
          ...terminalBody(body),
          preview_token: preview.previewToken,
          preview_fingerprint: preview.previewFingerprint,
          client_idempotency_key: clientIdempotencyKey,
        }),
      },
    ).then(mapContainerTerminalSession);
  }

  recoverContainerTerminal(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<ContainerTerminalRecovery> {
    return this.request<WireContainerTerminalRecovery>(
      `engagements/${encodeURIComponent(engagementId)}/container-terminal/recover`,
      { method: "POST", signal },
    ).then((value) => ({
      active: value.active,
      session: value.session
        ? mapContainerTerminalSession(value.session)
        : undefined,
      runtime: value.runtime
        ? mapContainerTerminalRuntime(value.runtime)
        : undefined,
    }));
  }

  recoverContainerTerminals(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<ContainerTerminalRecoveryList> {
    return this.request<WireContainerTerminalRecoveryList>(
      `engagements/${encodeURIComponent(engagementId)}/container-terminals/recover`,
      { method: "POST", signal },
    ).then((value) => ({
      sessions: value.sessions.map((item) => ({
        session: mapContainerTerminalSession(item.session),
        runtime: mapContainerTerminalRuntime(item.runtime),
      })),
    }));
  }

  containerTerminalCapacity(
    signal?: AbortSignal,
  ): Promise<ContainerTerminalCapacity> {
    return this.request<WireContainerTerminalCapacity>(
      "container-terminal/capacity",
      { signal },
    ).then((value) => ({
      activeSessions: value.active_sessions,
      availableSessions: value.available_sessions,
      maxActiveSessions: value.max_active_sessions,
    }));
  }

  closeContainerTerminal(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<void> {
    return this.request<void>(
      `container-terminals/${encodeURIComponent(sessionId)}`,
      { method: "DELETE", signal },
    );
  }

  executionCapabilities(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<ExecutionCapabilities> {
    return this.request<WireExecutionCapabilities>(
      `engagements/${encodeURIComponent(engagementId)}/execution-capabilities`,
      { signal },
    ).then((value) => ({
      engagementId: value.engagement_id,
      ready: value.ready,
      runtimes: value.runtimes.map((runtime) => ({
        language: runtime.language,
        aliases: runtime.aliases,
        offline: runtime.offline,
        scopedNetwork: runtime.scoped_network,
        detail: runtime.detail ?? undefined,
      })),
      limits: mapExecutionLimits(value.limits),
      workspace: value.workspace,
    }));
  }

  preflightExecution(
    body: ExecutionRequest,
    signal?: AbortSignal,
  ): Promise<ExecutionPreflight> {
    return this.request<WireExecutionPreflight>("executions/preflight", {
      method: "POST",
      signal,
      body: JSON.stringify(executionBody(body)),
    }).then(mapExecutionPreflight);
  }

  startExecution(
    body: ExecutionRequest,
    preview: Pick<ExecutionPreflight, "previewToken" | "previewFingerprint">,
    clientIdempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<OperatorExecution> {
    return this.request<WireOperatorExecution>("executions", {
      method: "POST",
      signal,
      body: JSON.stringify({
        ...executionBody(body),
        preview_token: preview.previewToken,
        preview_fingerprint: preview.previewFingerprint,
        client_idempotency_key: clientIdempotencyKey,
      }),
    }).then(mapOperatorExecution);
  }

  listExecutions(
    engagementId: string,
    options: {
      offset?: number;
      limit?: number;
      status?: string;
      language?: string;
      operatorId?: string;
      dateFrom?: string;
      dateTo?: string;
      query?: string;
    } = {},
    signal?: AbortSignal,
  ): Promise<Page<OperatorExecution>> {
    const parameters = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 100),
    });
    if (options.status) parameters.set("status", options.status);
    if (options.language) parameters.set("language", options.language);
    if (options.operatorId) parameters.set("operator_id", options.operatorId);
    if (options.dateFrom) parameters.set("date_from", options.dateFrom);
    if (options.dateTo) parameters.set("date_to", options.dateTo);
    if (options.query) parameters.set("query", options.query);
    return this.request<WireOperatorExecution[]>(
      `engagements/${encodeURIComponent(engagementId)}/executions?${parameters}`,
      { signal },
    ).then((items) => page(items.map(mapOperatorExecution)));
  }

  getExecution(id: string, signal?: AbortSignal): Promise<OperatorExecution> {
    return this.request<WireOperatorExecution>(
      `executions/${encodeURIComponent(id)}`,
      { signal },
    ).then(mapOperatorExecution);
  }

  cancelExecution(
    id: string,
    signal?: AbortSignal,
  ): Promise<OperatorExecution> {
    return this.request<WireOperatorExecution>(
      `executions/${encodeURIComponent(id)}/cancel`,
      {
        method: "POST",
        signal,
      },
    ).then(mapOperatorExecution);
  }

  generateExecutionDraft(
    executionId: string,
    providerId: string,
    model: string,
    cloudConfirmed: boolean,
  ): Promise<GeneratedDraft> {
    return this.request<WireGeneratedDraft>(
      `executions/${encodeURIComponent(executionId)}/draft-notes`,
      {
        method: "POST",
        body: JSON.stringify({
          provider_id: providerId,
          model,
          cloud_confirmed: cloudConfirmed,
        }),
      },
    ).then(mapGeneratedDraft);
  }

  getGeneratedDraft(id: string, signal?: AbortSignal): Promise<GeneratedDraft> {
    return this.request<WireGeneratedDraft>(
      `generated-drafts/${encodeURIComponent(id)}`,
      { signal },
    ).then(mapGeneratedDraft);
  }

  editGeneratedDraft(
    id: string,
    content: GeneratedDraftContent,
    expectedRevision: number,
  ): Promise<GeneratedDraft> {
    return this.request<WireGeneratedDraft>(
      `generated-drafts/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          content: wireDraftContent(content),
          expected_revision: expectedRevision,
        }),
      },
    ).then(mapGeneratedDraft);
  }

  transitionGeneratedDraft(
    id: string,
    transition: "accept" | "reject",
    expectedRevision: number,
  ): Promise<GeneratedDraft> {
    return this.request<WireGeneratedDraft>(
      `generated-drafts/${encodeURIComponent(id)}/${transition}`,
      {
        method: "POST",
        body: JSON.stringify({ expected_revision: expectedRevision }),
      },
    ).then(mapGeneratedDraft);
  }

  attachExecutionToChat(
    executionId: string,
    providerId: string,
    model: string,
    cloudConfirmed: boolean,
  ): Promise<ExecutionChatAttachment> {
    return this.request<WireExecutionChatAttachment>(
      `executions/${encodeURIComponent(executionId)}/chat-attachments`,
      {
        method: "POST",
        body: JSON.stringify({
          provider_id: providerId,
          model,
          cloud_confirmed: cloudConfirmed,
        }),
      },
    ).then((value) => ({
      sessionId: value.session.id,
      contextFingerprint: value.context_fingerprint,
      categories: value.categories,
    }));
  }

  async executionOutput(
    id: string,
    stream: "stdout" | "stderr",
    offset = 0,
    signal?: AbortSignal,
  ): Promise<ExecutionOutputPage> {
    const headers = new Headers({ Accept: "text/plain" });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      `${this.baseUrl}/executions/${encodeURIComponent(id)}/output/${stream}?offset=${offset}&limit=${256 * 1024}`,
      { headers, signal, credentials: "same-origin" },
    );
    if (!response.ok) throw await responseError(response);
    return {
      text: await response.text(),
      totalBytes: Number(response.headers.get("x-nebula-output-total") ?? 0),
      nextOffset: Number(
        response.headers.get("x-nebula-output-next") ?? offset,
      ),
    };
  }

  listWorkspace(
    engagementId: string,
    path = "",
    offset = 0,
    signal?: AbortSignal,
  ): Promise<WorkspaceListing> {
    const parameters = new URLSearchParams({
      path,
      offset: String(offset),
      limit: "100",
    });
    return this.request<WireWorkspaceListing>(
      `engagements/${encodeURIComponent(engagementId)}/workspace?${parameters}`,
      { signal },
    ).then(mapWorkspaceListing);
  }

  previewWorkspaceFile(
    engagementId: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<WorkspacePreview> {
    return this.request<WireWorkspacePreview>(
      `engagements/${encodeURIComponent(engagementId)}/workspace/preview?path=${encodeURIComponent(path)}`,
      { signal },
    ).then(mapWorkspacePreview);
  }

  promoteWorkspaceFile(
    engagementId: string,
    path: string,
    title?: string,
    description?: string,
  ): Promise<EvidenceSummary> {
    return this.request<WireEvidence>(
      `engagements/${encodeURIComponent(engagementId)}/workspace/promote`,
      {
        method: "POST",
        body: JSON.stringify({ path, title, description: description ?? "" }),
      },
    ).then(mapEvidence);
  }

  resetWorkspace(
    engagementId: string,
    engagementName: string,
  ): Promise<WorkspaceResetResult> {
    return this.request<{ engagement_id: string; removed_entries: number }>(
      `engagements/${encodeURIComponent(engagementId)}/workspace/reset`,
      {
        method: "POST",
        body: JSON.stringify({ engagement_name: engagementName }),
      },
    ).then((value) => ({
      engagementId: value.engagement_id,
      removedEntries: value.removed_entries,
    }));
  }

  async uploadWorkspaceFile(
    engagementId: string,
    path: string,
    file: Blob,
    overwrite = false,
    signal?: AbortSignal,
    expectedSha256?: string,
  ): Promise<WorkspaceUploadResult> {
    const headers = new Headers({ "Content-Type": "application/octet-stream" });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (expectedSha256) headers.set("If-Match", expectedSha256);
    const parameters = new URLSearchParams({
      path,
      overwrite: String(overwrite),
    });
    const response = await this.fetchImpl(
      `${this.baseUrl}/engagements/${encodeURIComponent(engagementId)}/workspace/file?${parameters}`,
      {
        method: "PUT",
        headers,
        body: file,
        signal,
        credentials: "same-origin",
      },
    );
    if (!response.ok) throw await responseError(response);
    const value = (await response.json()) as {
      engagement_id: string;
      path: string;
      size: number;
      sha256: string;
      overwritten: boolean;
    };
    return {
      engagementId: value.engagement_id,
      path: value.path,
      size: value.size,
      sha256: value.sha256,
      overwritten: value.overwritten,
    };
  }

  async downloadWorkspaceFile(
    engagementId: string,
    path: string,
    signal?: AbortSignal,
  ): Promise<Blob> {
    const headers = new Headers({ Accept: "*/*" });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      `${this.baseUrl}/engagements/${encodeURIComponent(engagementId)}/workspace/download?path=${encodeURIComponent(path)}`,
      { headers, signal, credentials: "same-origin" },
    );
    if (!response.ok) throw await responseError(response);
    return response.blob();
  }

  completeChat(
    body: ChatCompletionRequest,
    signal?: AbortSignal,
  ): Promise<ChatCompletionResponse> {
    return this.request<WireChatCompletion>("chat/completions", {
      method: "POST",
      signal,
      body: JSON.stringify(chatRequestBody(body, false)),
    }).then(mapChatCompletion);
  }

  listChatSessions(
    engagementId: string,
    signal?: AbortSignal,
  ): Promise<Page<ChatSessionSummary>> {
    return this.listAll<WireChatSession>(
      "chat-sessions",
      signal,
      engagementId,
    ).then((items) => page(items.map(mapChatSession)));
  }

  renameChatSession(
    sessionId: string,
    body: ChatSessionRenameRequest,
  ): Promise<ChatSessionSummary> {
    return this.request<WireChatSession>(
      `chat-sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          title: body.title.trim(),
          expected_revision: body.expectedRevision,
        }),
      },
    ).then(mapChatSession);
  }

  async deleteChatSession(sessionId: string): Promise<void> {
    await this.request<void>(`chat-sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
  }

  listChatMessages(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<PersistedChatMessage[]> {
    return this.request<WirePersistedChatMessage[]>(
      `chat/sessions/${encodeURIComponent(sessionId)}/messages`,
      { signal },
    ).then((items) => items.map(mapPersistedChatMessage));
  }

  getChatContext(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<ContextStatus> {
    return this.request<WireContextStatus>(
      `chat/sessions/${encodeURIComponent(sessionId)}/context`,
      { signal },
    ).then(mapContextStatus);
  }

  getRunContext(runId: string, signal?: AbortSignal): Promise<ContextStatus> {
    return this.request<WireContextStatus>(
      `runs/${encodeURIComponent(runId)}/context`,
      { signal },
    ).then(mapContextStatus);
  }

  async streamChat(
    body: ChatCompletionRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal,
    resumeTurnId?: string,
  ): Promise<ChatCompletionResponse | undefined> {
    const headers = new Headers({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(
      resumeTurnId
        ? `${this.baseUrl}/chat/turns/${encodeURIComponent(resumeTurnId)}/resume`
        : `${this.baseUrl}/chat/completions`,
      {
        method: "POST",
        headers,
        signal,
        credentials: "same-origin",
        body: resumeTurnId
          ? undefined
          : JSON.stringify(chatRequestBody(body, true)),
      },
    );
    if (!response.ok) throw await responseError(response);
    if (!response.body) {
      throw new ApiError("The chat response stream was empty.", 502);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let completed: ChatCompletionResponse | undefined;
    let pausedForApproval = false;

    const processBlock = (block: string) => {
      const lines = block.replace(/\r/g, "").split("\n");
      const data = lines
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data || data === "[DONE]") return;
      let wire: WireChatStreamEvent;
      try {
        wire = JSON.parse(data) as WireChatStreamEvent;
      } catch (caughtError) {
        void logCaughtDiagnostic(
          "interface.client.caught_failure_02",
          "A handled interface operation failed.",
          caughtError,
          "client",
        );
        throw new ApiError(
          "Nebula Core returned a malformed chat stream frame.",
          502,
          undefined,
          data,
        );
      }
      if (wire.type === "error") {
        const event: ChatStreamEvent = {
          type: "error",
          detail: wire.detail || "Chat completion failed.",
        };
        onEvent(event);
        throw new ApiError(event.detail, 502, undefined, wire);
      }
      if (wire.type === "started") {
        onEvent({
          type: "started",
          providerId: wire.provider_id ?? body.providerId,
          harnessProfileId: wire.harness_profile_id ?? body.harnessProfileId,
          harnessSessionId: wire.harness_session_id ?? body.harnessSessionId,
          harnessTurnId: wire.harness_turn_id ?? undefined,
          model: wire.model ?? body.model ?? "unknown",
          sessionId: wire.session_id ?? undefined,
          turnId: wire.turn_id ?? undefined,
        });
        return;
      }
      if (wire.type === "delta" || wire.type === "message_delta") {
        onEvent({
          type: wire.type,
          providerId: wire.provider_id ?? body.providerId,
          harnessSessionId: wire.harness_session_id ?? body.harnessSessionId,
          model: wire.model ?? body.model ?? "unknown",
          delta: wire.delta ?? "",
          turnId: wire.turn_id ?? undefined,
        });
        return;
      }
      if (wire.type === "tool_started") {
        const turnId = wire.turn_id ?? wire.harness_turn_id;
        const capability = wire.capability ?? wire.tool_name;
        if (!turnId || !wire.tool_call_id || !capability) return;
        onEvent({
          type: "tool_started",
          turnId,
          toolCallId: wire.tool_call_id,
          capability,
          arguments: wire.arguments ?? wire.payload ?? {},
          step: wire.step ?? 0,
        });
        return;
      }
      if (wire.type === "tool_completed") {
        const turnId = wire.turn_id ?? wire.harness_turn_id;
        const capability = wire.capability ?? wire.tool_name;
        if (!turnId || !wire.tool_call_id || !capability) return;
        const payloadArtifacts = Array.isArray(wire.payload?.artifacts)
          ? (wire.payload.artifacts as WireChatStreamEvent["artifacts"])
          : [];
        const artifacts = wire.artifacts ?? payloadArtifacts ?? [];
        onEvent({
          type: "tool_completed",
          turnId,
          toolCallId: wire.tool_call_id,
          capability,
          status:
            wire.status ??
            (typeof wire.payload?.status === "string"
              ? wire.payload.status
              : "complete"),
          summary:
            wire.summary ??
            (typeof wire.payload?.summary === "string"
              ? wire.payload.summary
              : "Capability completed"),
          evidenceIds: wire.evidence_ids ?? [],
          resultArtifactId:
            wire.result_artifact_id ??
            (typeof wire.payload?.result_artifact_id === "string"
              ? wire.payload.result_artifact_id
              : undefined),
          receipt:
            wire.receipt ??
            (wire.payload?.receipt &&
            typeof wire.payload.receipt === "object" &&
            !Array.isArray(wire.payload.receipt)
              ? (wire.payload.receipt as Record<string, unknown>)
              : undefined),
          artifacts: artifacts.map((artifact) => ({
            artifactId: artifact.artifact_id,
            kind: artifact.kind,
            filename: artifact.filename ?? undefined,
            mediaType: artifact.media_type,
            byteCount: artifact.byte_count,
            observedByteCount: artifact.observed_byte_count,
            sha256: artifact.sha256,
            searchable: artifact.searchable,
            truncated: artifact.truncated,
          })),
          step: wire.step ?? 0,
        });
        return;
      }
      if (wire.type === "approval_required") {
        const turnId = wire.turn_id ?? wire.harness_turn_id;
        if (!turnId || !wire.tool_call_id) return;
        pausedForApproval = true;
        onEvent({
          type: "approval_required",
          turnId,
          toolCallId: wire.tool_call_id,
          approval: wire.approval ?? {
            id: wire.approval_id,
            exact_request: wire.payload ?? {},
          },
        });
        return;
      }
      if (wire.type === "status") {
        onEvent({
          type: "status",
          phase:
            typeof wire.payload?.phase === "string"
              ? wire.payload.phase
              : "working",
          detail:
            typeof wire.payload?.detail === "string"
              ? wire.payload.detail
              : "Harness is working.",
          harnessSessionId: wire.harness_session_id ?? body.harnessSessionId,
          harnessTurnId: wire.harness_turn_id ?? undefined,
          previousSessionId:
            typeof wire.payload?.previous_session_id === "string"
              ? wire.payload.previous_session_id
              : undefined,
        });
        return;
      }
      if (
        [
          "turn_status",
          "item_upsert",
          "output_delta",
          "approval",
          "interaction",
          "checkpoint",
          "notice",
        ].includes(wire.type)
      ) {
        onEvent(mapHarnessActivityEvent(wire) as ChatStreamEvent);
        return;
      }
      if (
        [
          "item_started",
          "item_completed",
          "usage",
          "interrupted",
          "completed",
        ].includes(wire.type)
      ) {
        onEvent({
          type: wire.type as
            | "item_started"
            | "item_completed"
            | "usage"
            | "interrupted"
            | "completed",
          harnessSessionId: wire.harness_session_id ?? body.harnessSessionId,
          harnessTurnId: wire.harness_turn_id ?? undefined,
          payload: wire.payload,
        });
        return;
      }
      if (wire.type === "done") {
        if (
          !wire.model ||
          !wire.message ||
          typeof wire.message === "string" ||
          (!wire.provider_id && !wire.harness_profile_id)
        ) {
          throw new ApiError(
            "Nebula Core returned an incomplete chat completion.",
            502,
            undefined,
            wire,
          );
        }
        completed = mapChatCompletion(wire as unknown as WireChatCompletion);
        onEvent({ type: "done", ...completed });
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let separator = buffer.search(/\r?\n\r?\n/);
      while (separator >= 0) {
        const block = buffer.slice(0, separator);
        const match = buffer.slice(separator).match(/^\r?\n\r?\n/);
        buffer = buffer.slice(separator + (match?.[0].length ?? 2));
        processBlock(block);
        separator = buffer.search(/\r?\n\r?\n/);
      }
      if (done) break;
    }
    if (buffer.trim()) processBlock(buffer);
    if (!completed && !pausedForApproval) {
      throw new ApiError(
        "The chat response ended before a completion was received.",
        502,
      );
    }
    return completed;
  }

  resumeChatTurn(
    turnId: string,
    fallback: ChatCompletionRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<ChatCompletionResponse | undefined> {
    return this.streamChat(fallback, onEvent, signal, turnId);
  }

  getPendingChatTurn(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<ChatTurn | undefined> {
    return this.request<WireChatTurn | null>(
      `chat/sessions/${encodeURIComponent(sessionId)}/pending-turn`,
      { signal },
    ).then((value) => (value ? mapChatTurn(value) : undefined));
  }

  cancelChatTurn(turnId: string): Promise<ChatTurn> {
    return this.request<WireChatTurn>(
      `chat/turns/${encodeURIComponent(turnId)}/cancel`,
      {
        method: "POST",
      },
    ).then(mapChatTurn);
  }
}
