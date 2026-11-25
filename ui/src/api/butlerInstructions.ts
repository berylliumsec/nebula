import type { ApiClient } from "./client";

export const BUTLER_INTERVAL_CHANGED_EVENT = "nebula:butler-interval-changed";

export interface ButlerInstructionsFile {
  path: string;
  status: "ready" | "missing" | "invalid";
  content: string | null;
  sha256: string | null;
  detail: string | null;
}

export function getButlerInstructions(api: ApiClient, signal?: AbortSignal): Promise<ButlerInstructionsFile> {
  return api.request<ButlerInstructionsFile>("butler/instructions", {signal});
}

export function updateButlerInstructions(api: ApiClient, content: string, expectedSha256: string | null): Promise<ButlerInstructionsFile> {
  return api.request<ButlerInstructionsFile>("butler/instructions", {
    method: "PUT", body: JSON.stringify({content, expected_sha256: expectedSha256}),
  });
}

export interface ButlerSummaryTargetChoice {
  project_id: string;
  project_name: string;
  session_id: string;
  title: string;
}

export interface ButlerSummaryTarget {
  configured: {project_id: string; session_id: string | null} | null;
  effective: {source: string; project_id: string; session_id: string; title: string; url: string} | null;
  unavailable: {source: string; project_id: string; reason: string}[];
  last_fallback: {case_id: string; fallbacks: {source: string; project_id: string; reason: string}[]; at: string} | null;
  pending: number;
  options: ButlerSummaryTargetChoice[];
}

export interface ButlerCheckInterval {
  check_interval_minutes: number;
  revision: number;
  last_check_at: string | null;
  next_check_at: string | null;
  // Where each finished Butler check posts its summary; absent on older Core.
  summary_target?: ButlerSummaryTarget;
}

export function getButlerCheckInterval(api: ApiClient, signal?: AbortSignal): Promise<ButlerCheckInterval> {
  return api.request<ButlerCheckInterval>("butler/check-interval", {signal});
}

export function updateButlerCheckInterval(api: ApiClient, minutes: number, expectedRevision: number): Promise<ButlerCheckInterval> {
  return saveButlerCheckSettings(api, {check_interval_minutes: minutes, expected_revision: expectedRevision});
}

/** Choose the Butler chat that receives check summaries; null restores Core's default. */
export function updateButlerSummaryTarget(api: ApiClient, target: {project_id: string; session_id: string} | null, expectedRevision: number): Promise<ButlerCheckInterval> {
  return saveButlerCheckSettings(api, {summary_target: target, expected_revision: expectedRevision});
}

function saveButlerCheckSettings(api: ApiClient, body: Record<string, unknown>): Promise<ButlerCheckInterval> {
  return api.request<ButlerCheckInterval>("butler/check-interval", {
    method: "PUT", body: JSON.stringify(body),
  });
}
