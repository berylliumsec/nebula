import type { ApiClient } from "./client";

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

export interface ButlerCheckInterval {
  check_interval_minutes: number;
  revision: number;
  last_check_at: string | null;
  next_check_at: string | null;
}

export function getButlerCheckInterval(api: ApiClient, signal?: AbortSignal): Promise<ButlerCheckInterval> {
  return api.request<ButlerCheckInterval>("butler/check-interval", {signal});
}

export function updateButlerCheckInterval(api: ApiClient, minutes: number, expectedRevision: number): Promise<ButlerCheckInterval> {
  return api.request<ButlerCheckInterval>("butler/check-interval", {
    method: "PUT", body: JSON.stringify({check_interval_minutes: minutes, expected_revision: expectedRevision}),
  });
}
