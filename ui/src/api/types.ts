export type Identifier = string;

export interface Page<T> {
  items: T[];
  total: number;
  nextCursor?: string;
}

export interface EngagementSummary {
  id: Identifier;
  name: string;
  description: string;
  clientName?: string;
  status: "draft" | "active" | "paused" | "complete" | "archived";
  tags: string[];
  createdAt: string;
  updatedAt: string;
  scopeAssetCount: number;
}

export interface EngagementCreateRequest {
  name: string;
  description?: string;
  clientName?: string;
  status?: EngagementSummary["status"];
  tags?: string[];
}

export interface AgentRunSummary {
  id: Identifier;
  engagementId: Identifier;
  title: string;
  status:
    | "queued"
    | "planning"
    | "running"
    | "waiting_approval"
    | "paused"
    | "failed"
    | "complete"
    | "cancelled"
    | "cancelling";
  startedAt?: string;
  updatedAt: string;
  completedTasks: number;
  totalTasks: number;
  spentUsd?: number;
}

export interface MissionCreateRequest {
  engagementId: Identifier;
  objective: string;
  providerId: Identifier;
  model: string;
  maxDurationSeconds?: number;
  maxTokens?: number;
  maxCostUsd?: number;
  maxRetries?: number;
  toolNames?: string[];
  maxToolCalls?: number;
  maxConcurrency?: number;
}

export type ToolPackStatus = "pending" | "pulling" | "verifying" | "ready" | "failed" | "disabled";

export interface ToolPackCatalogEntry {
  id: Identifier;
  publisher: string;
  name: string;
  version: string;
  description: string;
  manifestDigest: string;
  minimumNebulaVersion?: string;
  licenses: string[];
  platforms: string[];
  toolNames: string[];
  permissions: string[];
  signed: boolean;
  collectionId?: string;
  collectionName?: string;
  collectionOrder: number;
  interfaceCatalogDigest?: string;
  interfaceCatalogProtocol?: string;
  interfaceToolCount?: number;
}

export interface ToolPackInstallation {
  id: Identifier;
  catalogId?: Identifier;
  publisher: string;
  name: string;
  version: string;
  manifestDigest: string;
  source: string;
  trustState: "trusted" | "developer" | "untrusted" | "invalid";
  runtimeProfileId?: Identifier;
  imageLocks: Record<string, string>;
  interfaceCatalogDigest?: string;
  status: ToolPackStatus;
  toolNames: string[];
  permissions: string[];
  installedAt?: string;
  verifiedAt?: string;
  failureDetail?: string;
}

export interface ToolSummary {
  name: string;
  packId: Identifier;
  packManifestDigest: string;
  description: string;
  riskClass: "local_read" | "passive" | "active_scan" | "workspace_write" | "credential_use" | "exploitation" | "persistence" | "destructive" | "scope_change";
  requiresNetwork: boolean;
  requiresApproval: boolean;
  available: boolean;
  unavailableReason?: string;
}

export type RunnerRuntime = "podman" | "docker";
export type RunnerIsolation = "rootless" | "podman_machine" | "docker_desktop_vm" | "unverified";

export interface RunnerProfile {
  id: Identifier;
  name: string;
  runtimeType: RunnerRuntime;
  executable: string;
  context?: string;
  socket?: string;
  platform: string;
  isolationMode: RunnerIsolation;
  state: "ready" | "degraded" | "unavailable" | "unchecked";
  lastCheckedAt?: string;
  detail?: string;
  egressHelperImage?: string;
  seccompProfile?: string;
  revision: number;
}

export interface RunnerProfileUpdateRequest {
  name: string;
  runtimeType: RunnerRuntime;
  executable: string;
  context?: string;
  socket?: string;
  platform: string;
  isolationMode: Exclude<RunnerIsolation, "unverified">;
  egressHelperImage?: string;
  seccompProfile?: string;
  expectedRevision?: number;
}

export interface EngagementScopePolicy {
  id?: Identifier;
  engagementId: Identifier;
  allowedCidrs: string[];
  allowedDomains: string[];
  allowedUrls: string[];
  allowedPorts: number[];
  notBefore?: string;
  notAfter?: string;
  prohibitedActions: string[];
  localOnly: boolean;
  maxConcurrency: number;
  grants: MissionGrant[];
  revision: number;
}

export interface MissionGrant {
  riskClasses: string[];
  toolNames: string[];
  targets: string[];
  grantedAt: string;
  expiresAt: string;
  grantedBy: string;
}

export interface EngagementScopeUpdateRequest extends Omit<EngagementScopePolicy, "engagementId" | "revision"> {
  expectedRevision: number;
}

export interface EngagementToolAssignment {
  id?: Identifier;
  engagementId: Identifier;
  manifestDigest?: string;
  toolNames: string[];
  enabled: boolean;
  revision: number;
  updatedBy?: string;
  updatedAt?: string;
}

export interface EngagementToolAssignmentUpdateRequest {
  manifestDigest: string;
  toolNames: string[];
  enabled: boolean;
  expectedRevision?: number;
}

export interface RunStopRequest {
  reason?: string;
}

export type ApprovalDecision = "approve" | "reject" | "stop";

export interface ApprovalSummary {
  id: Identifier;
  runId: Identifier;
  engagementId: Identifier;
  status: "pending" | "approved" | "rejected" | "expired" | "cancelled";
  risk: "passive" | "active" | "credentialed" | "exploit" | "destructive";
  toolName: string;
  agentName: string;
  target: string;
  rationale: string;
  expectedEffects: string;
  arguments: Record<string, unknown>;
  command?: string[];
  image?: string;
  manifestDigest?: string;
  credentialClass?: string;
  expiresAt?: string;
  createdAt: string;
}

export interface ApprovalDecisionRequest {
  decision: ApprovalDecision;
  reason?: string;
  editedArguments?: Record<string, unknown>;
}

export interface AssetSummary {
  id: Identifier;
  engagementId: Identifier;
  displayName: string;
  kind: "host" | "domain" | "url" | "cloud" | "repository" | "other";
  address?: string;
  hostname?: string;
  criticality: "critical" | "high" | "medium" | "low" | "info";
  exposure: "external" | "internal" | "unknown";
  tags: string[];
  serviceCount?: number;
  findingCount?: number;
  lastSeenAt?: string;
  createdAt: string;
  updatedAt: string;
}

export interface AssetCreateRequest {
  engagementId: Identifier;
  name: string;
  kind: AssetSummary["kind"];
  address?: string;
  hostname?: string;
  criticality?: AssetSummary["criticality"];
  exposure?: AssetSummary["exposure"];
  tags?: string[];
}

export type FindingStatus =
  | "candidate"
  | "validated"
  | "confirmed"
  | "accepted_risk"
  | "false_positive"
  | "remediated"
  | "retest_passed"
  | "retest_failed";

export interface FindingSummary {
  id: Identifier;
  engagementId: Identifier;
  title: string;
  description: string;
  severity: "critical" | "high" | "medium" | "low" | "info";
  severityRationale: string;
  status: FindingStatus;
  assetIds: string[];
  evidenceIds: string[];
  affectedAssetCount: number;
  evidenceCount: number;
  cveIds: string[];
  cweIds: string[];
  verifierId?: string;
  verifiedAt?: string;
  updatedAt: string;
}

export interface FindingCreateRequest {
  engagementId: Identifier;
  title: string;
  description?: string;
  severity: FindingSummary["severity"];
  severityRationale?: string;
  assetIds?: Identifier[];
  cveIds?: string[];
  cweIds?: string[];
}

export interface ReportSummary {
  id: Identifier;
  engagementId: Identifier;
  title: string;
  status: string;
  executiveSummary: string;
  findingIds: string[];
  observationIds: string[];
  artifactIds: string[];
  signedOffBy?: string;
  signedOffAt?: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
}

export interface ReportCreateRequest {
  engagementId: Identifier;
  title: string;
  status?: string;
  executiveSummary?: string;
  findingIds?: string[];
  observationIds?: string[];
}

export interface ReportUpdateRequest {
  title?: string;
  status?: string;
  executiveSummary?: string;
  findingIds?: string[];
  observationIds?: string[];
  expectedRevision: number;
}

export interface ObservationSummary {
  id: Identifier;
  engagementId: Identifier;
  observationType: string;
  title: string;
  body: string;
  evidenceIds: Identifier[];
  createdAt: string;
  updatedAt: string;
}

export interface ReportRender {
  id: Identifier;
  engagementId: Identifier;
  reportId: Identifier;
  reportRevision: number;
  inputFingerprint: string;
  templateVersion: string;
  rendererVersion: string;
  status: "queued" | "rendering" | "completed" | "failed" | "interrupted";
  warnings: string[];
  generatedAt?: string;
  errorDetail?: string;
  revision: number;
}

export interface PotentialFindingDraft {
  title: string;
  rationale: string;
}

export interface GeneratedDraftContent {
  title: string;
  summary: string;
  observations: string[];
  potentialFindings: PotentialFindingDraft[];
  evidenceIds: Identifier[];
}

export interface GeneratedDraft {
  id: Identifier;
  engagementId: Identifier;
  executionId: Identifier;
  providerProfileId: Identifier;
  model: string;
  promptVersion: string;
  contextFingerprint: string;
  status: "generating" | "ready" | "accepted" | "rejected" | "failed";
  content?: GeneratedDraftContent;
  observationId?: Identifier;
  providerRequestId?: string;
  errorDetail?: string;
  metadata: Record<string, unknown>;
  revision: number;
}

export interface ExecutionChatAttachment {
  sessionId: Identifier;
  contextFingerprint: string;
  categories: string[];
}

export interface EvidenceSummary {
  id: Identifier;
  engagementId: Identifier;
  evidenceType: string;
  title: string;
  description: string;
  artifactId?: Identifier;
  findingId?: Identifier;
  executionId?: Identifier;
  assetIds: Identifier[];
  sha256?: string;
  capturedAt: string;
  capturedBy?: string;
  sourceVersion?: string;
  createdAt: string;
  updatedAt: string;
  metadata: {
    filename?: string;
    mediaType?: string;
    size?: number;
    source?: string;
    [key: string]: unknown;
  };
}

export interface EvidenceUploadRequest {
  engagementId: Identifier;
  filename: string;
  title: string;
  evidenceType: string;
  contentBase64: string;
  mediaType?: string;
  description?: string;
  source?: string;
  findingId?: Identifier;
  assetIds?: Identifier[];
  capturedBy?: string;
  sourceVersion?: string;
  metadata?: Record<string, unknown>;
}

export interface OperatorProfile {
  id: Identifier;
  displayName: string;
  email?: string;
  role?: string;
  active: boolean;
  activatedAt?: string;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  revision: number;
}

export interface OperatorProfileCreateRequest {
  displayName: string;
  email?: string;
  role?: string;
  metadata?: Record<string, unknown>;
}

export interface OperatorProfileUpdateRequest {
  displayName?: string;
  email?: string;
  role?: string;
  metadata?: Record<string, unknown>;
  expectedRevision?: number;
}

export interface ProviderHealth {
  id: Identifier;
  revision: number;
  name: string;
  providerType: string;
  kind: "commercial" | "local" | "gateway";
  local: boolean;
  state: "healthy" | "degraded" | "offline" | "unconfigured" | "unchecked";
  enabled: boolean;
  endpoint?: string;
  models: string[];
  modelAllowlist: string[];
  defaultModel?: string;
  effectiveDefaultModel?: string;
  credentialEnv?: string;
  permitsSensitiveData: boolean;
  retention?: string;
  residency: string[];
  options: Record<string, unknown>;
  metadata: Record<string, unknown>;
  modelCount: number;
  latencyMs?: number;
  privacy: "local_only" | "regional" | "cloud";
  lastCheckedAt?: string;
  capabilities: string[];
  message?: string;
}

export interface ProviderRuntimeHealth {
  providerId: Identifier;
  healthy: boolean;
  models: string[];
  detail?: string;
}

export interface ProviderCatalogEntry {
  flavor: string;
  adapter: string;
  displayName: string;
  local: boolean;
  defaultBaseUrl?: string;
  suggestedKeyEnv?: string;
  supportTier: "native" | "standard" | "compatible" | "gateway";
  notes?: string;
}

export interface ProviderCreateRequest {
  name: string;
  providerType: string;
  endpoint?: string;
  local: boolean;
  defaultModel?: string;
  modelAllowlist?: string[];
  credentialEnv?: string;
  permitsSensitiveData?: boolean;
  options?: Record<string, unknown>;
}

export interface ProviderUpdateRequest {
  name: string;
  providerType: string;
  endpoint?: string;
  local: boolean;
  defaultModel?: string;
  modelAllowlist: string[];
  credentialEnv?: string;
  permitsSensitiveData: boolean;
  retention?: string;
  residency: string[];
  options?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  expectedRevision: number;
}

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  id?: Identifier;
  role: ChatRole;
  content: string;
}

export interface ChatCitation {
  sourceId: Identifier;
  name: string;
  citation?: string;
  artifactId?: Identifier;
  chunkId: string;
  page?: number;
  excerpt: string;
}

export interface ChatUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
}

export interface ChatCompletionRequest {
  providerId: Identifier;
  engagementId?: Identifier;
  sessionId?: Identifier;
  model?: string;
  messages: ChatMessage[];
  maxOutputTokens?: number;
  temperature?: number;
  includeKnowledge?: boolean;
  allowCloudKnowledge?: boolean;
}

export interface ChatCompletionResponse {
  sessionId?: Identifier;
  providerId: Identifier;
  model: string;
  message: ChatMessage;
  usage: ChatUsage;
  finishReason?: string;
  providerRequestId?: string;
  citations: ChatCitation[];
}

export type ChatStreamEvent =
  | { type: "started"; providerId: Identifier; model: string; sessionId?: Identifier }
  | { type: "delta"; providerId: Identifier; model: string; delta: string }
  | ({ type: "done" } & ChatCompletionResponse)
  | { type: "error"; detail: string };

export interface ChatSessionSummary {
  id: Identifier;
  engagementId: Identifier;
  title: string;
  providerId: Identifier;
  model?: string;
  createdAt: string;
  updatedAt: string;
}

export interface PersistedChatMessage extends ChatMessage {
  id: Identifier;
  engagementId: Identifier;
  sessionId: Identifier;
  sequence: number;
  providerId?: Identifier;
  model?: string;
  usage?: ChatUsage;
  finishReason?: string;
  providerRequestId?: string;
  citations: ChatCitation[];
  createdAt: string;
  updatedAt: string;
}

export interface KnowledgeSource {
  id: Identifier;
  engagementId: Identifier;
  name: string;
  sourceType: string;
  artifactId?: Identifier;
  status: string;
  citation?: string;
  documentCount: number;
  createdAt: string;
  updatedAt: string;
  metadata: {
    filename?: string;
    mediaType?: string;
    size?: number;
    sha256?: string;
    chunkCount?: number;
    indexedAt?: string;
    [key: string]: unknown;
  };
}

export interface KnowledgeIngestRequest {
  engagementId: Identifier;
  filename: string;
  mediaType?: string;
  contentBase64: string;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  mode: "local" | "team";
  runner: "ready" | "unavailable" | "degraded";
}

export type ExecutionLanguage = "bash" | "sh" | "python";
export type ExecutionStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "completed"
  | "denied"
  | "timed_out"
  | "cancelled"
  | "failed"
  | "interrupted";

export interface ExecutionOrigin {
  kind: "assistant_message" | "rerun";
  messageId?: Identifier;
  blockOrdinal?: number;
  blockSha256?: string;
  selectionStartByte?: number;
  selectionEndByte?: number;
  executionId?: Identifier;
}

export interface ExecutionNetworkRequest {
  mode: "none" | "scoped";
  target?: string;
  ports: number[];
}

export interface ExecutionRequest {
  engagementId: Identifier;
  language: string;
  source: string;
  origin: ExecutionOrigin;
  network: ExecutionNetworkRequest;
}

export interface ExecutionRuntimeSnapshot {
  language: ExecutionLanguage;
  interpreter: string;
  arguments: string[];
  toolPackInstallationId: Identifier;
  manifestDigest: string;
  image: string;
  runnerProfileId: Identifier;
  runnerProfileRevision: number;
  runnerRuntime: "docker" | "podman";
  runnerIsolation: string;
  runnerExecutable: string;
  runnerPlatform: string;
  runnerContext?: string;
  runnerSocket?: string;
  trusted: boolean;
}

export interface ExecutionNetworkSnapshot {
  mode: "none" | "scoped";
  target?: string;
  ports: number[];
  resolvedAddresses: string[];
  scopePolicyId?: Identifier;
  scopePolicyRevision?: number;
}

export interface ExecutionLimits {
  cpuCount: number;
  memoryMb: number;
  pids: number;
  timeoutSeconds: number;
  outputBytesPerStream: number;
}

export interface ExecutionPreflight {
  allowed: boolean;
  errorCode?: string;
  detail: string;
  canonicalLanguage?: ExecutionLanguage;
  sourceSha256?: string;
  runtime?: ExecutionRuntimeSnapshot;
  network?: ExecutionNetworkSnapshot;
  limits: ExecutionLimits;
  workspace: "/workspace";
  policyRule?: string;
  previewFingerprint?: string;
  previewToken?: string;
  expiresAt?: string;
}

export interface ExecutionCapability {
  language: ExecutionLanguage;
  aliases: string[];
  offline: boolean;
  scopedNetwork: boolean;
  detail?: string;
}

export interface ExecutionCapabilities {
  engagementId: Identifier;
  ready: boolean;
  runtimes: ExecutionCapability[];
  limits: ExecutionLimits;
  workspace: "/workspace";
}

export interface WorkspaceChange {
  path: string;
  change: "added" | "modified" | "deleted";
  size?: number;
}

export interface OperatorExecution {
  id: Identifier;
  engagementId: Identifier;
  operatorId: Identifier;
  origin: ExecutionOrigin;
  language: ExecutionLanguage;
  sourceSha256: string;
  sourceArtifactId: Identifier;
  sourcePreview: string;
  runtime: ExecutionRuntimeSnapshot;
  network: ExecutionNetworkSnapshot;
  limits: ExecutionLimits;
  workspace: "/workspace";
  policyDecision: string;
  status: ExecutionStatus;
  errorCode?: string;
  errorDetail?: string;
  queuedAt: string;
  startedAt?: string;
  completedAt?: string;
  exitCode?: number;
  outputTruncated: boolean;
  evidenceId?: Identifier;
  workspaceChanges: WorkspaceChange[];
}

export interface ExecutionOutputPage {
  text: string;
  totalBytes: number;
  nextOffset: number;
}

export interface WorkspaceEntry {
  path: string;
  name: string;
  kind: "file" | "directory" | "symlink" | "other";
  size: number;
  modifiedAt: string;
}

export interface WorkspaceListing {
  engagementId: Identifier;
  path: string;
  entries: WorkspaceEntry[];
  offset: number;
  nextOffset?: number;
  total: number;
}

export interface WorkspacePreview {
  engagementId: Identifier;
  path: string;
  text: string;
  bytesReturned: number;
  truncated: boolean;
  previewSha256: string;
}

export interface WorkspaceResetResult {
  engagementId: Identifier;
  removedEntries: number;
}

export type RunEventKind =
  | "run.created"
  | "run.queued"
  | "run.started"
  | "run.planned"
  | "run.waiting_approval"
  | "run.stop_requested"
  | "run.completed"
  | "run.failed"
  | "run.cancelled"
  | "run.status_changed"
  | "task.created"
  | "task.started"
  | "task.completed"
  | "task.verified"
  | "task.failed"
  | "task.cancelled"
  | "task.status_changed"
  | "agent.message"
  | "tool.requested"
  | "tool.started"
  | "tool.completed"
  | "tool.failed"
  | "approval.requested"
  | "approval.resolved"
  | "finding.created"
  | "finding.updated"
  | "evidence.created"
  | "system.notice";

export interface RunEvent<T = Record<string, unknown>> {
  sequence: number;
  id: Identifier;
  kind: RunEventKind;
  engagementId?: Identifier;
  runId?: Identifier;
  actor?: string;
  occurredAt: string;
  summary: string;
  payload: T;
}

export interface EventCursor {
  after: number;
  engagementId?: Identifier;
  runId?: Identifier;
}
