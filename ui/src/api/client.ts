import type {
  AgentRunSummary,
  ApprovalDecisionRequest,
  ApprovalSummary,
  AssetSummary,
  AssetCreateRequest,
  ChatCitation,
  ChatCompletionRequest,
  ChatCompletionResponse,
  ChatSessionSummary,
  ChatStreamEvent,
  EngagementSummary,
  EngagementCreateRequest,
  EvidenceSummary,
  EvidenceUploadRequest,
  EngagementScopePolicy,
  EngagementScopeUpdateRequest,
  FindingCreateRequest,
  FindingSummary,
  HealthResponse,
  KnowledgeIngestRequest,
  KnowledgeSource,
  MissionCreateRequest,
  OperatorProfile,
  OperatorProfileCreateRequest,
  OperatorProfileUpdateRequest,
  Page,
  PersistedChatMessage,
  ProviderCatalogEntry,
  ProviderCreateRequest,
  ProviderHealth,
  ProviderRuntimeHealth,
  ProviderUpdateRequest,
  ReportCreateRequest,
  ReportSummary,
  ReportUpdateRequest,
  RunStopRequest,
  RunnerProfile,
  RunnerProfileUpdateRequest,
  EngagementToolAssignment,
  EngagementToolAssignmentUpdateRequest,
  ToolPackCatalogEntry,
  ToolPackInstallation,
  ToolSummary,
} from "./types";

type JsonObject = Record<string, unknown>;

interface WireEntity extends JsonObject {
  id: string;
  created_at: string;
  updated_at: string;
  revision: number;
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
}

interface WireApproval extends WireEntity {
  engagement_id: string;
  run_id: string;
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
  artifact_ids?: string[];
  signed_off_by?: string | null;
  signed_off_at?: string | null;
  metadata?: JsonObject;
}

interface WireEvidence extends WireEntity {
  engagement_id: string;
  evidence_type: string;
  title: string;
  description?: string;
  artifact_id?: string | null;
  finding_id?: string | null;
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
  session_id?: string | null;
  provider_id: string;
  model: string;
  message: { role: "assistant"; content: string };
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_tokens?: number;
  };
  finish_reason?: string | null;
  provider_request_id?: string | null;
  citations?: WireChatCitation[];
}

interface WireChatStreamEvent extends Partial<WireChatCompletion> {
  type: "started" | "delta" | "done" | "error";
  provider_id?: string;
  model?: string;
  delta?: string;
  detail?: string;
}

interface WireChatSession extends WireEntity {
  engagement_id: string;
  title: string;
  provider_profile_id: string;
  model?: string | null;
  metadata?: JsonObject;
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

interface WireToolPackCatalogEntry extends JsonObject {
  id?: string;
  catalog_id?: string;
  publisher: string;
  name: string;
  version: string;
  description?: string;
  manifest_digest: string;
  minimum_nebula_version?: string | null;
  licenses?: string[];
  platforms?: string[];
  tool_names?: string[];
  permissions?: string[];
  signed?: boolean;
  collection_id?: string | null;
  collection_name?: string | null;
  collection_order?: number;
  interface_catalog_digest?: string | null;
  interface_catalog_protocol?: string | null;
  interface_tool_count?: number | null;
}

interface WireToolPackInstallation extends JsonObject {
  id: string;
  catalog_id?: string | null;
  publisher: string;
  name: string;
  version: string;
  interface_catalog_digest?: string | null;
  manifest_digest: string;
  source?: string;
  trust?: "curated" | "trusted_publisher" | "local_unsigned";
  trust_state?: ToolPackInstallation["trustState"];
  runtime_profile_id?: string | null;
  image_locks?: Record<string, string>;
  status: ToolPackInstallation["status"];
  tool_names?: string[];
  permissions?: string[];
  installed_at?: string | null;
  verified_at?: string | null;
  failure_detail?: string | null;
}

interface WireToolSummary extends JsonObject {
  name: string;
  pack_id: string;
  pack_manifest_digest?: string;
  manifest_digest?: string;
  description?: string;
  risk_class?: ToolSummary["riskClass"];
  requires_network?: boolean;
  network_access?: boolean;
  requires_approval?: boolean;
  available?: boolean;
  unavailable_reason?: string | null;
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
  egress_helper_image?: string | null;
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

interface WireEngagementToolAssignment extends JsonObject {
  id?: string;
  engagement_id: string;
  manifest_digest?: string | null;
  tool_names?: string[];
  allowed_tool_names?: string[];
  enabled?: boolean;
  revision?: number;
  updated_by?: string | null;
  assigned_by?: string | null;
  updated_at?: string | null;
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

  constructor(message: string, status: number, requestId?: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
    this.details = details;
  }
}

function normalizeBaseUrl(value?: string): string {
  const origin = value?.trim() || globalThis.location?.origin || "http://127.0.0.1";
  const withoutSlash = origin.replace(/\/+$/, "");
  return withoutSlash.endsWith("/api/v1") ? withoutSlash : `${withoutSlash}/api/v1`;
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

function configuredDefaultModel(providerType: string, value?: string): string | undefined {
  const model = value?.trim() || undefined;
  if (["anthropic", "bedrock"].includes(providerType.toLowerCase()) && !model) {
    throw new Error(`${providerType === "bedrock" ? "AWS Bedrock" : "Anthropic"} profiles require a default model ID.`);
  }
  return model;
}

function normalizedIdentifiers(values?: string[]): string[] {
  return [...new Set((values ?? []).map((value) => value.trim().toUpperCase()).filter(Boolean))];
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
    spentUsd: typeof value.metadata?.spent_usd === "number" ? value.metadata.spent_usd : undefined,
  };
}

function mapApprovalStatus(value: string): ApprovalSummary["status"] {
  if (value === "edited") return "approved";
  if (["pending", "approved", "rejected", "expired", "cancelled"].includes(value)) {
    return value as ApprovalSummary["status"];
  }
  return "cancelled";
}

function mapApproval(value: WireApproval): ApprovalSummary {
  const request = value.exact_request ?? {};
  const command = Array.isArray(request.argv)
    && request.argv.every((item) => typeof item === "string")
    ? request.argv as string[]
    : undefined;
  return {
    id: value.id,
    runId: value.run_id,
    engagementId: value.engagement_id,
    status: mapApprovalStatus(value.status),
    risk: mapRiskClass(value.risk_class),
    toolName: stringField(request.tool_name) ?? "Tool request",
    agentName: value.requested_by,
    target: value.target ?? "No network target",
    rationale: value.policy_rationale,
    expectedEffects: (value.expected_effects ?? []).join("; ") || "No effects declared",
    arguments: request.arguments && typeof request.arguments === "object"
      ? (request.arguments as JsonObject)
      : {},
    command,
    image: stringField(request.image),
    manifestDigest: stringField(request.manifest_digest),
    credentialClass: value.credential_class ?? undefined,
    expiresAt: value.expires_at ?? undefined,
    createdAt: value.requested_at ?? value.created_at,
  };
}

function mapRiskClass(value: string): ApprovalSummary["risk"] {
  if (value === "credential_use") return "credentialed";
  if (["exploitation", "persistence"].includes(value)) return "exploit";
  if (value === "destructive") return "destructive";
  if (["active_scan", "workspace_write", "scope_change"].includes(value)) return "active";
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
    exposure: value.exposed === true ? "external" : value.exposed === false ? "internal" : "unknown",
    tags: value.tags ?? [],
    serviceCount: typeof value.metadata?.service_count === "number" ? value.metadata.service_count : undefined,
    findingCount: typeof value.metadata?.finding_count === "number" ? value.metadata.finding_count : undefined,
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
  const normalizedStatus = value.status.replaceAll("-", "_") as FindingSummary["status"];
  return {
    id: value.id,
    engagementId: value.engagement_id,
    title: value.title,
    description: value.description ?? "",
    severity: value.severity,
    severityRationale: value.severity_rationale ?? "",
    status: findingStatuses.has(normalizedStatus) ? normalizedStatus : "candidate",
    assetIds: value.asset_ids ?? [],
    evidenceIds: value.evidence_ids ?? [],
    affectedAssetCount: value.asset_ids?.length ?? 0,
    evidenceCount: value.evidence_ids?.length ?? 0,
    cveIds: value.cve_ids ?? [],
    cweIds: value.cwe_ids ?? [],
    verifierId: value.verifier_id ?? undefined,
    verifiedAt: value.verified_at ?? undefined,
    updatedAt: value.updated_at,
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
    artifactIds: value.artifact_ids ?? [],
    signedOffBy: value.signed_off_by ?? undefined,
    signedOffAt: value.signed_off_at ?? undefined,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    revision: value.revision,
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
  const requiresDefaultModel = ["anthropic", "bedrock"].includes(value.provider_type.toLowerCase());
  const state: ProviderHealth["state"] = value.enabled === false
    ? "offline"
    : requiresDefaultModel && !defaultModel
      ? "unconfigured"
      : "unchecked";
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
    modelAllowlist: value.model_allowlist ?? [],
    defaultModel,
    effectiveDefaultModel,
    credentialEnv: value.secret_ref?.startsWith("env:")
      ? value.secret_ref.slice(4)
      : undefined,
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
    message: value.enabled === false
      ? "Provider profile is disabled."
      : requiresDefaultModel && !defaultModel
        ? "Configure a default model before using this provider for chat or missions."
      : "Profile loaded; run a health check to discover available models.",
  };
}

function mapProviderRuntimeHealth(value: WireProviderRuntimeHealth): ProviderRuntimeHealth {
  return {
    providerId: value.provider_id,
    healthy: value.healthy,
    models: value.models ?? [],
    detail: value.detail ?? undefined,
  };
}

function mapProviderCatalog(value: WireProviderCatalogEntry): ProviderCatalogEntry {
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
      chunkCount: typeof metadata.chunk_count === "number" ? metadata.chunk_count : undefined,
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
  return {
    sessionId: value.session_id ?? undefined,
    providerId: value.provider_id,
    model: value.model,
    message: value.message,
    usage: {
      inputTokens,
      outputTokens,
      totalTokens: typeof value.usage?.total_tokens === "number"
        ? value.usage.total_tokens
        : inputTokens + outputTokens,
    },
    finishReason: value.finish_reason ?? undefined,
    providerRequestId: value.provider_request_id ?? undefined,
    citations: (value.citations ?? []).map(mapChatCitation),
  };
}

function chatRequestBody(body: ChatCompletionRequest, stream: boolean): JsonObject {
  return {
    provider_id: body.providerId,
    engagement_id: body.engagementId,
    session_id: body.sessionId,
    model: body.model || undefined,
    messages: body.messages,
    max_output_tokens: body.maxOutputTokens,
    temperature: body.temperature,
    include_knowledge: body.includeKnowledge ?? true,
    allow_cloud_knowledge: body.allowCloudKnowledge ?? false,
    stream,
  };
}

function mapChatSession(value: WireChatSession): ChatSessionSummary {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    title: value.title,
    providerId: value.provider_profile_id,
    model: value.model ?? undefined,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

function mapPersistedChatMessage(value: WirePersistedChatMessage): PersistedChatMessage {
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
    usage: value.usage ? {
      inputTokens,
      outputTokens,
      totalTokens: typeof value.usage.total_tokens === "number"
        ? value.usage.total_tokens
        : inputTokens + outputTokens,
    } : undefined,
    finishReason: value.finish_reason ?? undefined,
    providerRequestId: value.provider_request_id ?? undefined,
    citations: (value.citations ?? []).map(mapChatCitation),
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

function wireItems<T>(value: T[] | { items?: T[]; entries?: T[] }): T[] {
  return Array.isArray(value) ? value : value.items ?? value.entries ?? [];
}

function mapToolCatalogEntry(value: WireToolPackCatalogEntry): ToolPackCatalogEntry {
  return {
    id: value.id ?? value.catalog_id ?? `${value.publisher}/${value.name}@${value.version}`,
    publisher: value.publisher,
    name: value.name,
    version: value.version,
    description: value.description ?? "",
    manifestDigest: value.manifest_digest,
    minimumNebulaVersion: value.minimum_nebula_version ?? undefined,
    licenses: value.licenses ?? [],
    platforms: value.platforms ?? [],
    toolNames: value.tool_names ?? [],
    permissions: value.permissions ?? [],
    signed: value.signed !== false,
    collectionId: value.collection_id ?? undefined,
    collectionName: value.collection_name ?? undefined,
    collectionOrder: numberField(value.collection_order),
    interfaceCatalogDigest: value.interface_catalog_digest ?? undefined,
    interfaceCatalogProtocol: value.interface_catalog_protocol ?? undefined,
    interfaceToolCount: value.interface_tool_count ?? undefined,
  };
}

function mapToolPackInstallation(value: WireToolPackInstallation): ToolPackInstallation {
  const trustState = value.trust_state
    ?? (value.trust === "local_unsigned" ? "developer" : value.trust ? "trusted" : "untrusted");
  return {
    id: value.id,
    catalogId: value.catalog_id ?? undefined,
    publisher: value.publisher,
    name: value.name,
    version: value.version,
    manifestDigest: value.manifest_digest,
    source: value.source ?? "local",
    trustState,
    runtimeProfileId: value.runtime_profile_id ?? undefined,
    imageLocks: value.image_locks ?? {},
    interfaceCatalogDigest: value.interface_catalog_digest ?? undefined,
    status: value.status,
    toolNames: value.tool_names ?? [],
    permissions: value.permissions ?? [],
    installedAt: value.installed_at ?? undefined,
    verifiedAt: value.verified_at ?? undefined,
    failureDetail: value.failure_detail ?? undefined,
  };
}

function mapToolSummary(value: WireToolSummary): ToolSummary {
  return {
    name: value.name,
    packId: value.pack_id,
    packManifestDigest: value.pack_manifest_digest ?? value.manifest_digest ?? "",
    description: value.description ?? "",
    riskClass: value.risk_class ?? "passive",
    requiresNetwork: value.requires_network === true || value.network_access === true,
    requiresApproval: value.requires_approval === true,
    available: value.available === true,
    unavailableReason: value.unavailable_reason ?? undefined,
  };
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
    state: value.state ?? (value.healthy
      ? "ready"
      : value.enabled === false
        ? "unavailable"
        : value.last_health_at || value.last_checked_at
          ? "degraded"
          : "unchecked"),
    lastCheckedAt: value.last_checked_at ?? value.last_health_at ?? undefined,
    detail: value.detail ?? value.last_health_detail ?? undefined,
    egressHelperImage: value.egress_helper_image ?? undefined,
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

function mapToolAssignment(value: WireEngagementToolAssignment): EngagementToolAssignment {
  return {
    id: value.id,
    engagementId: value.engagement_id,
    manifestDigest: value.manifest_digest ?? undefined,
    toolNames: value.tool_names ?? value.allowed_tool_names ?? [],
    enabled: value.enabled === true,
    revision: numberField(value.revision),
    updatedBy: value.updated_by ?? value.assigned_by ?? undefined,
    updatedAt: value.updated_at ?? undefined,
  };
}

async function responseError(response: Response): Promise<ApiError> {
  const text = await response.text();
  let details: unknown = text;
  if (text) {
    try {
      details = JSON.parse(text);
    } catch {
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
    return typeof this.tokenSource === "function" ? this.tokenSource() : this.tokenSource;
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

    const response = await this.fetchImpl(`${this.baseUrl}/${path.replace(/^\//, "")}`, {
      ...init,
      headers,
      credentials: "same-origin",
    });

    if (!response.ok) {
      throw await responseError(response);
    }

    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  private async listAll<T>(resource: string, signal?: AbortSignal, engagementId?: string): Promise<T[]> {
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
        human_pty?: HealthResponse["humanPty"];
      }
    >("health", { signal }).then((health) => ({
      status: health.status === "degraded" ? "degraded" : "ok",
      version: health.version ?? health.api_version ?? "unknown",
      mode: health.mode ?? (health.dialect?.startsWith("postgres") ? "team" : "local"),
      runner: health.runner ?? "unavailable",
      humanPty: health.humanPty ?? health.human_pty ?? "unavailable",
    }));
  }

  listEngagements(signal?: AbortSignal): Promise<Page<EngagementSummary>> {
    return this.listAll<WireEngagement>("engagements", signal)
      .then((items) => page(items.map(mapEngagement)));
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
    return this.request<WireOperatorProfile[]>("operator-profiles", { signal })
      .then((items) => items.map(mapOperatorProfile));
  }

  getActiveOperatorProfile(signal?: AbortSignal): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>("operator-profiles/active", { signal })
      .then(mapOperatorProfile);
  }

  createOperatorProfile(body: OperatorProfileCreateRequest): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>("operator-profiles", {
      method: "POST",
      body: JSON.stringify({ display_name: body.displayName, email: body.email || null, role: body.role || null, metadata: body.metadata ?? {} }),
    }).then(mapOperatorProfile);
  }

  updateOperatorProfile(id: string, body: OperatorProfileUpdateRequest): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>(`operator-profiles/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        ...(body.displayName === undefined ? {} : { display_name: body.displayName }),
        ...(body.email === undefined ? {} : { email: body.email || null }),
        ...(body.role === undefined ? {} : { role: body.role || null }),
        ...(body.metadata === undefined ? {} : { metadata: body.metadata }),
        expected_revision: body.expectedRevision,
      }),
    }).then(mapOperatorProfile);
  }

  activateOperatorProfile(id: string, expectedRevision?: number): Promise<OperatorProfile> {
    return this.request<WireOperatorProfile>(`operator-profiles/${encodeURIComponent(id)}/activate`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    }).then(mapOperatorProfile);
  }

  async deleteOperatorProfile(id: string, expectedRevision?: number): Promise<void> {
    const headers = new Headers();
    if (expectedRevision !== undefined) headers.set("If-Match", String(expectedRevision));
    await this.request<void>(`operator-profiles/${encodeURIComponent(id)}`, { method: "DELETE", headers });
  }

  listRuns(engagementId: string, signal?: AbortSignal): Promise<Page<AgentRunSummary>> {
    return this.listAll<WireAgentRun>("runs", signal, engagementId)
      .then((items) => page(items.map(mapRun)));
  }

  createMission(body: MissionCreateRequest): Promise<AgentRunSummary> {
    return this.request<WireAgentRun>("missions", {
      method: "POST",
      body: JSON.stringify({
        engagement_id: body.engagementId,
        objective: body.objective,
        provider_id: body.providerId,
        model: body.model,
        max_duration_seconds: body.maxDurationSeconds,
        max_tokens: body.maxTokens,
        max_cost_usd: body.maxCostUsd,
        max_retries: body.maxRetries,
        tool_names: body.toolNames ?? [],
        max_tool_calls: body.maxToolCalls ?? 0,
        max_concurrency: body.maxConcurrency ?? 1,
      }),
    }).then(mapRun);
  }

  listToolCatalog(signal?: AbortSignal): Promise<ToolPackCatalogEntry[]> {
    return this.request<WireToolPackCatalogEntry[] | { items?: WireToolPackCatalogEntry[]; entries?: WireToolPackCatalogEntry[] }>("tool-catalog", { signal })
      .then((value) => wireItems(value).map(mapToolCatalogEntry));
  }

  listToolPacks(signal?: AbortSignal): Promise<ToolPackInstallation[]> {
    return this.request<WireToolPackInstallation[] | { items?: WireToolPackInstallation[] }>("tool-packs", { signal })
      .then((value) => wireItems(value).map(mapToolPackInstallation));
  }

  listTools(signal?: AbortSignal): Promise<ToolSummary[]> {
    return this.request<WireToolSummary[] | { items?: WireToolSummary[] }>("tools", { signal })
      .then((value) => wireItems(value).map(mapToolSummary));
  }

  installToolPack(catalogId: string, runtimeProfileId: string, version?: string): Promise<ToolPackInstallation> {
    return this.request<WireToolPackInstallation>("tool-packs/install", {
      method: "POST",
      body: JSON.stringify({ catalog_id: catalogId, version, runtime_profile_id: runtimeProfileId }),
    }).then(mapToolPackInstallation);
  }

  installToolCollection(collectionId: string, runtimeProfileId: string): Promise<ToolPackInstallation[]> {
    return this.request<WireToolPackInstallation[]>("tool-collections/install", {
      method: "POST",
      body: JSON.stringify({ collection_id: collectionId, runtime_profile_id: runtimeProfileId }),
    }).then((items) => items.map(mapToolPackInstallation));
  }

  installLocalToolPack(bundleBase64: string, runtimeProfileId: string, developerModeConfirmed: boolean): Promise<ToolPackInstallation> {
    return this.request<WireToolPackInstallation>("tool-packs/install-local", {
      method: "POST",
      body: JSON.stringify({
        bundle_base64: bundleBase64,
        runtime_profile_id: runtimeProfileId,
        developer_mode_confirmed: developerModeConfirmed,
      }),
    }).then(mapToolPackInstallation);
  }

  verifyToolPack(id: string): Promise<ToolPackInstallation> {
    return this.request<WireToolPackInstallation>(`tool-packs/${encodeURIComponent(id)}/verify`, { method: "POST" })
      .then(mapToolPackInstallation);
  }

  updateToolPack(id: string): Promise<ToolPackInstallation> {
    return this.request<WireToolPackInstallation>(`tool-packs/${encodeURIComponent(id)}/update`, { method: "POST" })
      .then(mapToolPackInstallation);
  }

  async removeToolPack(id: string): Promise<void> {
    await this.request<void>(`tool-packs/${encodeURIComponent(id)}`, { method: "DELETE" });
  }

  listRunnerProfiles(signal?: AbortSignal): Promise<RunnerProfile[]> {
    return this.request<WireRunnerProfile[] | { items?: WireRunnerProfile[] }>("runner-profiles", { signal })
      .then((value) => wireItems(value).map(mapRunnerProfile));
  }

  updateRunnerProfile(id: string, body: RunnerProfileUpdateRequest): Promise<RunnerProfile> {
    return this.request<WireRunnerProfile>(`runner-profiles/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({
        name: body.name,
        runtime: body.runtimeType,
        executable: body.executable,
        context: body.context || null,
        socket: body.socket || null,
        platform: body.platform,
        isolation: body.isolationMode,
        ...(body.egressHelperImage ? { egress_helper_image: body.egressHelperImage } : {}),
        ...(body.seccompProfile ? { seccomp_profile: body.seccompProfile } : {}),
        expected_revision: body.expectedRevision,
      }),
    }).then(mapRunnerProfile);
  }

  getEngagementScope(engagementId: string, signal?: AbortSignal): Promise<EngagementScopePolicy> {
    return this.request<WireEngagementScope>(`engagements/${encodeURIComponent(engagementId)}/scope`, { signal })
      .then(mapEngagementScope);
  }

  updateEngagementScope(engagementId: string, body: EngagementScopeUpdateRequest): Promise<EngagementScopePolicy> {
    return this.request<WireEngagementScope>(`engagements/${encodeURIComponent(engagementId)}/scope`, {
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
    }).then(mapEngagementScope);
  }

  listEngagementToolAssignments(engagementId: string, signal?: AbortSignal): Promise<EngagementToolAssignment[]> {
    return this.request<WireEngagementToolAssignment[] | { items?: WireEngagementToolAssignment[] }>(`engagements/${encodeURIComponent(engagementId)}/tool-assignment`, { signal })
      .then((value) => wireItems(value).map(mapToolAssignment));
  }

  updateEngagementToolAssignment(engagementId: string, body: EngagementToolAssignmentUpdateRequest): Promise<EngagementToolAssignment> {
    return this.request<WireEngagementToolAssignment>(`engagements/${encodeURIComponent(engagementId)}/tool-assignment`, {
      method: "PUT",
      body: JSON.stringify({
        manifest_digest: body.manifestDigest,
        tool_names: body.toolNames,
        enabled: body.enabled,
        expected_revision: body.expectedRevision || undefined,
      }),
    }).then(mapToolAssignment);
  }

  stopRun(id: string, body: RunStopRequest = {}): Promise<AgentRunSummary> {
    return this.request<WireAgentRun>(`runs/${encodeURIComponent(id)}/stop`, {
      method: "POST",
      body: JSON.stringify({ reason: body.reason }),
    }).then(mapRun);
  }

  listApprovals(engagementId: string, signal?: AbortSignal): Promise<Page<ApprovalSummary>> {
    return this.listAll<WireApproval>("approvals", signal, engagementId)
      .then((items) => page(items.map(mapApproval).filter((item) => item.status === "pending")));
  }

  decideApproval(id: string, body: ApprovalDecisionRequest): Promise<ApprovalSummary> {
    return this.request<WireApproval>(`approvals/${encodeURIComponent(id)}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision: body.decision,
        reason: body.reason,
        edited_arguments: body.editedArguments,
      }),
    }).then(mapApproval);
  }

  listAssets(engagementId: string, signal?: AbortSignal): Promise<Page<AssetSummary>> {
    return this.listAll<WireAsset>("assets", signal, engagementId)
      .then((items) => page(items.map(mapAsset)));
  }

  createAsset(body: AssetCreateRequest): Promise<AssetSummary> {
    const exposed = body.exposure === "external"
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

  listFindings(engagementId: string, signal?: AbortSignal): Promise<Page<FindingSummary>> {
    return this.listAll<WireFinding>("findings", signal, engagementId)
      .then((items) => page(items.map(mapFinding)));
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

  listEvidence(engagementId: string, signal?: AbortSignal): Promise<Page<EvidenceSummary>> {
    return this.listAll<WireEvidence>("evidence", signal, engagementId)
      .then((items) => page(items.map(mapEvidence)));
  }

  uploadEvidence(body: EvidenceUploadRequest, signal?: AbortSignal): Promise<EvidenceSummary> {
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
        metadata: body.metadata ?? {},
      }),
    }).then(mapEvidence);
  }

  listReports(engagementId: string, signal?: AbortSignal): Promise<Page<ReportSummary>> {
    return this.listAll<WireReport>("reports", signal, engagementId)
      .then((items) => page(items.map(mapReport)));
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
          ...(body.executiveSummary === undefined ? {} : { executive_summary: body.executiveSummary }),
          ...(body.findingIds === undefined ? {} : { finding_ids: body.findingIds }),
        },
      }),
    }).then(mapReport);
  }

  listProviders(signal?: AbortSignal): Promise<Page<ProviderHealth>> {
    // Provider profiles are global in the current single-organization Core.
    // Applying engagement_id would correctly return no rows.
    return this.listAll<WireProvider>("providers", signal)
      .then((items) => page(items.map(mapProvider)));
  }

  listProviderCatalog(signal?: AbortSignal): Promise<ProviderCatalogEntry[]> {
    return this.request<WireProviderCatalogEntry[]>("provider-catalog", { signal })
      .then((items) => items.map(mapProviderCatalog));
  }

  createProvider(body: ProviderCreateRequest): Promise<ProviderHealth> {
    const defaultModel = configuredDefaultModel(body.providerType, body.defaultModel);
    const modelAllowlist = [...new Set(
      [defaultModel, ...(body.modelAllowlist ?? [])]
        .filter((value): value is string => Boolean(value?.trim()))
        .map((value) => value.trim()),
    )];
    const credentialEnv = body.credentialEnv?.trim().replace(/^env:/, "");
    return this.request<WireProvider>("providers", {
      method: "POST",
      body: JSON.stringify({
        name: body.name.trim(),
        provider_type: body.providerType,
        endpoint: body.endpoint?.trim() || null,
        enabled: true,
        is_local: body.local,
        secret_ref: credentialEnv ? `env:${credentialEnv}` : null,
        model_allowlist: modelAllowlist,
        capabilities: { streaming: true },
        privacy: {
          local_only: body.local,
          permits_sensitive_data: body.permitsSensitiveData === true,
        },
        metadata: {
          ...(defaultModel ? { default_model: defaultModel } : {}),
          ...(body.options && Object.keys(body.options).length ? { options: body.options } : {}),
        },
      }),
    }).then(mapProvider);
  }

  updateProvider(id: string, body: ProviderUpdateRequest): Promise<ProviderHealth> {
    const defaultModel = configuredDefaultModel(body.providerType, body.defaultModel);
    const modelAllowlist = [...new Set(
      [defaultModel, ...body.modelAllowlist]
        .filter((value): value is string => Boolean(value?.trim()))
        .map((value) => value.trim()),
    )];
    const credentialEnv = body.credentialEnv?.trim().replace(/^env:/, "");
    const metadata = { ...(body.metadata ?? {}) };
    delete metadata.default_model;
    delete metadata.options;
    if (defaultModel) metadata.default_model = defaultModel;
    if (body.options && Object.keys(body.options).length) metadata.options = body.options;
    return this.request<WireProvider>(`providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        changes: {
          name: body.name.trim(),
          endpoint: body.endpoint?.trim() || null,
          secret_ref: credentialEnv ? `env:${credentialEnv}` : null,
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

  setProviderEnabled(id: string, enabled: boolean, expectedRevision: number): Promise<ProviderHealth> {
    return this.request<WireProvider>(`providers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ changes: { enabled }, expected_revision: expectedRevision }),
    }).then(mapProvider);
  }

  async deleteProvider(id: string, expectedRevision: number): Promise<void> {
    await this.request<void>(`providers/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { "If-Match": String(expectedRevision) },
    });
  }

  refreshProviderHealth(id: string, signal?: AbortSignal): Promise<ProviderRuntimeHealth> {
    return this.request<WireProviderRuntimeHealth>(
      `providers/${encodeURIComponent(id)}/health`,
      { method: "POST", signal },
    ).then(mapProviderRuntimeHealth);
  }

  listKnowledgeSources(engagementId: string, signal?: AbortSignal): Promise<Page<KnowledgeSource>> {
    return this.listAll<WireKnowledgeSource>("knowledge", signal, engagementId)
      .then((items) => page(items.map(mapKnowledgeSource)));
  }

  ingestKnowledgeSource(body: KnowledgeIngestRequest, signal?: AbortSignal): Promise<KnowledgeSource> {
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

  reindexKnowledgeSource(id: string, signal?: AbortSignal): Promise<KnowledgeSource> {
    return this.request<WireKnowledgeSource>(`knowledge/${encodeURIComponent(id)}/reindex`, {
      method: "POST",
      signal,
    }).then(mapKnowledgeSource);
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

  completeChat(body: ChatCompletionRequest, signal?: AbortSignal): Promise<ChatCompletionResponse> {
    return this.request<WireChatCompletion>("chat/completions", {
      method: "POST",
      signal,
      body: JSON.stringify(chatRequestBody(body, false)),
    }).then(mapChatCompletion);
  }

  listChatSessions(engagementId: string, signal?: AbortSignal): Promise<Page<ChatSessionSummary>> {
    return this.listAll<WireChatSession>("chat-sessions", signal, engagementId)
      .then((items) => page(items.map(mapChatSession)));
  }

  listChatMessages(sessionId: string, signal?: AbortSignal): Promise<PersistedChatMessage[]> {
    return this.request<WirePersistedChatMessage[]>(
      `chat/sessions/${encodeURIComponent(sessionId)}/messages`,
      { signal },
    ).then((items) => items.map(mapPersistedChatMessage));
  }

  async streamChat(
    body: ChatCompletionRequest,
    onEvent: (event: ChatStreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<ChatCompletionResponse> {
    const headers = new Headers({
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await this.fetchImpl(`${this.baseUrl}/chat/completions`, {
      method: "POST",
      headers,
      signal,
      credentials: "same-origin",
      body: JSON.stringify(chatRequestBody(body, true)),
    });
    if (!response.ok) throw await responseError(response);
    if (!response.body) {
      throw new ApiError("The chat response stream was empty.", 502);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let completed: ChatCompletionResponse | undefined;

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
      } catch {
        throw new ApiError("Nebula Core returned a malformed chat stream frame.", 502, undefined, data);
      }
      if (wire.type === "error") {
        const event: ChatStreamEvent = { type: "error", detail: wire.detail || "Chat completion failed." };
        onEvent(event);
        throw new ApiError(event.detail, 502, undefined, wire);
      }
      if (wire.type === "started") {
        onEvent({
          type: "started",
          providerId: wire.provider_id ?? body.providerId,
          model: wire.model ?? body.model ?? "unknown",
          sessionId: wire.session_id ?? undefined,
        });
        return;
      }
      if (wire.type === "delta") {
        onEvent({
          type: "delta",
          providerId: wire.provider_id ?? body.providerId,
          model: wire.model ?? body.model ?? "unknown",
          delta: wire.delta ?? "",
        });
        return;
      }
      if (wire.type === "done") {
        if (!wire.provider_id || !wire.model || !wire.message) {
          throw new ApiError("Nebula Core returned an incomplete chat completion.", 502, undefined, wire);
        }
        completed = mapChatCompletion(wire as WireChatCompletion);
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
    if (!completed) {
      throw new ApiError("The chat response ended before a completion was received.", 502);
    }
    return completed;
  }
}
