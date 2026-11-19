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

export const HOST_COMMAND_LIMIT_MAX = 64;
export type HostCommandState = "queued" | "reserved" | "starting" | "running" | "updating" | "uncertain";
/** One Host command in Core's shared admission queue, running or waiting. */
export interface HostCommandItem {
  job_id: string; execution_id: string; project_id: string | null; state: HostCommandState;
  /** The command exited but left processes running that still hold its memory. */
  detached: boolean; cancel_requested: boolean;
  /** One-based place in line; queued commands only. */
  position: number | null;
  command: string | null; cwd: string | null; owner_kind: string | null;
  tool_call_id: string | null; chat_session_id: string | null; background: boolean;
  reserved_bytes: number; limit_bytes: number; queued_at: string; started_at?: string | null; updated_at: string;
}
export interface HostCommandSettings {revision: number; running_at_once: number}
export interface HostCommandView {settings: HostCommandSettings; running_count: number; running: HostCommandItem[]; queued: HostCommandItem[]}
export const getHostCommands = (api: ApiClient, options: {toolCallId?: string; signal?: AbortSignal} = {}) =>
  api.request<HostCommandView>(`memory-budgets/host-commands${options.toolCallId ? `?tool_call_id=${encodeURIComponent(options.toolCallId)}` : ""}`, {signal: options.signal});
export const saveHostCommandSettings = (api: ApiClient, expectedRevision: number, runningAtOnce: number) =>
  api.request<HostCommandSettings>("memory-budgets/host-commands/settings", {method: "PUT", body: JSON.stringify({expected_revision: expectedRevision, running_at_once: runningAtOnce})});
export const cancelHostCommand = (api: ApiClient, jobId: string) =>
  api.request<HostCommandItem>(`memory-budgets/host-commands/${encodeURIComponent(jobId)}/cancel`, {method: "POST"});
export function isHostCommandView(value: unknown): value is HostCommandView {
  const view = value as Partial<HostCommandView> | null;
  return !!view && Array.isArray(view.running) && Array.isArray(view.queued) && typeof view.settings?.running_at_once === "number";
}
/** "1st", "2nd", "3rd", "11th"… for a place in line. */
export function ordinal(value: number): string {
  const tens = value % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : ({1: "st", 2: "nd", 3: "rd"} as Record<number, string>)[value % 10] ?? "th";
  return `${value}${suffix}`;
}
/** Short elapsed time since an ISO timestamp: "8s", "3m 4s", "1h 5m". */
export function elapsedSince(iso: string, now: number): string {
  const started = Date.parse(iso);
  const seconds = Number.isNaN(started) ? 0 : Math.max(0, Math.floor((now - started) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return seconds % 60 ? `${minutes}m ${seconds % 60}s` : `${minutes}m`;
  return minutes % 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${Math.floor(minutes / 60)}h`;
}
