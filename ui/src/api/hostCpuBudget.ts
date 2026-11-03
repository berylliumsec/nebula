import type {ApiClient} from "./client";

export const HOST_CPU_BUDGET_CHANGED = "nebula:host-cpu-budget-changed";

export interface HostCpuBudget {
  host_id: string;
  host_name: string;
  revision: number;
  cpu_limit: number | null;
  cpu_capacity: number;
  automatic_cpu_limit: number;
  effective_cpu_limit: number;
  applied_cpu_limit: number | null;
  apply_state: "inactive" | "applied" | "pending" | "failed" | "unavailable";
  capacity_clamped: boolean;
  detail: string;
}

export function getHostCpuBudget(api: ApiClient, signal?: AbortSignal): Promise<HostCpuBudget> {
  return api.request<HostCpuBudget>("host-cpu-budget", {signal});
}

export function saveHostCpuBudget(api: ApiClient, saved: HostCpuBudget, cpuLimit: number | null): Promise<HostCpuBudget> {
  return api.request<HostCpuBudget>("host-cpu-budget", {
    method: "PUT",
    body: JSON.stringify({host_id: saved.host_id, expected_revision: saved.revision, cpu_limit: cpuLimit}),
  });
}
