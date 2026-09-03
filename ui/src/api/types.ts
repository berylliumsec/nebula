export type Identifier = string;

export type ResourceKind =
  | "project" | "conversation" | "note" | "source" | "library_item"
  | "workspace_file" | "asset" | "evidence" | "finding" | "report"
  | "terminal_session" | "terminal_command" | "browser_session" | "browser_tab"
  | "browser_assessment" | "browser_exchange" | "mission" | "execution"
  | "approval" | "receipt" | "artifact";

export interface ResourceRef {
  projectId?: Identifier;
  kind: ResourceKind;
  id: Identifier;
  revision?: number;
}

export interface ResourceResolution {
  ref: ResourceRef;
  label: string;
  state: "available" | "deleted" | "inaccessible" | "wrong_project";
  actualProjectId?: Identifier;
}

export type RelationPredicate =
  | "affects"
  | "supports"
  | "includes"
  | "references"
  | "produced_by"
  | "derived_from";

export interface ResourceRelation {
  id: Identifier;
  projectId: Identifier;
  source: ResourceRef;
  predicate: RelationPredicate;
  target: ResourceRef;
  attribution?: string;
  provenance: Record<string, unknown>;
  revision: number;
  createdAt: string;
  updatedAt: string;
}

export interface ActionDescriptor {
  id: string;
  acceptedResourceKinds: ResourceKind[];
  resultKind?: ResourceKind;
  authority: "ui" | "core" | "device";
  requiredCapabilities: string[];
  risk: "safe" | "mutating" | "risky";
  confirmationPolicy: "none" | "mutation" | "always";
  available: boolean;
  disabledReason?: string;
}

export interface SearchResult {
  ref: ResourceRef;
  project: string;
  label: string;
  description: string;
  snippet: string;
  breadcrumb: string;
  updatedAt: string;
  score: number;
  actions: ActionDescriptor[];
}

export interface SearchResponse {
  items: SearchResult[];
  nextCursor?: string;
  partialIndex: boolean;
}

export interface HandoffEnvelope {
  id: Identifier;
  projectId: Identifier;
  sourceRefs: ResourceRef[];
  actionId: string;
  targetRef?: ResourceRef;
  originDeviceId: string;
  sourceHashes: Record<string, string>;
  sourceLabels: Record<string, string>;
  transient: boolean;
  status: "pending" | "consumed" | "cancelled" | "expired";
  createdAt: string;
  updatedAt: string;
  expiresAt: string;
  consumedAt?: string;
  consumedByDeviceId?: string;
  revision: number;
}

export interface HandoffResolution {
  envelope: HandoffEnvelope;
  sources: Array<{
    ref: ResourceRef;
    state: "available" | "changed" | "deleted" | "origin_required";
    label: string;
  }>;
  recovery: "ready" | "resume_origin" | "preserve_or_recapture";
}

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
  workspacePath?: string;
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
  workspacePath?: string;
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
    | "interrupted"
    | "complete"
    | "cancelled"
    | "cancelling";
  startedAt?: string;
  updatedAt: string;
  completedTasks: number;
  totalTasks: number;
  spentUsd?: number;
  backend?: "native" | "harness";
  harnessProfileId?: Identifier;
  harnessSessionId?: Identifier;
  model?: string;
  reasoningEffort?: string;
  serviceTier?: string;
  objective?: string;
  finalSummary?: string;
  retryOfRunId?: Identifier;
  remoteMcpConfirmed?: boolean;
  scheduledFor?: string;
  repeatIntervalSeconds?: number;
  stages?: Array<{ title: string; objective: string }>;
}

export interface MissionCreateRequest {
  engagementId: Identifier;
  name: string;
  objective: string;
  backend?: "native" | "harness";
  providerId?: Identifier;
  harnessProfileId?: Identifier;
  harnessSessionId?: Identifier;
  mcpServerIds?: Identifier[];
  model?: string;
  harnessReasoningEffort?: string;
  harnessServiceTier?: string;
  stages?: Array<{ title: string; objective: string }>;
  scheduledFor?: string;
  repeatIntervalSeconds?: number;
  maxDurationSeconds?: number | null;
  maxTokens?: number | null;
  maxCostUsd?: number | null;
  maxRetries?: number;
  maxToolCalls?: number | null;
  maxArtifactQueries?: number | null;
  maxConcurrency?: number;
  allowCloudToolResults?: boolean;
  browserAutonomy?: {
    sessionId: Identifier;
    targets: string[];
    allowedRiskClasses?: string[];
    credentialRefs?: string[];
    durationSeconds?: number;
    maxCommands?: number;
    maxRequests?: number;
    maxBodyBytes?: number;
  };
}

export interface AutomationRuntimeInfo {
  configured: boolean;
  ready: boolean;
  image?: string;
  digest?: string;
  runnerProfileId?: Identifier;
  detail: string;
  inventory: Array<{ name: string; version: string; path: string }>;
}

export interface AutomationProjectPolicy {
  id: Identifier;
  engagementId: Identifier;
  approvalPolicy: "always" | "on_boundary" | "never";
  networkEnabled: boolean;
  runnerProfileId?: Identifier;
  vpnProfileId?: Identifier;
  maxTimeoutMs: number;
  revision: number;
}

export interface VpnProfile {
  id: Identifier;
  name: string;
  filename: string;
  remoteHost: string;
  remotePort: number;
  protocol: "udp" | "tcp";
  fingerprint: string;
  requiresCredentials: boolean;
  available: boolean;
  revision: number;
}

export interface ToolArtifactReference {
  artifactId: Identifier;
  kind:
    | "stdout"
    | "stderr"
    | "parsed"
    | "receipt"
    | "mcp_content"
    | "generated_file";
  filename?: string;
  mediaType: string;
  byteCount: number;
  observedByteCount: number;
  sha256: string;
  searchable: boolean;
  truncated: boolean;
}

export interface ToolOutputLine {
  line: number;
  text: string;
  lineTruncated?: boolean;
}

export interface ToolOutputSearchResult {
  matches: Array<{
    artifactId: Identifier;
    filename?: string;
    line: number;
    context: ToolOutputLine[];
  }>;
  truncated: boolean;
  continuationCursor?: string;
  skipped: Array<{ artifactId: Identifier; reason: string }>;
}

export interface ToolOutputReadResult {
  artifactId: Identifier;
  filename?: string;
  searchable: boolean;
  lines: ToolOutputLine[];
  truncated: boolean;
  continuationStartingLine?: number;
}

export type RunnerRuntime = "podman" | "docker";
export type RunnerIsolation =
  | "rootless"
  | "podman_machine"
  | "docker_desktop_vm"
  | "unverified";

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
  allowAllTargets: boolean;
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

export interface EngagementScopeUpdateRequest
  extends Omit<EngagementScopePolicy, "engagementId" | "revision"> {
  expectedRevision: number;
}

export type BrowserCaptureMode = "metadata" | "headers" | "bodies";
export type BrowserScopeState = "in_scope" | "out_of_scope" | "inactive" | "unconfigured" | "unknown";

export interface SecurityBrowserIdentity {
  id: Identifier;
  name: string;
  description: string;
  color: string;
  storagePartition: string;
  ephemeral: boolean;
  isDefault: boolean;
  revokedAt?: string;
  revision: number;
}

export interface SecurityBrowserTab {
  id: Identifier;
  url?: string;
  title: string;
  position: number;
  lastScopeState: BrowserScopeState;
  lastScopeRevision?: number;
}

export interface SecurityBrowserSession {
  id: Identifier;
  name: string;
  identityId: Identifier;
  status: "active" | "paused" | "closed";
  captureMode: BrowserCaptureMode;
  proxyEnabled: boolean;
  proxyTrustAcknowledged: boolean;
  tabs: SecurityBrowserTab[];
  activeTabId?: Identifier;
  upstreamProxyEnabled: boolean;
  upstreamProxyUrl?: string;
  upstreamProxyCredentialRef?: string;
  interceptionEnabled: boolean;
  deviceOwner?: string;
  lastSeenAt: string;
  revision: number;
}

export interface SecurityBrowserExchange {
  id: Identifier;
  sessionId: Identifier;
  tabId: Identifier;
  identityId: Identifier;
  method: string;
  url: string;
  protocol: "http/1.0" | "http/1.1" | "h2" | "h3" | "websocket" | "unknown";
  statusCode?: number;
  requestHeaders: Record<string, string>;
  responseHeaders: Record<string, string>;
  requestBodyArtifactId?: Identifier;
  responseBodyArtifactId?: Identifier;
  requestBytes?: number;
  responseBytes?: number;
  durationMs?: number;
  scopeState: Exclude<BrowserScopeState, "unknown">;
  scopePolicyRevision: number;
  startedAt: string;
  completedAt?: string;
  replayOfExchangeId?: Identifier;
  error?: string;
  blocked?: boolean;
  truncated: boolean;
}

export interface SecurityBrowserWebSocketFrame {
  id: Identifier;
  sessionId: Identifier;
  exchangeId: Identifier;
  direction: "client" | "server";
  opcode: "text" | "binary" | "ping" | "pong" | "close";
  payloadPreview: string;
  payloadSha256: string;
  payloadBytes: number;
  observedAt: string;
  truncated: boolean;
}

export interface SecurityBrowserAction {
  id: Identifier;
  sessionId: Identifier;
  tabId: Identifier;
  identityId: Identifier;
  kind: "navigate" | "click" | "fill" | "select" | "press" | "extract" | "screenshot" | "replay";
  status: "proposed" | "approved" | "executing" | "complete" | "failed" | "rejected" | "expired";
  locator: Record<string, string>;
  arguments: Record<string, unknown>;
  proposal: string;
  proposedBy: string;
  pageUrl: string;
  scopePolicyRevision: number;
  actionSha256: string;
  approvedBy?: string;
  approvedAt?: string;
  expiresAt: string;
  completedAt?: string;
  result: Record<string, unknown>;
  evidenceIds: Identifier[];
  error?: string;
  revision: number;
}

export interface SecurityBrowserAutomationLease {
  id: Identifier;
  revision: number;
  engagementId: Identifier;
  runId: Identifier;
  sessionId: Identifier;
  identityId: Identifier;
  scopePolicyId: Identifier;
  scopePolicyRevision: number;
  targetUrls: string[];
  allowedRiskClasses: string[];
  credentialRefs: string[];
  maxCommands: number;
  maxRequests: number;
  maxBodyBytes: number;
  commandsUsed: number;
  requestsUsed: number;
  status: "active" | "paused" | "revoked" | "expired" | "complete" | "failed";
  expiresAt: string;
  lastHeartbeatAt: string;
  stopReason?: string;
}

export interface SecurityBrowserCommand {
  id: Identifier;
  revision: number;
  engagementId: Identifier;
  runId: Identifier;
  leaseId: Identifier;
  sessionId: Identifier;
  tabId: Identifier;
  kind: string;
  arguments: Record<string, unknown>;
  expectedPageUrl?: string;
  status: "queued" | "claimed" | "complete" | "failed" | "expired" | "cancelled";
  claimedByDeviceId?: Identifier;
  claimToken?: string;
  expiresAt: string;
  result: Record<string, unknown>;
  evidenceIds: Identifier[];
  error?: string;
}

export interface SecurityBrowserProxyRule {
  id: Identifier;
  revision: number;
  engagementId: Identifier;
  runId: Identifier;
  leaseId: Identifier;
  sessionId: Identifier;
  match: Record<string, unknown>;
  action: Record<string, unknown>;
  priority: number;
  enabled: boolean;
  expiresAt: string;
  disabledReason?: string;
}

export interface SecurityBrowserAutomationStatus {
  leases: SecurityBrowserAutomationLease[];
  commands: SecurityBrowserCommand[];
  rules: SecurityBrowserProxyRule[];
}

export interface SecurityBrowserHandoff {
  id: Identifier;
  sessionId: Identifier;
  requestedByDeviceId: Identifier;
  command: "navigate" | "focus_tab";
  tabId?: Identifier;
  url?: string;
  status: "queued" | "claimed" | "complete" | "failed" | "cancelled" | "expired";
  expiresAt: string;
  claimedByDeviceId?: Identifier;
  error?: string;
  revision: number;
}

export interface SecurityBrowserWorkspace {
  identities: SecurityBrowserIdentity[];
  sessions: SecurityBrowserSession[];
  traffic: SecurityBrowserExchange[];
  frames: SecurityBrowserWebSocketFrame[];
  actions: SecurityBrowserAction[];
  handoffs: SecurityBrowserHandoff[];
}

export type SecurityBrowserAssessmentProfile = "explore" | "standard" | "deep" | "api" | "validation";
export type SecurityBrowserAssessmentStatus = "draft" | "ready" | "running" | "waiting_operator" | "paused" | "stopping" | "stopped" | "complete" | "failed" | "revoked";

export interface SecurityBrowserEngineCapability {
  adapter: string;
  displayName: string;
  contractVersion: string;
  state: "ready" | "degraded" | "preparing" | "unavailable";
  installedVersion?: string;
  digest?: string;
  actions: string[];
  protocols: string[];
  checkFamilies: string[];
  unavailabilityReason?: string;
  recoveryAction?: string;
  desktopOnly: boolean;
}

export interface SecurityBrowserAssessmentBudget {
  maxRequests: number;
  maxActions: number;
  maxDurationSeconds: number;
  maxConcurrency: number;
  requestsUsed: number;
  actionsUsed: number;
}

export interface SecurityBrowserAssessmentCoverage {
  discoveredUrls: number;
  visitedUrls: number;
  analyzedExchanges: number;
  discoveredForms: number;
  discoveredApis: number;
  websocketChannels: number;
}

export interface SecurityBrowserAssessment {
  id: Identifier;
  revision: number;
  engagementId: Identifier;
  name: string;
  objective: string;
  profile: SecurityBrowserAssessmentProfile;
  sessionId: Identifier;
  identityIds: Identifier[];
  primaryIdentityId: Identifier;
  targetUrls: string[];
  scopePolicyId: Identifier;
  scopePolicyRevision: number;
  riskClasses: string[];
  validationGrantId?: Identifier;
  status: SecurityBrowserAssessmentStatus;
  phase: "preflight" | "discovery" | "crawl" | "passive_audit" | "active_audit" | "validation" | "reporting" | "complete";
  progress: number;
  budget: SecurityBrowserAssessmentBudget;
  coverage: SecurityBrowserAssessmentCoverage;
  engines: SecurityBrowserEngineCapability[];
  evidenceIds: Identifier[];
  candidateIds: Identifier[];
  activeStepId?: Identifier;
  controlOwner: "nebula" | "operator";
  pauseReason?: string;
  failure?: string;
  recoveryAction?: string;
  startedAt?: string;
  completedAt?: string;
  createdAt: string;
  updatedAt: string;
}

export interface SecurityBrowserAssessmentStep {
  id: Identifier;
  revision: number;
  assessmentId: Identifier;
  sequence: number;
  title: string;
  intent: string;
  capability: string;
  target: string;
  status: "queued" | "running" | "waiting_operator" | "complete" | "failed" | "cancelled";
  retryClassification: "safe_before_side_effect" | "never" | "operator_review";
  traceIds: Identifier[];
  evidenceIds: Identifier[];
  error?: string;
  recoveryAction?: string;
}

export interface SecurityBrowserScanProfile {
  id: SecurityBrowserAssessmentProfile;
  name: string;
  summary: string;
  riskClasses: string[];
  requiredAdapters: string[];
  defaultBudget: SecurityBrowserAssessmentBudget;
  validationLocked: boolean;
}

export interface SecurityBrowserIssueCandidate {
  id: Identifier;
  revision: number;
  assessmentId: Identifier;
  ruleId: string;
  checkFamily: string;
  title: string;
  cwe?: string;
  targetUrl: string;
  insertionPoint?: string;
  severity: "info" | "low" | "medium" | "high" | "critical";
  confidence: "tentative" | "firm" | "certain";
  evidenceIds: Identifier[];
  validationStatus: "unvalidated" | "queued" | "validating" | "confirmed" | "rejected" | "inconclusive";
  validationGrantId?: Identifier;
  promotedFindingId?: Identifier;
}

export interface SecurityBrowserValidationGrant {
  id: Identifier;
  revision: number;
  assessmentId: Identifier;
  candidateId: Identifier;
  targetUrl: string;
  technique: string;
  maxRequests: number;
  requestsUsed: number;
  durationSeconds: number;
  expiresAt: string;
  status: "active" | "revoked" | "expired" | "consumed";
}

export interface SecurityBrowserAssessmentWorkspace {
  assessments: SecurityBrowserAssessment[];
  steps: SecurityBrowserAssessmentStep[];
  profiles: SecurityBrowserScanProfile[];
  engines: SecurityBrowserEngineCapability[];
  candidates: SecurityBrowserIssueCandidate[];
  validationGrants: SecurityBrowserValidationGrant[];
}

export interface SecurityBrowserSiteNode {
  id: Identifier;
  revision: number;
  sessionId: Identifier;
  identityId: Identifier;
  url: string;
  method: string;
  kind: "page" | "api" | "form" | "resource" | "websocket";
  discoverySource: "browser" | "proxy" | "crawl" | "repeater" | "intruder" | "har" | "automation";
  statusCode?: number;
  parameterNames: string[];
  contentType?: string;
  lastExchangeId?: Identifier;
  evidenceIds: Identifier[];
  firstSeenAt: string;
  lastSeenAt: string;
}

export interface SecurityBrowserIntercept {
  id: Identifier;
  revision: number;
  sessionId: Identifier;
  tabId: Identifier;
  transactionId: string;
  phase: "request" | "response";
  method: string;
  url: string;
  statusCode?: number;
  headers: Array<[string, string]>;
  state: "paused" | "forwarded" | "dropped" | "interrupted" | "expired";
  expiresAt: string;
  error?: string;
}

export interface SecurityBrowserCrawlJob {
  id: Identifier;
  revision: number;
  sessionId: Identifier;
  identityId: Identifier;
  startUrl: string;
  state: "draft" | "queued" | "running" | "paused" | "complete" | "cancelled" | "failed";
  maxDepth: number;
  maxRequests: number;
  maxConcurrency: number;
  maxDurationSeconds: number;
  maxBodyBytes: number;
  requestsCompleted: number;
  nodesDiscovered: number;
  checkpoint: number;
  frontier: Array<[string, number]>;
  visitedUrls: string[];
  error?: string;
}

export interface SecurityBrowserRepeaterTab {
  id: Identifier;
  revision: number;
  sessionId: Identifier;
  identityId: Identifier;
  name: string;
  group: string;
  notes: string;
  protocol: "http" | "websocket";
  method: string;
  url: string;
  headers: Array<[string, string]>;
  bodyTemplate: string;
  sourceExchangeId?: Identifier;
  historyExchangeIds: Identifier[];
  evidenceIds: Identifier[];
  state: "draft" | "queued" | "running" | "ready" | "cancelled" | "failed";
  requestCount: number;
  error?: string;
}

export interface SecurityBrowserRepeaterResult {
  id: Identifier;
  revision: number;
  tabId: Identifier;
  sequence: number;
  exchangeId?: Identifier;
  statusCode?: number;
  responseHeaders: Array<[string, string]>;
  responseBytes?: number;
  durationMs?: number;
  responseBodyArtifactId?: Identifier;
  error?: string;
  createdAt: string;
}

export interface SecurityBrowserAttack {
  id: Identifier;
  revision: number;
  sessionId: Identifier;
  identityId: Identifier;
  name: string;
  strategy: "sniper" | "battering_ram" | "pitchfork" | "cluster_bomb";
  method: string;
  urlTemplate: string;
  headersTemplate: Array<[string, string]>;
  bodyTemplate: string;
  positions: string[];
  payloadSets: Array<Record<string, unknown>>;
  transforms: string[];
  state: "draft" | "queued" | "running" | "paused" | "complete" | "cancelled" | "failed";
  maxRequests: number;
  maxConcurrency: number;
  requestsPerSecond: number;
  requestCount: number;
  errorCount: number;
  error?: string;
}

export interface SecurityBrowserAttackResult {
  id: Identifier;
  attackId: Identifier;
  sequence: number;
  payloads: string[];
  exchangeId?: Identifier;
  statusCode?: number;
  responseBytes?: number;
  durationMs?: number;
  error?: string;
  evidenceIds: Identifier[];
}

export interface SecurityBrowserTokenAnalysis {
  id: Identifier;
  sessionId: Identifier;
  name: string;
  sampleCount: number;
  tokenLengthMin: number;
  tokenLengthMax: number;
  uniqueCount: number;
  collisionCount: number;
  shannonBitsPerCharacter: number;
  characterFrequencies: Record<string, number>;
}

export interface SecurityBrowserResearchWorkspace {
  siteNodes: SecurityBrowserSiteNode[];
  crawlJobs: SecurityBrowserCrawlJob[];
  intercepts: SecurityBrowserIntercept[];
  repeaterTabs: SecurityBrowserRepeaterTab[];
  repeaterResults: SecurityBrowserRepeaterResult[];
  attacks: SecurityBrowserAttack[];
  attackResults: SecurityBrowserAttackResult[];
  tokenAnalyses: SecurityBrowserTokenAnalysis[];
}

export interface ScopeImportCandidate {
  id: Identifier;
  targetType: "cidr" | "domain" | "url";
  classification: "allowed" | "excluded" | "ambiguous";
  rawValue: string;
  normalizedValue?: string;
  sourceLocation: string;
  sourceExcerpt: string;
  warnings: string[];
}

export interface ScopeImportProvenance {
  backendKind?: "provider" | "harness";
  providerProfileId: Identifier;
  harnessProfileId?: Identifier;
  model: string;
  promptVersion: string;
  sourceSha256: string;
  generatedAt: string;
  providerRequestIds: string[];
}

export interface ScopeImport {
  id: Identifier;
  engagementId: Identifier;
  artifactId: Identifier;
  filename: string;
  sourceType: string;
  sourceSha256: string;
  baseScopeRevision: number;
  status: "generating" | "ready" | "applied" | "discarded" | "failed";
  candidates: ScopeImportCandidate[];
  warnings: string[];
  provenance?: ScopeImportProvenance;
  usage: ChatUsage;
  errorDetail?: string;
  appliedCandidateIds: string[];
  appliedScopePolicyId?: string;
  appliedScopeRevision?: number;
  revision: number;
}

export interface ScopeImportCreateRequest {
  engagementId: Identifier;
  backendKind?: "provider" | "harness";
  providerId?: Identifier;
  harnessProfileId?: Identifier;
  model: string;
  filename: string;
  mediaType?: string;
  contentBase64: string;
  cloudConfirmed: boolean;
}

export interface ScopeImportApplyResult {
  scope: EngagementScopePolicy;
  scopeImport: ScopeImport;
}

export interface RunStopRequest {
  reason?: string;
}

export type ApprovalDecision = "approve" | "reject" | "stop";

export interface ApprovalSummary {
  id: Identifier;
  runId: Identifier;
  engagementId: Identifier;
  origin?: "mission" | "chat";
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
  runtimeDigest?: string;
  credentialClass?: string;
  expiresAt?: string;
  createdAt: string;
  argumentEditing?: boolean;
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
  revision: number;
}

export interface FindingCreateRequest {
  engagementId: Identifier;
  title: string;
  description?: string;
  severity: FindingSummary["severity"];
  severityRationale?: string;
  assetIds?: Identifier[];
  evidenceIds?: Identifier[];
  cveIds?: string[];
  cweIds?: string[];
  sourceRunId?: Identifier;
}

export interface FindingUpdateRequest {
  title?: string;
  description?: string;
  severity?: FindingSummary["severity"];
  severityRationale?: string;
  assetIds?: Identifier[];
  cveIds?: string[];
  cweIds?: string[];
  status?: FindingStatus;
  evidenceIds?: Identifier[];
  verifierId?: Identifier;
  verifiedAt?: string;
  expectedRevision: number;
}

export interface ReportSummary {
  id: Identifier;
  engagementId: Identifier;
  title: string;
  status: string;
  executiveSummary: string;
  findingIds: string[];
  observationIds: string[];
  noteTransforms: ReportNoteTransform[];
  artifactIds: string[];
  executiveSummaryProvenance?: AIWritingProvenance;
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
  noteTransforms?: ReportNoteTransform[];
  sourceRunId?: Identifier;
}

export interface ReportUpdateRequest {
  title?: string;
  status?: string;
  executiveSummary?: string;
  findingIds?: string[];
  observationIds?: string[];
  noteTransforms?: ReportNoteTransform[];
  executiveSummaryProvenance?: AIWritingProvenance | null;
  expectedRevision: number;
}

export interface AIWritingProvenance {
  backendKind?: "provider" | "harness";
  providerProfileId: Identifier;
  harnessProfileId?: Identifier;
  model: string;
  promptVersion: string;
  sourceSha256: string;
  instruction: string;
  generatedAt: string;
  providerRequestId?: string;
}

export interface ReportNoteTransform {
  observationId: Identifier;
  sourceRevision: number;
  title: string;
  body: string;
  provenance: AIWritingProvenance;
}

export interface WritingTransformRequest {
  engagementId: Identifier;
  backendKind?: "provider" | "harness";
  providerId?: Identifier;
  harnessProfileId?: Identifier;
  model: string;
  purpose: "note" | "report_summary" | "report_section" | "code_suggestion";
  instruction: string;
  sourceText: string;
  cloudConfirmed?: boolean;
}

export interface WritingTransformResponse {
  content: string;
  provenance: AIWritingProvenance;
  usage: ChatUsage;
}

export interface CodeCompletionItem {
  label: string;
  type?: string;
  detail?: string;
}

export interface ObservationSummary {
  id: Identifier;
  engagementId: Identifier;
  observationType: string;
  title: string;
  body: string;
  assetIds: Identifier[];
  serviceIds: Identifier[];
  evidenceIds: Identifier[];
  source?: string;
  confidence: number;
  metadata: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  revision: number;
}

export interface ObservationCreateRequest {
  engagementId: Identifier;
  observationType?: string;
  title: string;
  body?: string;
  assetIds?: Identifier[];
  serviceIds?: Identifier[];
  evidenceIds?: Identifier[];
  source?: string;
  confidence?: number;
  metadata?: Record<string, unknown>;
}

export interface ObservationUpdateRequest {
  title?: string;
  body?: string;
  assetIds?: Identifier[];
  serviceIds?: Identifier[];
  evidenceIds?: Identifier[];
  confidence?: number;
  metadata?: Record<string, unknown>;
  expectedRevision: number;
}

export interface ObservationReportDependency {
  id: Identifier;
  title: string;
  status: "draft" | "review" | "final";
}

export interface ObservationDependencies {
  observationId: Identifier;
  deletable: boolean;
  reports: ObservationReportDependency[];
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
  nextStep?: {
    title: string;
    rationale: string;
    command: string;
    language: ExecutionLanguage;
    networkTarget?: string;
    networkPorts: number[];
  };
}

export interface PostToolAssistantConfig {
  suggestNextSteps: boolean;
  takeNotes: boolean;
  providerId?: Identifier;
  backendKind: "provider" | "harness";
  harnessProfileId?: Identifier;
  model?: string;
  cloudConfirmed: boolean;
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
  parentArtifactId?: Identifier;
  sourceContext?: Record<string, unknown>;
  editRecipe?: Record<string, unknown>;
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
  availableModels?: string[];
  modelAllowlist: string[];
  defaultModel?: string;
  effectiveDefaultModel?: string;
  credentialEnv?: string;
  credentialRef?: string;
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
  capabilityVerifications?: Record<string, ProviderCapabilityVerification>;
  message?: string;
}

export interface ProviderCapabilityVerification {
  model: string;
  status: "verified" | "failed";
  checkedAt: string;
  contractVersion: string;
  failureDetail?: string;
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

export interface LocalProviderDetection {
  flavor: string;
  displayName: string;
  endpoint: string;
  models: string[];
}

export interface ProviderCreateRequest {
  name: string;
  providerType: string;
  endpoint?: string;
  local: boolean;
  defaultModel?: string;
  modelAllowlist?: string[];
  credentialEnv?: string;
  credentialRef?: string;
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
  credentialRef?: string;
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
  contentBlocks?: ChatContentBlock[];
}

export interface ChatContentBlock {
  type: "text" | "code" | "image" | "artifact" | "citation" | "activity";
  text?: string;
  language?: string;
  artifactId?: Identifier;
  mediaType?: string;
  alt?: string;
  activityId?: Identifier;
  metadata?: Record<string, unknown>;
}

export interface PairedDevice {
  id: Identifier;
  name: string;
  createdAt: string;
  lastUsedAt: string;
  idleExpiresAt: string;
  absoluteExpiresAt: string;
  current: boolean;
  platform?: string;
  appVersion?: string;
  capabilities?: string[];
  ownershipClaims?: ResourceRef[];
  heartbeatAt?: string;
  healthy?: boolean;
}

export interface DeviceCapabilitySnapshot {
  platform: string;
  appVersion: string;
  capabilities: string[];
  ownershipClaims: ResourceRef[];
  heartbeatAt?: string;
  expectedRevision?: number;
}

export type ActionIntentStatus =
  | "queued" | "claimed" | "prepared" | "committed" | "succeeded" | "failed"
  | "compensating" | "compensated" | "reconcile_required" | "cancelled" | "expired";

export interface ActionIntent {
  id: Identifier;
  projectId: Identifier;
  resources: ResourceRef[];
  actionId: string;
  requester: string;
  eligibleDeviceIds: Identifier[];
  selectedDeviceId?: Identifier;
  idempotencyKey: string;
  expectedRevisions: Record<string, number>;
  logicalLeaseKey: string;
  leaseExpiresAt?: string;
  status: ActionIntentStatus;
  expiresAt: string;
  preparedAt?: string;
  committedAt?: string;
  receipt?: Record<string, unknown>;
  resultRefs: ResourceRef[];
  error?: string;
  coreMutationCommitted: boolean;
  revision: number;
}

export interface ChatContextAttachment {
  sourceKind: string;
  sourceId?: Identifier;
  sourceLabel: string;
  text: string;
  sha256: string;
  truncated: boolean;
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

export interface HarnessDetailedUsage extends ChatUsage {
  cachedInputTokens: number;
  cacheCreationTokens: number;
  cacheReadTokens: number;
  reasoningTokens: number;
  costUsd: number;
  durationMs?: number;
  apiDurationMs?: number;
  turnCount: number;
  contextUsedTokens?: number;
  contextLimitTokens?: number;
  modelUsage: Record<string, Record<string, unknown>>;
  rateLimit: Record<string, unknown>;
}

export type HarnessActivityItemKind =
  | "reasoning"
  | "plan"
  | "command"
  | "file_change"
  | "tool"
  | "web_search"
  | "browser"
  | "image"
  | "skill"
  | "subagent"
  | "hook"
  | "review"
  | "compaction"
  | "mode"
  | "goal";

export interface HarnessPlanEntry {
  id: string;
  title: string;
  status: "pending" | "in_progress" | "completed" | "blocked";
}

export interface HarnessGoalSnapshot {
  objective: string;
  status: "pending" | "running" | "complete" | "blocked" | "failed";
  progress?: number;
  currentStep?: string;
  elapsedMs?: number;
  tokenBudget?: number;
  tokensUsed?: number;
  childAgents: number;
}

export interface HarnessActivityEvent {
  schemaVersion: "nebula.harness-activity/v1" | "nebula.harness-activity/v2";
  id?: Identifier;
  sequence?: number;
  type: string;
  vendor?: "codex_app_server" | "claude_agent_sdk" | "grok_acp";
  harnessSessionId?: Identifier;
  harnessTurnId?: Identifier;
  externalSessionId?: string;
  externalTurnId?: string;
  itemId?: string;
  parentItemId?: string;
  itemKind?: HarnessActivityItemKind;
  itemStatus?: string;
  title?: string;
  summary?: string;
  serverId?: string;
  toolName?: string;
  stream?: string;
  delta?: string;
  message?: string;
  usage?: ChatUsage;
  detailedUsage?: HarnessDetailedUsage;
  artifactIds: Identifier[];
  payload: Record<string, unknown>;
  occurredAt?: string;
  mode?: string;
  plan?: HarnessPlanEntry[];
  goal?: HarnessGoalSnapshot;
}

export interface HarnessActivityEventPage {
  events: HarnessActivityEvent[];
  nextSequence: number;
}

export interface HarnessTurnDetail {
  id: Identifier;
  status:
    | "queued"
    | "running"
    | "waiting_approval"
    | "complete"
    | "failed"
    | "cancelled"
    | "interrupted";
  origin: "chat" | "mission";
  harnessSessionId: Identifier;
  chatSessionId?: Identifier;
  runId?: Identifier;
  error?: string;
  retryOfTurnId?: Identifier;
}

export interface ChatCompletionRequest {
  backend?: "provider" | "harness";
  providerId?: Identifier;
  harnessProfileId?: Identifier;
  harnessSessionId?: Identifier;
  mcpServerIds?: Identifier[];
  engagementId?: Identifier;
  sessionId?: Identifier;
  model?: string;
  messages: ChatMessage[];
  contextAttachments?: ChatContextAttachment[];
  maxOutputTokens?: number;
  temperature?: number;
  includeKnowledge?: boolean;
  allowCloudKnowledge?: boolean;
  toolsEnabled?: boolean;
  maxArtifactQueries?: number;
  allowCloudToolResults?: boolean;
  harnessMode?: string;
  harnessReasoningEffort?: string;
  harnessServiceTier?: string;
  harnessSkill?: HarnessSkillInvocation;
}

export interface ChatCompletionResponse {
  turnId?: Identifier;
  sessionId?: Identifier;
  backend?: "provider" | "harness";
  providerId?: Identifier;
  harnessProfileId?: Identifier;
  harnessSessionId?: Identifier;
  harnessTurnId?: Identifier;
  model: string;
  message: ChatMessage;
  usage: ChatUsage;
  contextUsage?: ChatUsage;
  finishReason?: string;
  providerRequestId?: string;
  citations: ChatCitation[];
}

export interface ContextSourceReference {
  sourceKind: string;
  sourceId: Identifier;
  sequence?: number;
}

export interface ContextMemoryItem {
  text: string;
  sources: ContextSourceReference[];
}

export interface ContextMemory {
  objective?: string;
  summary: string;
  confirmedFacts: ContextMemoryItem[];
  decisions: ContextMemoryItem[];
  constraints: ContextMemoryItem[];
  corrections: ContextMemoryItem[];
  openQuestions: ContextMemoryItem[];
  evidenceIds: Identifier[];
  artifactIds: Identifier[];
}

export interface ContextSnapshot {
  id: Identifier;
  ownerType: "chat_session" | "agent_run";
  ownerId: Identifier;
  version: number;
  status: "ready" | "failed";
  compactedThrough: number;
  memory?: ContextMemory;
  sourceReferences: ContextSourceReference[];
  providerId: Identifier;
  model: string;
  promptVersion: string;
  usage: ChatUsage;
  costUsd: number;
  error?: string;
  createdAt: string;
}

export interface ContextStatus {
  ownerType: "chat_session" | "agent_run";
  ownerId: Identifier;
  status: "not_needed" | "ready" | "stale" | "failed" | "runtime_managed";
  contextWindow: number;
  maxOutputTokens: number;
  targetInputTokens: number;
  estimatedInputTokens: number;
  compactedThrough: number;
  sourceReferences: ContextSourceReference[];
  compactionUsage: ChatUsage;
  compactionCostUsd: number;
  snapshot?: ContextSnapshot;
}

export type ChatStreamEvent =
  | {
      type: "started";
      providerId?: Identifier;
      harnessProfileId?: Identifier;
      harnessSessionId?: Identifier;
      harnessTurnId?: Identifier;
      model: string;
      sessionId?: Identifier;
      turnId?: Identifier;
    }
  | {
      type: "delta" | "message_delta";
      providerId?: Identifier;
      harnessSessionId?: Identifier;
      model: string;
      delta: string;
      turnId?: Identifier;
    }
  | {
      type: "tool_started";
      turnId: Identifier;
      toolCallId: Identifier;
      capability: string;
      arguments: Record<string, unknown>;
      step: number;
    }
  | {
      type: "tool_completed";
      turnId: Identifier;
      toolCallId: Identifier;
      capability: string;
      status: string;
      summary: string;
      evidenceIds: Identifier[];
      resultArtifactId?: Identifier;
      artifacts: ToolArtifactReference[];
      receipt?: Record<string, unknown>;
      step: number;
    }
  | {
      type: "approval_required";
      turnId: Identifier;
      toolCallId: Identifier;
      approval: Record<string, unknown>;
    }
  | {
      type: "status";
      phase: string;
      detail: string;
      harnessSessionId?: Identifier;
      harnessTurnId?: Identifier;
      previousSessionId?: Identifier;
    }
  | ({
      type:
        | "turn_status"
        | "item_upsert"
        | "output_delta"
        | "approval"
        | "interaction"
        | "checkpoint"
        | "notice";
    } & HarnessActivityEvent)
  | {
      type:
        | "item_started"
        | "item_completed"
        | "usage"
        | "interrupted"
        | "completed";
      harnessSessionId?: Identifier;
      harnessTurnId?: Identifier;
      payload?: Record<string, unknown>;
    }
  | ({ type: "done" } & ChatCompletionResponse)
  | { type: "error"; detail: string };

export interface ChatTurn {
  id: Identifier;
  sessionId: Identifier;
  status:
    | "routing"
    | "waiting_approval"
    | "finalizing"
    | "complete"
    | "failed"
    | "cancelled"
    | "interrupted";
  approvalId?: Identifier;
  harnessTurnId?: Identifier;
  toolCallIds: Identifier[];
}

export interface ChatSessionSummary {
  id: Identifier;
  engagementId: Identifier;
  title: string;
  backend: "provider" | "harness";
  providerId?: Identifier;
  harnessProfileId?: Identifier;
  harnessSessionId?: Identifier;
  parentSessionId?: Identifier;
  forkedFromMessageId?: Identifier;
  model?: string;
  toolsEnabled: boolean;
  createdAt: string;
  updatedAt: string;
  revision: number;
}

export interface HarnessProfile {
  id: Identifier;
  name: string;
  // Claude remains a wire-compatible legacy value but is not a provided harness.
  kind: "codex_app_server" | "claude_agent_sdk" | "grok_acp";
  connectionMode: "spawn" | "endpoint";
  transport: "stdio" | "unix" | "websocket";
  executable?: string;
  endpoint?: string;
  authMode: "existing_session" | "secret_ref" | "endpoint_bearer";
  secretRef?: string;
  defaultModel?: string;
  models: string[];
  modelOptions?: HarnessModelOptions[];
  enabled: boolean;
  localOnly: boolean;
  permitsSensitiveData: boolean;
  nativeCapabilities: HarnessNativeCapabilities;
  healthy?: boolean;
  version?: string;
  detail?: string;
  capabilities?: HarnessCapabilities;
  revision: number;
}

export interface HarnessRuntimeOption {
  id: string;
  label: string;
  description: string;
}

export interface HarnessModelOptions {
  model: string;
  reasoningEfforts: HarnessRuntimeOption[];
  defaultReasoningEffort?: string;
  serviceTiers: HarnessRuntimeOption[];
  defaultServiceTier?: string;
}

export interface HarnessCapabilities {
  activityReplay: boolean;
  reasoningSummaries: boolean;
  plans: boolean;
  planningMode: boolean;
  goalMonitoring: boolean;
  skillInvocation: boolean;
  modes: string[];
  liveCommandOutput: boolean;
  fileDiffs: boolean;
  detailedUsage: boolean;
  interactions: boolean;
  hooks: boolean;
  subagentActivity: boolean;
  subagentControl: boolean;
  checkpointRewind: boolean;
  steering: boolean;
  interruption: boolean;
}

export interface HarnessSkillInvocation {
  name: string;
  path: string;
}

export interface HarnessSkillSummary extends HarnessSkillInvocation {
  source: "project" | "installed";
}

export interface HarnessInteraction {
  id: Identifier;
  harnessTurnId: Identifier;
  status: "pending" | "answered" | "declined" | "cancelled" | "expired";
  kind: "user_input" | "mcp_elicitation";
  prompt: string;
  questions: Record<string, unknown>[];
  responseSchema: Record<string, unknown>;
  containsSecret: boolean;
  createdAt: string;
}

export interface HarnessNativeCapabilities {
  workspaceAccess: "none" | "read" | "write";
  shell: boolean;
  webSearch: boolean;
  webFetch: boolean;
  browser: boolean;
  computerUse: boolean;
  imageGeneration: boolean;
  skills: boolean;
  subagents: boolean;
}

export interface McpToolProfile {
  name: string;
  description: string;
  readOnly: boolean;
  destructive: boolean;
  openWorld: boolean;
  credentialed?: boolean;
  approval: "risk_based" | "allow" | "ask" | "deny";
}

export interface McpServerProfile {
  id: Identifier;
  name: string;
  transport: "stdio" | "streamable_http";
  command?: string;
  arguments: string[];
  url?: string;
  authMode: "none" | "bearer" | "headers";
  enabled: boolean;
  required: boolean;
  trustedStdio: boolean;
  defaultApproval: "risk_based" | "allow" | "ask" | "deny";
  toolOverrides: Record<string, "risk_based" | "allow" | "ask" | "deny">;
  tools: McpToolProfile[];
  checkedAt?: string;
  detail?: string;
  revision: number;
}

export interface HarnessSessionSummary {
  id: Identifier;
  engagementId: Identifier;
  harnessProfileId: Identifier;
  model: string;
  reasoningEffort?: string;
  serviceTier?: string;
  status:
    | "starting"
    | "idle"
    | "running"
    | "waiting_approval"
    | "closed"
    | "failed"
    | "interrupted";
  mcpServerIds: Identifier[];
  lastActivityAt: string;
}

export interface HarnessSessionActivity {
  sessionId: Identifier;
  sessionStatus: HarnessSessionSummary["status"];
  busy: boolean;
  live: boolean;
  turnId?: Identifier;
  turnStatus?:
    | "queued"
    | "running"
    | "waiting_approval"
    | "complete"
    | "failed"
    | "cancelled"
    | "interrupted";
  turnOrigin?: "chat" | "mission";
  startedAt?: string;
  lastActivityAt: string;
  detail: string;
  mode?: string;
  plan?: HarnessPlanEntry[];
  goal?: HarnessGoalSnapshot;
}

export interface ChatSessionRenameRequest {
  title: string;
  expectedRevision?: number;
}

export interface PersistedChatMessage extends ChatMessage {
  id: Identifier;
  engagementId: Identifier;
  sessionId: Identifier;
  sequence: number;
  sourceMessageId?: Identifier;
  providerId?: Identifier;
  model?: string;
  usage?: ChatUsage;
  finishReason?: string;
  providerRequestId?: string;
  citations: ChatCitation[];
  contextAttachments: ChatContextAttachment[];
  harnessTurnId?: Identifier;
  toolResults?: ChatToolResult[];
  createdAt: string;
  updatedAt: string;
}

export interface ChatToolResult {
  toolCallId: Identifier;
  capability: string;
  status: string;
  summary?: string;
  evidenceIds: Identifier[];
  resultArtifactId?: Identifier;
  receipt: Record<string, unknown>;
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
    origin?: string;
    sourceUrl?: string;
    fetchedAt?: string;
    [key: string]: unknown;
  };
}

export interface LibraryItem {
  id: Identifier;
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
    scope?: string;
    [key: string]: unknown;
  };
}

export interface LibraryIngestRequest {
  filename: string;
  mediaType?: string;
  contentBase64: string;
}

export interface KnowledgeIngestRequest {
  engagementId: Identifier;
  filename: string;
  mediaType?: string;
  contentBase64: string;
}

export interface KnowledgeUrlIngestRequest {
  engagementId: Identifier;
  url: string;
}

export interface KnowledgeIndexStatus {
  backend: string;
  state: "disabled" | "required" | "downloading" | "preparing" | "ready" | "error";
  model: string;
  downloadedBytes: number;
  totalBytes: number;
  detail?: string;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  mode: "local" | "team";
  runner: "ready" | "unavailable" | "degraded";
  containerTerminal: "configured" | "unavailable";
  diagnosticsDegraded?: boolean;
  browserDiagnosticIngress: "enabled" | "disabled";
}

export interface RunnerCandidate {
  candidateId?: Identifier;
  runnerProfileId?: Identifier;
  source: "configured" | "detected";
  name: string;
  runtime: "podman" | "docker";
  executable: string;
  context?: string;
  platform: "linux/amd64" | "linux/arm64";
  isolation: "rootless" | "podman_machine" | "docker_desktop_vm";
  healthy: boolean;
  detail?: string;
}

export interface SetupImagePreparation {
  phase:
    | "not_started"
    | "queued"
    | "resolving_runtime"
    | "preparing_image"
    | "ready"
    | "cancelling"
    | "cancelled"
    | "error";
  operationId?: Identifier;
  projectId?: Identifier;
  progressPercent?: number;
  progressIndeterminate: boolean;
  canCancel: boolean;
  canRetry: boolean;
  imageDigest?: string;
  startedAt?: string;
  completedAt?: string;
  detail?: string;
}

export interface SetupStatus {
  applicationStage:
    | "starting_core"
    | "migrating"
    | "loading_project"
    | "detecting_runner"
    | "preparing_image"
    | "loading_workspace"
    | "ready"
    | "degraded"
    | "failed";
  stageDetail: string;
  stageStartedAt?: string;
  retryable: boolean;
  recoveryActions: Array<{ id: string; label: string; destination?: string }>;
  core: {
    status: "ready" | "degraded" | "error";
    detail?: string;
  };
  scratchProjectId?: Identifier;
  terminal: {
    status:
      | "detecting_runner"
      | "needs_runner"
      | "preparing_image"
      | "ready"
      | "disabled"
      | "error";
    runnerProfileId?: Identifier;
    candidates: RunnerCandidate[];
    imagePreparation: SetupImagePreparation;
    detail?: string;
  };
  assistant: {
    status: "needs_model" | "configured" | "error";
    providerProfileId?: Identifier;
    detail?: string;
  };
}

export interface SetupControlResponse {
  operation:
    | "runner_selection"
    | "image_preparation"
    | "image_preparation_retry"
    | "image_preparation_cancellation";
  accepted: boolean;
  idempotent: boolean;
  operationId?: Identifier;
  setup: SetupStatus;
}

export interface CredentialStatus {
  reference: string;
  persistence: "environment" | "vault" | "session";
  available: boolean;
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
  kind: "assistant_message" | "rerun" | "selection";
  messageId?: Identifier;
  blockOrdinal?: number;
  blockSha256?: string;
  selectionStartByte?: number;
  selectionEndByte?: number;
  executionId?: Identifier;
  sourceKind?: string;
  sourceId?: Identifier;
  sourceLabel?: string;
  sourceSha256?: string;
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
  runtimeDigest: string;
  image: string;
  runnerProfileId: Identifier;
  runnerProfileRevision: number;
  runnerRuntime: "docker" | "podman";
  runnerIsolation: string;
  runnerExecutable: string;
  runnerPlatform: string;
  runnerContext?: string;
  runnerSocket?: string;
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

export interface ContainerTerminalRequest {
  engagementId: Identifier;
  columns: number;
  rows: number;
  publishedPorts?: ContainerTerminalPublishedPort[];
}

export interface ContainerTerminalPublishedPort {
  port: number;
  protocol: "tcp" | "udp";
}

export interface ContainerTerminalRuntimeSnapshot {
  sourceImage: string;
  baseImage: string;
  baseImageDigest: string;
  image: string;
  imageDigest: string;
  installedPackages: string[];
  interpreter: string;
  arguments: string[];
  runnerProfileId: Identifier;
  runnerProfileRevision: number;
  runnerRuntime: "docker" | "podman";
  runnerIsolation: string;
  runnerExecutable: string;
  runnerPlatform: string;
  runnerContext?: string;
}

export interface ContainerTerminalNetworkSnapshot {
  mode: "unrestricted";
  runtimeNetwork: "bridge";
  publishedPorts: ContainerTerminalPublishedPort[];
}

export interface ContainerTerminalSecuritySnapshot {
  containerUser: "root";
  rootFilesystem: "writable";
  linuxCapabilities: string[];
  noNewPrivileges: boolean;
  hostNetwork: boolean;
  runtimeSocket: boolean;
  hostShell: boolean;
}

export interface ContainerTerminalCapabilities {
  engagementId: Identifier;
  ready: boolean;
  detail?: string;
  errorCode?: string;
  workspaceEntries?: number;
  workspaceMaxEntries?: number;
  sourceImage: string;
  installedPackages: string[];
  network: ContainerTerminalNetworkSnapshot;
  security: ContainerTerminalSecuritySnapshot;
  workspace: "/workspace";
  limits: ExecutionLimits;
  idleTimeoutSeconds: number;
  freshContainer: true;
}

export interface ContainerTerminalPreflight {
  allowed: boolean;
  errorCode?: string;
  detail: string;
  runtime?: ContainerTerminalRuntimeSnapshot;
  network: ContainerTerminalNetworkSnapshot;
  security: ContainerTerminalSecuritySnapshot;
  limits: ExecutionLimits;
  workspace: "/workspace";
  policyRule?: string;
  previewFingerprint?: string;
  previewToken?: string;
  expiresAt?: string;
  idleTimeoutSeconds: number;
  freshContainer: true;
}

export interface ContainerTerminalSession {
  sessionId: Identifier;
  createdAt: string;
  websocketTicket: string;
  ticketExpiresAt: string;
  websocketPath: string;
  reconnectGraceSeconds: number;
  replayMaxBytes: number;
  lastSequence: number;
}

export interface ContainerTerminalRecovery {
  active: boolean;
  session?: ContainerTerminalSession;
  runtime?: ContainerTerminalRuntimeSnapshot;
}

export interface ContainerTerminalRecoveredSession {
  session: ContainerTerminalSession;
  runtime: ContainerTerminalRuntimeSnapshot;
}

export interface ContainerTerminalRecoveryList {
  sessions: ContainerTerminalRecoveredSession[];
}

export interface ContainerTerminalCapacity {
  activeSessions: number;
  availableSessions: number;
  maxActiveSessions: number;
}

export interface TerminalCommandRecord {
  id: Identifier;
  engagementId: Identifier;
  sessionId: Identifier;
  operatorId?: Identifier;
  shellSequence?: string;
  command: string;
  commandSha256?: string;
  cwd: string;
  status:
    | "completed"
    | "interrupted"
    | "framing_lost"
    | "capture_failed"
    | "legacy_metadata_only";
  exitCode?: number;
  startedAt?: string;
  completedAt?: string;
  occurredAt: string;
  rawOutputAvailable: boolean;
  redactedOutputAvailable: boolean;
  observedOutputBytes: number;
  capturedOutputBytes: number;
  outputSha256?: string;
  outputTruncated: boolean;
  outputPreview: string;
  captureError?: string;
  captureDecision:
    | "selected_tool"
    | "not_selected"
    | "classification_failed"
    | "capture_failed"
    | "legacy_all_commands"
    | "legacy_metadata_only";
  matchedTools: string[];
  recordingPolicyRevision?: number;
  runtimeImageDigest?: string;
}

export interface TerminalCommandPage {
  records: TerminalCommandRecord[];
  total: number;
  offset: number;
  limit: number;
  nextOffset?: number;
}

export interface TerminalCommandHistoryStatus {
  engagementId: Identifier;
  enabled: boolean;
  captureMode: "selected_tools";
  recordCount: number;
  recordedOutputCount: number;
  metadataOnlyCount: number;
  classificationFailureCount: number;
  degradedCount: number;
  truncatedCount: number;
  auditGapCount: number;
  capturedOutputBytes: number;
  retentionDays?: number;
  maxRecords?: number;
  oldestRecordedAt?: string;
  newestRecordedAt?: string;
}

export interface TerminalRecordingTools {
  engagementId: Identifier;
  inventoryStatus: "verified" | "unavailable";
  runtimeImageDigest?: string;
  manifestSha256?: string;
  defaultTools: string[];
  customTools: string[];
  disabledTools: string[];
  effectiveTools: string[];
  revision: number;
  updatedAt?: string;
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

export interface WorkspaceSearchMatch {
  path: string;
  kind: "path" | "content";
  line?: number;
  column?: number;
  preview: string;
}

export interface WorkspaceSearchResult {
  engagementId: Identifier;
  query: string;
  mode: "files" | "text";
  matches: WorkspaceSearchMatch[];
  scannedFiles: number;
  truncated: boolean;
}

export interface WorkspaceTask {
  id: string;
  label: string;
  command: string;
  kind: "test" | "build" | "run" | "lint" | "custom";
  source: "package.json" | "Makefile" | "pytest" | "go.mod" | "Cargo.toml" | ".vscode/tasks.json";
  detail: string;
  path?: string;
  supported: boolean;
  unsupportedReason?: string;
}

export interface WorkspaceTaskList {
  engagementId: Identifier;
  tasks: WorkspaceTask[];
  scannedEntries: number;
  truncated: boolean;
}

export interface WorkspaceDebugConfiguration {
  id: string;
  name: string;
  path?: string;
  arguments: string[];
  source: ".vscode/launch.json";
  detail: string;
  supported: boolean;
  unsupportedReason?: string;
}

export interface WorkspaceDebugConfigurationList {
  engagementId: Identifier;
  activePath: string;
  configurations: WorkspaceDebugConfiguration[];
  truncated: boolean;
}

export interface DebugSessionStart {
  sessionId: string;
  websocketPath: string;
  websocketTicket: string;
  protocol: "nebula.debug.v1";
  path: string;
  sourceSha256: string;
  imageDigest: string;
  workspaceAccess: "read-only";
  network: "none";
  expiresAt: string;
}

export type SourceControlFileStatus =
  | "unmodified"
  | "modified"
  | "added"
  | "deleted"
  | "renamed"
  | "copied"
  | "unmerged"
  | "untracked"
  | "ignored"
  | "unknown";

export interface SourceControlFile {
  path: string;
  indexStatus: SourceControlFileStatus;
  worktreeStatus: SourceControlFileStatus;
  originalPath?: string;
}

export interface SourceControlStatus {
  engagementId: Identifier;
  state: "ready" | "not_repository" | "unavailable";
  branch?: string;
  head?: string;
  files: SourceControlFile[];
  truncated: boolean;
  detail: string;
}

export interface SourceControlDiff {
  engagementId: Identifier;
  path: string;
  staged: boolean;
  text: string;
  truncated: boolean;
  head?: string;
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

export interface WorkspaceResetStatus {
  engagementId: Identifier;
  canReset: boolean;
  activeTerminalCount: number;
  activeExecutionCount: number;
  reasonCode?: "workspace_busy" | "linked_workspace";
  detail: string;
}

export interface WorkspaceUploadResult {
  engagementId: Identifier;
  path: string;
  size: number;
  sha256: string;
  overwritten: boolean;
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
  | "stage.completed"
  | "task.created"
  | "task.started"
  | "task.turn_completed"
  | "task.continuing"
  | "task.completed"
  | "task.verified"
  | "task.verification_failed"
  | "task.blocked"
  | "task.retry_scheduled"
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
  | "system.notice"
  | `harness.${string}`;

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
