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
