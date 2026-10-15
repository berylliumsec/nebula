import type { ApiClient } from "./client";

export type ButlerAuthority = "watch" | "steer" | "delegate" | "act";
export type ButlerActorRole = "main_agent" | "subagent";
export type ButlerDelegateAction = "pause_urgent_machine_blocker";

export interface ButlerAuthorityPolicy {
  projectId: string;
  authority: ButlerAuthority;
  delegateActions: ButlerDelegateAction[];
  effectiveActions: string[];
  supportedActions: string[];
  revision: number;
}

export interface ButlerGoalCoverage {
  reportCapability: string;
  reason?: string;
}

export interface ButlerWatchedGoal {
  goalId: string;
  sessionId: string;
  parentGoalId?: string;
  objective: string;
  status: string;
  actorRole: ButlerActorRole;
  progressDueAt?: string;
  coverage: ButlerGoalCoverage;
}

export interface ButlerIncident {
  id: string;
  incidentKey: string;
  revision: number;
  status: string;
  summary: string;
  source: string;
  verificationState: string;
  evidenceRefs: Array<{ kind: string; id: string; projectId: string }>;
  evidenceSummary: string[];
  impact?: string;
  nextAction?: string;
  goalIds: string[];
  sessionIds: string[];
  observedAt?: string;
  updatedAt?: string;
  acknowledgedAt?: string;
  resolvedAt?: string;
}

export interface ButlerDecision {
  id: string;
  summary: string;
  status?: string;
  choice?: string;
  reason?: string;
  impact?: string;
  nextAction?: string;
  whyNow?: string;
  evidenceRefs: Array<{ kind: string; id: string; projectId: string }>;
  sourceVerificationState?: string;
  actorRole?: ButlerActorRole;
  goalId?: string;
  sessionId?: string;
  reviewedAt?: string;
}

export interface ButlerAction {
  id: string;
  actionType: "request_parent_status" | "pause_urgent_machine_blocker";
  status: "planned" | "dispatching" | "dispatched" | "applied" | "uncertain" | "cancelled";
  summary: string;
  goalId?: string;
  sourceSessionId?: string;
  targetSessionId?: string;
  verificationState?: string;
  createdAt?: string;
  startedAt?: string;
  finishedAt?: string;
}

export interface ButlerRecoveryCase {
  id: string;
  trigger: "immediate" | "periodic";
  signal: string;
  status: "observed" | "investigating" | "verifying" | "resolved" | "needs_attention" | "uncertain" | "cancelled";
  summary: string;
  outcome: string;
  sourceSessionId?: string;
  targetSessionId?: string;
  createdAt?: string;
  finishedAt?: string;
  steps: Array<{ id: string; operation: string; status: string; started_at?: string; finished_at?: string }>;
}

export interface ButlerOpenRouterResource {
  kind: "openrouter_account";
  providerId: string;
  providerName: string;
  status: "observed" | "unavailable";
  remainingUsd?: number;
  observedAt?: string;
  thresholdState: "above" | "at_or_below" | "unknown";
}

export interface ButlerHarnessQuotaResource {
  kind: "harness_quota";
  harnessProfileId: string;
  harnessName: string;
  status: "observed" | "unavailable";
  remainingPercent?: number;
  observedAt?: string;
  thresholdState: "above" | "at_or_below" | "unknown";
  windowId: string;
  windowLabel: string;
  resetsAt?: string;
  source: string;
}

export type ButlerResource = ButlerOpenRouterResource | ButlerHarnessQuotaResource;

export function butlerResourceKey(resource: ButlerResource): string {
  return resource.kind === "openrouter_account" ? `account:${resource.providerId}` : `quota:${resource.harnessProfileId}:${resource.windowId}`;
}

export interface ButlerOverview {
  projectId: string;
  authority: ButlerAuthority;
  authorityRevision?: number;
  delegateActions: ButlerDelegateAction[];
  effectiveActions: string[];
  watchedGoals: ButlerWatchedGoal[];
  incidents: ButlerIncident[];
  decisions: ButlerDecision[];
  actions: ButlerAction[];
  recoveryCases: ButlerRecoveryCase[];
  resources: ButlerResource[];
  lastEventSequence: number;
  coverageGaps: string[];
  summary: { watchedGoals: number; openIncidents: number; acknowledgedIncidents: number; resolvedIncidents: number; actions: number; recoveryCases: number; recoveryAttention: number; recoveryActive: number; accountResources: number; harnessQuotaResources: number };
  nextWatchOffset?: number;
  nextIncidentOffset?: number;
  nextActionOffset?: number;
  nextRecoveryOffset?: number;
  nextResourceOffset?: number;
  capabilities: {
    authorityLevels: ButlerAuthority[];
    incidents: boolean;
    decisions: boolean;
    actions: boolean;
    recovery: boolean;
    agentReports: boolean;
    machineAlerts: boolean;
    resourceAlerts: boolean;
    liveEvents: boolean;
  };
}

interface WireButlerOverview {
  project_id: string;
  authority: ButlerAuthority;
  authority_revision?: number | null;
  delegate_actions?: ButlerDelegateAction[];
  effective_actions?: string[];
  watched_goals?: Array<{
    goal_id: string;
    session_id: string;
    parent_goal_id?: string | null;
    objective: string;
    status: string;
    actor_role: ButlerActorRole;
    progress_due_at?: string | null;
    coverage?: { report_capability: string; reason?: string | null };
  }>;
  incidents?: Array<{
    id: string;
    incident_key: string;
    revision: number;
    status: string;
    summary: string;
    source?: string;
    verification_state?: string;
    evidence_refs?: Array<{ kind: string; id: string; project_id: string }>;
    evidence_summary?: string[];
    impact?: string | null;
    next_action?: string | null;
    goal_ids?: string[];
    session_ids?: string[];
    observed_at?: string | null;
    updated_at?: string | null;
    acknowledged_at?: string | null;
    resolved_at?: string | null;
  }>;
  decisions?: Array<{
    id: string;
    summary: string;
    status?: string | null;
    choice?: string | null;
    reason?: string | null;
    impact?: string | null;
    next_action?: string | null;
    why_now?: string | null;
    evidence_refs?: Array<{ kind: string; id: string; project_id: string }>;
    source_verification_state?: string | null;
    actor_role?: ButlerActorRole | null;
    goal_id?: string | null;
    session_id?: string | null;
    reviewed_at?: string | null;
  }>;
  actions?: Array<{
    id: string;
    action_type: ButlerAction["actionType"];
    status: ButlerAction["status"];
    summary: string;
    goal_id?: string | null;
    source_session_id?: string | null;
    target_session_id?: string | null;
    verification_state?: string | null;
    created_at?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
  }>;
  recovery_cases?: Array<{
    id: string;
    trigger: ButlerRecoveryCase["trigger"];
    signal: string;
    status: ButlerRecoveryCase["status"];
    summary: string;
    outcome: string;
    source_session_id?: string | null;
    target_session_id?: string | null;
    created_at?: string | null;
    finished_at?: string | null;
    steps?: ButlerRecoveryCase["steps"];
  }>;
  resources?: Array<{
    kind: "openrouter_account";
    provider_id: string;
    provider_name: string;
    status: "observed" | "unavailable";
    remaining_usd?: number | null;
    observed_at?: string | null;
    threshold_state: "above" | "at_or_below" | "unknown";
  } | {
    kind: "harness_quota";
    harness_profile_id: string;
    harness_name: string;
    status: "observed" | "unavailable";
    remaining_percent: number | null;
    observed_at: string | null;
    threshold_state: "above" | "at_or_below" | "unknown";
    window_id: string;
    window_label: string;
    resets_at: string | null;
    source: string;
  }>;
  last_event_sequence?: number;
  coverage_gaps?: string[];
  summary?: { watched_goals: number; open_incidents: number; acknowledged_incidents: number; resolved_incidents: number; actions?: number; recovery_cases?: number; recovery_attention?: number; recovery_active?: number; account_resources?: number; harness_quota_resources?: number };
  next_watch_offset?: number | null;
  next_incident_offset?: number | null;
  next_action_offset?: number | null;
  next_recovery_offset?: number | null;
  next_resource_offset?: number | null;
  capabilities?: {
    authority_levels?: ButlerAuthority[];
    incidents?: boolean;
    decisions?: boolean;
    actions?: boolean;
    recovery?: boolean;
    agent_reports?: boolean;
    machine_alerts?: boolean;
    resource_alerts?: boolean;
    live_events?: boolean;
  };
}

/** Read the selected project's durable oversight snapshot through normal Core auth. */
export async function getButlerOverview(api: ApiClient, projectId: string, signal?: AbortSignal, offsets?: {
  watch?: number; incident?: number; action?: number; recovery?: number; resource?: number;
}): Promise<ButlerOverview> {
  const query = new URLSearchParams();
  if (offsets?.watch) query.set("watch_offset", String(offsets.watch));
  if (offsets?.incident) query.set("incident_offset", String(offsets.incident));
  if (offsets?.action) query.set("action_offset", String(offsets.action));
  if (offsets?.recovery) query.set("recovery_offset", String(offsets.recovery));
  if (offsets?.resource) query.set("resource_offset", String(offsets.resource));
  const wire = await api.request<WireButlerOverview>(
    `engagements/${encodeURIComponent(projectId)}/butler/overview${query.size ? `?${query}` : ""}`, { signal },
  );
  if (wire.project_id !== projectId) {
    throw new Error("Core returned oversight for a different project. Retry after changing projects.");
  }
  return {
    projectId,
    authority: wire.authority,
    authorityRevision: wire.authority_revision ?? undefined,
    delegateActions: wire.delegate_actions ?? [],
    effectiveActions: wire.effective_actions ?? [],
    watchedGoals: (wire.watched_goals ?? []).map((goal) => ({
      goalId: goal.goal_id,
      sessionId: goal.session_id,
      parentGoalId: goal.parent_goal_id ?? undefined,
      objective: goal.objective,
      status: goal.status,
      actorRole: goal.actor_role,
      progressDueAt: goal.progress_due_at ?? undefined,
      coverage: {
        reportCapability: goal.coverage?.report_capability ?? "unknown",
        reason: goal.coverage?.reason ?? undefined,
      },
    })),
    incidents: (wire.incidents ?? []).map((incident) => ({
      id: incident.id,
      incidentKey: incident.incident_key,
      revision: incident.revision,
      status: incident.status,
      summary: incident.summary,
      source: incident.source ?? "unknown",
      verificationState: incident.verification_state ?? "unverified",
      evidenceRefs: (incident.evidence_refs ?? []).map((ref) => ({ kind: ref.kind, id: ref.id, projectId: ref.project_id })),
      evidenceSummary: incident.evidence_summary ?? [],
      impact: incident.impact ?? undefined,
      nextAction: incident.next_action ?? undefined,
      goalIds: incident.goal_ids ?? [],
      sessionIds: incident.session_ids ?? [],
      observedAt: incident.observed_at ?? undefined,
      updatedAt: incident.updated_at ?? undefined,
      acknowledgedAt: incident.acknowledged_at ?? undefined,
      resolvedAt: incident.resolved_at ?? undefined,
    })),
    decisions: (wire.decisions ?? []).map((decision) => ({
      id: decision.id,
      summary: decision.summary,
      status: decision.status ?? undefined,
      choice: decision.choice ?? undefined,
      reason: decision.reason ?? undefined,
      impact: decision.impact ?? undefined,
      nextAction: decision.next_action ?? undefined,
      whyNow: decision.why_now ?? undefined,
      evidenceRefs: (decision.evidence_refs ?? []).map((ref) => ({ kind: ref.kind, id: ref.id, projectId: ref.project_id })),
      sourceVerificationState: decision.source_verification_state ?? undefined,
      actorRole: decision.actor_role ?? undefined,
      goalId: decision.goal_id ?? undefined,
      sessionId: decision.session_id ?? undefined,
      reviewedAt: decision.reviewed_at ?? undefined,
    })),
    actions: (wire.actions ?? []).map((action) => ({
      id: action.id,
      actionType: action.action_type,
      status: action.status,
      summary: action.summary,
      goalId: action.goal_id ?? undefined,
      sourceSessionId: action.source_session_id ?? undefined,
      targetSessionId: action.target_session_id ?? undefined,
      verificationState: action.verification_state ?? undefined,
      createdAt: action.created_at ?? undefined,
      startedAt: action.started_at ?? undefined,
      finishedAt: action.finished_at ?? undefined,
    })),
    recoveryCases: (wire.recovery_cases ?? []).map((item) => ({
      id: item.id,
      trigger: item.trigger,
      signal: item.signal,
      status: item.status,
      summary: item.summary,
      outcome: item.outcome,
      sourceSessionId: item.source_session_id ?? undefined,
      targetSessionId: item.target_session_id ?? undefined,
      createdAt: item.created_at ?? undefined,
      finishedAt: item.finished_at ?? undefined,
      steps: item.steps ?? [],
    })),
    resources: (wire.resources ?? []).map((resource): ButlerResource => resource.kind === "harness_quota" ? ({
      kind: resource.kind,
      harnessProfileId: resource.harness_profile_id,
      harnessName: resource.harness_name,
      status: resource.status,
      remainingPercent: resource.remaining_percent ?? undefined,
      observedAt: resource.observed_at ?? undefined,
      thresholdState: resource.threshold_state,
      windowId: resource.window_id,
      windowLabel: resource.window_label,
      resetsAt: resource.resets_at ?? undefined,
      source: resource.source,
    }) : ({
      kind: resource.kind,
      providerId: resource.provider_id,
      providerName: resource.provider_name,
      status: resource.status,
      remainingUsd: resource.remaining_usd ?? undefined,
      observedAt: resource.observed_at ?? undefined,
      thresholdState: resource.threshold_state,
    })),
    lastEventSequence: wire.last_event_sequence ?? 0,
    coverageGaps: wire.coverage_gaps ?? [],
    summary: {
      watchedGoals: wire.summary?.watched_goals ?? wire.watched_goals?.length ?? 0,
      openIncidents: wire.summary?.open_incidents ?? wire.incidents?.filter((item) => item.status === "open").length ?? 0,
      acknowledgedIncidents: wire.summary?.acknowledged_incidents ?? wire.incidents?.filter((item) => item.status === "acknowledged").length ?? 0,
      resolvedIncidents: wire.summary?.resolved_incidents ?? 0,
      actions: wire.summary?.actions ?? wire.actions?.length ?? 0,
      recoveryCases: wire.summary?.recovery_cases ?? wire.recovery_cases?.length ?? 0,
      recoveryAttention: wire.summary?.recovery_attention ?? wire.recovery_cases?.filter((item) => ["needs_attention", "uncertain"].includes(item.status)).length ?? 0,
      recoveryActive: wire.summary?.recovery_active ?? wire.recovery_cases?.filter((item) => ["observed", "investigating", "verifying"].includes(item.status)).length ?? 0,
      accountResources: wire.summary?.account_resources ?? wire.resources?.filter((item) => item.kind === "openrouter_account").length ?? 0,
      harnessQuotaResources: wire.summary?.harness_quota_resources ?? wire.resources?.filter((item) => item.kind === "harness_quota").length ?? 0,
    },
    nextWatchOffset: wire.next_watch_offset ?? undefined,
    nextIncidentOffset: wire.next_incident_offset ?? undefined,
    nextActionOffset: wire.next_action_offset ?? undefined,
    nextRecoveryOffset: wire.next_recovery_offset ?? undefined,
    nextResourceOffset: wire.next_resource_offset ?? undefined,
    capabilities: {
      authorityLevels: wire.capabilities?.authority_levels ?? ["watch"],
      incidents: wire.capabilities?.incidents ?? false,
      decisions: wire.capabilities?.decisions ?? false,
      actions: wire.capabilities?.actions ?? false,
      recovery: wire.capabilities?.recovery ?? false,
      agentReports: wire.capabilities?.agent_reports ?? false,
      machineAlerts: wire.capabilities?.machine_alerts ?? false,
      resourceAlerts: wire.capabilities?.resource_alerts ?? false,
      liveEvents: wire.capabilities?.live_events ?? false,
    },
  };
}

/** Acknowledgment is an operator action; Core persists it and owns notification deduplication. */
export async function acknowledgeButlerIncident(api: ApiClient, projectId: string, incident: ButlerIncident): Promise<void> {
  await api.request(`engagements/${encodeURIComponent(projectId)}/butler/incidents/${encodeURIComponent(incident.id)}/acknowledge`, {
    method: "POST",
    body: JSON.stringify({ expected_revision: incident.revision }),
  });
}

interface WireButlerAuthorityPolicy {
  project_id: string;
  authority: ButlerAuthority;
  delegate_actions: ButlerDelegateAction[];
  effective_actions: string[];
  supported_actions: string[];
  revision: number;
}

function mapAuthorityPolicy(wire: WireButlerAuthorityPolicy, projectId: string): ButlerAuthorityPolicy {
  if (wire.project_id !== projectId) throw new Error("Core returned Butler authority for a different project. Retry after changing projects.");
  return {
    projectId,
    authority: wire.authority,
    delegateActions: wire.delegate_actions ?? [],
    effectiveActions: wire.effective_actions ?? [],
    supportedActions: wire.supported_actions ?? [],
    revision: wire.revision,
  };
}

/** The saved Core policy and its revision are the authority editor's source of truth. */
export async function getButlerAuthority(api: ApiClient, projectId: string, signal?: AbortSignal): Promise<ButlerAuthorityPolicy> {
  const wire = await api.request<WireButlerAuthorityPolicy>(`engagements/${encodeURIComponent(projectId)}/butler/authority`, { signal });
  return mapAuthorityPolicy(wire, projectId);
}

export async function updateButlerAuthority(api: ApiClient, policy: ButlerAuthorityPolicy, authority: ButlerAuthority,
  delegateActions: ButlerDelegateAction[]): Promise<ButlerAuthorityPolicy> {
  const wire = await api.request<WireButlerAuthorityPolicy>(`engagements/${encodeURIComponent(policy.projectId)}/butler/authority`, {
    method: "PUT",
    body: JSON.stringify({ authority, delegate_actions: authority === "delegate" || authority === "act" ? delegateActions : [], expected_revision: policy.revision }),
  });
  return mapAuthorityPolicy(wire, policy.projectId);
}
