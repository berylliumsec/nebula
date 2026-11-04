import type {ApiClient} from "./client";

export const HOST_CPU_BUDGET_CHANGED = "nebula:host-cpu-budget-changed";

export interface HostCpuBudget {
  host_id: string;
  host_name: string;
  revision: number;
  enabled?: boolean | null;
  cpu_limit: number | null;
  cpu_capacity: number;
  automatic_cpu_limit: number;
  effective_cpu_limit: number;
  applied_cpu_limit: number | null;
  apply_state: "inactive" | "applied" | "pending" | "failed" | "unavailable";
  capacity_clamped: boolean;
  detail: string;
  activation_supported?: boolean;
  activation_unavailable_reason?: string | null;
  executor_scope?: string;
  executor_state?: string;
  worker_count?: number | null;
  active_jobs?: number | null;
  queued_jobs?: number | null;
  maximum_worker_cpus?: number | null;
}

export function getHostCpuBudget(api: ApiClient, signal?: AbortSignal): Promise<HostCpuBudget> {
  return api.request<HostCpuBudget>("host-cpu-budget", {signal});
}

export function saveHostCpuBudget(api: ApiClient, saved: HostCpuBudget, cpuLimit: number | null, enabled?: boolean): Promise<HostCpuBudget> {
  return api.request<HostCpuBudget>("host-cpu-budget", {
    method: "PUT",
    body: JSON.stringify({host_id: saved.host_id, expected_revision: saved.revision, cpu_limit: cpuLimit,
      ...(enabled === undefined ? {} : {enabled})}),
  });
}
