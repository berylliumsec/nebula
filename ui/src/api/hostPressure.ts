import type {ApiClient} from "./client";

export const HOST_PRESSURE_CHANGED = "nebula:host-pressure-changed";
export type PressureMetric = "cpu" | "memory" | "load" | "io";
export type PressureThresholds = Record<PressureMetric, number>;
export interface HostPressure {
  host_id: string;
  host_name: string;
  revision: number;
  saved_thresholds: PressureThresholds;
  effective_thresholds: PressureThresholds;
  readings: Record<PressureMetric, number | null>;
  waiting: boolean;
  pressure_metric: PressureMetric | null;
  reason: string | null;
  sample?: {load_average_ratio: number | null; host_io_pressure_ratio: number | null};
  concurrency: {provider_limit: number; background_limit: number; active: number; active_background: number};
}
export function getHostPressure(api: ApiClient, signal?: AbortSignal): Promise<HostPressure> {
  return api.request<HostPressure>("host-pressure", {signal});
}
export function saveHostPressure(api: ApiClient, saved: HostPressure, thresholds: PressureThresholds): Promise<HostPressure> {
  return api.request<HostPressure>("host-pressure", {method: "PUT", body: JSON.stringify({
    host_id: saved.host_id, expected_revision: saved.revision, thresholds,
  })});
}
