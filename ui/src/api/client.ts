import type {
  AgentRunSummary,
  ApprovalDecisionRequest,
  ApprovalSummary,
  AssetSummary,
  EngagementSummary,
  FindingSummary,
  HealthResponse,
  Page,
  ProviderCatalogEntry,
  ProviderCreateRequest,
  ProviderHealth,
  ProviderRuntimeHealth,
} from "./types";

type JsonObject = Record<string, unknown>;

interface WireEntity extends JsonObject {
  id: string;
  created_at: string;
  updated_at: string;
}

interface WireEngagement extends WireEntity {
  name: string;
  client_name?: string | null;
  status: EngagementSummary["status"];
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
  exposed?: boolean | null;
  metadata?: JsonObject;
}

interface WireFinding extends WireEntity {
  engagement_id: string;
  title: string;
  severity: FindingSummary["severity"];
  status: string;
  asset_ids?: string[];
  evidence_ids?: string[];
  cve_ids?: string[];
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
    residency?: string[];
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
  support_tier: ProviderCatalogEntry["supportTier"];
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

function page<T>(items: T[]): Page<T> {
  return { items, total: items.length };
}

function engagementQuery(engagementId: string): string {
  return `engagement_id=${encodeURIComponent(engagementId)}`;
}

function mapEngagement(value: WireEngagement): EngagementSummary {
  return {
    id: value.id,
    name: value.name,
    clientName: value.client_name ?? undefined,
    status: value.status,
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
    exposure: value.exposed === true ? "external" : value.exposed === false ? "internal" : "unknown",
    serviceCount: numberField(value.metadata?.service_count),
    findingCount: numberField(value.metadata?.finding_count),
    lastSeenAt: stringField(value.metadata?.last_seen_at),
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
    severity: value.severity,
    status: findingStatuses.has(normalizedStatus) ? normalizedStatus : "candidate",
    affectedAssetCount: value.asset_ids?.length ?? 0,
    evidenceCount: value.evidence_ids?.length ?? 0,
    cveIds: value.cve_ids ?? [],
    updatedAt: value.updated_at,
  };
}

function mapProvider(value: WireProvider): ProviderHealth {
  const capabilities = Object.entries(value.capabilities ?? {})
    .filter(([, supported]) => supported)
    .map(([name]) => name.replaceAll("_", " "));
  const isGateway = ["gateway", "openrouter", "litellm"].some((name) =>
    value.provider_type.toLowerCase().includes(name),
  );
  const state: ProviderHealth["state"] = value.enabled === false ? "offline" : "degraded";
  return {
    id: value.id,
    name: value.name,
    kind: value.is_local ? "local" : isGateway ? "gateway" : "commercial",
    state,
    modelCount: value.model_allowlist?.length ?? 0,
    privacy: value.privacy?.local_only
      ? "local_only"
      : value.privacy?.residency?.length
        ? "regional"
        : "cloud",
    capabilities,
    message: value.enabled === false
      ? "Provider profile is disabled."
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
    supportTier: value.support_tier,
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
      let details: unknown;
      try {
        details = await response.json();
      } catch {
        details = await response.text();
      }
      const message =
        typeof details === "object" && details && "message" in details
          ? String(details.message)
          : typeof details === "object" && details && "detail" in details
            ? String(details.detail)
          : `Nebula API request failed (${response.status})`;
      throw new ApiError(message, response.status, response.headers.get("x-request-id") ?? undefined, details);
    }

    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  health(signal?: AbortSignal): Promise<HealthResponse> {
    return this.request<
      Partial<HealthResponse> & {
        api_version?: string;
        dialect?: string;
      }
    >("health", { signal }).then((health) => ({
      status: health.status === "degraded" ? "degraded" : "ok",
      version: health.version ?? health.api_version ?? "unknown",
      mode: health.mode ?? (health.dialect?.startsWith("postgres") ? "team" : "local"),
      runner: health.runner ?? "unavailable",
    }));
  }

  listEngagements(signal?: AbortSignal): Promise<Page<EngagementSummary>> {
    return this.request<WireEngagement[]>("engagements", { signal })
      .then((items) => page(items.map(mapEngagement)));
  }

  listRuns(engagementId: string, signal?: AbortSignal): Promise<Page<AgentRunSummary>> {
    return this.request<WireAgentRun[]>(`runs?${engagementQuery(engagementId)}`, { signal })
      .then((items) => page(items.map(mapRun)));
  }

  listApprovals(engagementId: string, signal?: AbortSignal): Promise<Page<ApprovalSummary>> {
    return this.request<WireApproval[]>(`approvals?${engagementQuery(engagementId)}`, { signal })
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
    return this.request<WireAsset[]>(`assets?${engagementQuery(engagementId)}`, { signal })
      .then((items) => page(items.map(mapAsset)));
  }

  listFindings(engagementId: string, signal?: AbortSignal): Promise<Page<FindingSummary>> {
    return this.request<WireFinding[]>(`findings?${engagementQuery(engagementId)}`, { signal })
      .then((items) => page(items.map(mapFinding)));
  }

  listProviders(signal?: AbortSignal): Promise<Page<ProviderHealth>> {
    // Provider profiles are global in the current single-organization Core.
    // Applying engagement_id would correctly return no rows.
    return this.request<WireProvider[]>("providers", { signal })
      .then((items) => page(items.map(mapProvider)));
  }

  listProviderCatalog(signal?: AbortSignal): Promise<ProviderCatalogEntry[]> {
    return this.request<WireProviderCatalogEntry[]>("provider-catalog", { signal })
      .then((items) => items.map(mapProviderCatalog));
  }

  createProvider(body: ProviderCreateRequest): Promise<ProviderHealth> {
    const modelAllowlist = body.defaultModel ? [body.defaultModel] : [];
    return this.request<WireProvider>("providers", {
      method: "POST",
      body: JSON.stringify({
        name: body.name,
        provider_type: body.providerType,
        endpoint: body.endpoint || null,
        enabled: true,
        is_local: body.local,
        model_allowlist: modelAllowlist,
        capabilities: { streaming: true },
        privacy: { local_only: body.local },
        metadata: body.defaultModel ? { default_model: body.defaultModel } : {},
      }),
    }).then(mapProvider);
  }

  refreshProviderHealth(id: string, signal?: AbortSignal): Promise<ProviderRuntimeHealth> {
    return this.request<WireProviderRuntimeHealth>(
      `providers/${encodeURIComponent(id)}/health`,
      { method: "POST", signal },
    ).then(mapProviderRuntimeHealth);
  }
}
