import type {ApiClient} from "./client";

export const GIB = 1024 ** 3;
export const MEMORY_BUDGET_CHANGED = "nebula:memory-budget-changed";
export type MemoryEnvironment = "local" | "container" | "ssh" | "remote" | "cpu-worker";
export interface MemoryLimits {max_bytes: number; high_bytes: number | null; swap_bytes: number}
export interface MemoryPolicy {
  default_job: MemoryLimits; aggregate_bytes: number; reserve_bytes: number;
  environment_defaults: Partial<Record<MemoryEnvironment, MemoryLimits>>;
  profile_overrides: Record<string, MemoryLimits>;
}
export interface MemoryCapabilities {
  protocol: string; environment: MemoryEnvironment; profile_id: string | null;
  kernel_enforced: boolean; protected_process_tree: boolean; explicit_swap: boolean;
  pressure_threshold: boolean; live_update: boolean; detail: string;
}
export interface MemorySettings {
  revision: number; policy: MemoryPolicy;
  administrator_limits: {capacity_bytes: number; maximum_job_bytes: number; maximum_aggregate_bytes: number; minimum_reserve_bytes: number; maximum_swap_bytes: number};
  capabilities: MemoryCapabilities[];
  profiles: {id: string; name: string; enabled: boolean; capabilities: MemoryCapabilities | null}[];
  reserved_bytes: number; available_bytes: number; effective_aggregate_bytes: number;
  runtime_reserved_bytes?: number; minimum_core_reserve_bytes?: number;
  held_jobs: number; queued_jobs: number; application_state: string; detail: string;
}
export interface MemoryJob {
  id: string; project_id: string | null; resource_kind: string; resource_id: string; revision: number;
  state: string; reserved_bytes: number; application_state: string; outcome: string | null; detail: string | null;
  snapshot: {policy_revision: number; environment: MemoryEnvironment; profile_id: string | null; source: string; limits: MemoryLimits; runner_overhead_bytes?: number};
  evidence: {group_id: string; applied: MemoryLimits; peak_bytes: number | null; current_bytes: number | null; observed_at: string | null; oom_kill_count: number | null; group_empty: boolean; termination_reason: string; detail: string} | null;
  pending_limits: MemoryLimits | null;
}
export const getMemorySettings = (api: ApiClient, signal?: AbortSignal) => api.request<MemorySettings>("memory-budgets", {signal});
export const getMemoryJobs = (api: ApiClient, signal?: AbortSignal) => api.request<{items: MemoryJob[]; at_limit: boolean; display_limit: number}>("memory-budgets/jobs", {signal});
export const saveMemorySettings = (api: ApiClient, saved: MemorySettings, policy: MemoryPolicy) => api.request<MemorySettings>("memory-budgets", {method: "PUT", body: JSON.stringify({expected_revision: saved.revision, policy})});
export const probeMemoryRunner = (api: ApiClient, profileId: string) => api.request<MemoryCapabilities>(`memory-budgets/runners/${encodeURIComponent(profileId)}/probe`, {method: "POST"});
export const applyMemoryLimits = (api: ApiClient, job: MemoryJob, limits: MemoryLimits, acknowledge: boolean) => api.request<MemoryJob>(`memory-budgets/jobs/${encodeURIComponent(job.id)}/apply`, {method: "POST", body: JSON.stringify({expected_revision: job.revision, limits, acknowledge_possible_termination: acknowledge})});
export function memoryAmount(bytes: number): string {return `${Number((bytes / GIB).toFixed(3))} GiB`;}
