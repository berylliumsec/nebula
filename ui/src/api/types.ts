export type Identifier = string;

export interface Page<T> {
  items: T[];
  total: number;
  nextCursor?: string;
}

export interface EngagementSummary {
  id: Identifier;
  name: string;
  clientName?: string;
  status: "draft" | "active" | "paused" | "complete" | "archived";
  updatedAt: string;
  scopeAssetCount: number;
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
  exposure: "external" | "internal" | "unknown";
  serviceCount: number;
  findingCount: number;
  lastSeenAt?: string;
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
  severity: "critical" | "high" | "medium" | "low" | "info";
  status: FindingStatus;
  affectedAssetCount: number;
  evidenceCount: number;
  cveIds: string[];
  updatedAt: string;
}

export interface ProviderHealth {
  id: Identifier;
  name: string;
  kind: "commercial" | "local" | "gateway";
  state: "healthy" | "degraded" | "offline" | "unconfigured";
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
  supportTier: "native" | "standard" | "compatible" | "gateway";
}

export interface ProviderCreateRequest {
  name: string;
  providerType: string;
  endpoint?: string;
  local: boolean;
  defaultModel?: string;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  mode: "local" | "team";
  runner: "ready" | "unavailable" | "degraded";
}

export type RunEventKind =
  | "run.created"
  | "run.status_changed"
  | "task.created"
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
