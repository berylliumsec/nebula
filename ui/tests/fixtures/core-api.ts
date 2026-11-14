import {chmod, readdir, rm} from "node:fs/promises";
import path from "node:path";
import {expect, type APIRequestContext, type APIResponse} from "@playwright/test";

/**
 * Real-Core API helpers for Playwright request contexts whose baseURL ends with `/api/v1/`.
 * Core's API process refuses a mutation without a client Idempotency-Key (428) and may answer
 * with a saved-request receipt (202) that names the request to follow, as the GUI does.
 */

let mutationSequence = 0;

/** True while a follow read is still unanswered: a receipt, or a busy 429/503 that Core did not mark final. */
function stillPending(response: APIResponse): boolean {
  const state = response.headers()["x-nebula-request-state"];
  if (response.status() === 202) return state !== "complete";
  return [429, 503].includes(response.status()) && (state === undefined || state === "pending");
}

/** Send one mutation with a fresh Idempotency-Key and return Core's final answer, following a pending receipt. */
export async function coreMutation(
  api: APIRequestContext,
  method: "post" | "put" | "patch" | "delete",
  url: string,
  data?: unknown,
  options: {key?: string; timeout?: number} = {},
): Promise<APIResponse> {
  const key = options.key ?? `playwright-${process.pid}-${Date.now()}-${++mutationSequence}`;
  let response = await api[method](url, {...(data === undefined ? {} : {data}), headers: {"Idempotency-Key": key}});
  if (response.status() !== 202 || response.headers()["x-nebula-request-state"] === "complete") return response;
  const accepted = await response.json() as {command_id?: string; receipt?: {command_id?: string}};
  const command = response.headers()["x-nebula-command-id"] ?? accepted.receipt?.command_id ?? accepted.command_id;
  expect(command, "a pending 202 names its saved request").toBeTruthy();
  await expect.poll(async () => {
    response = await api.get(`runtime/requests/${encodeURIComponent(command!)}/response`);
    return stillPending(response);
  }, {timeout: options.timeout ?? 60_000}).toBe(false);
  return response;
}

/** Core prints its address before its API process is ready; wait for an authenticated API read. */
export async function waitForCoreApi(api: APIRequestContext, timeout = 60_000): Promise<void> {
  await expect.poll(async () => (await api.get("engagements")).status(), {timeout}).toBe(200);
}

/**
 * Remove a disposable Core data root. Core keeps its retained runtime releases read-only, and a detached
 * worker that outlives a killed supervisor can still write logs there, so removal retries briefly.
 */
export async function removeCoreData(dataDir: string): Promise<void> {
  const writable = async (directory: string): Promise<void> => {
    await chmod(directory, 0o700);
    for (const entry of await readdir(directory, {withFileTypes: true})) {
      if (entry.isDirectory()) await writable(path.join(directory, entry.name));
    }
  };
  await writable(dataDir).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; });
  await rm(dataDir, {recursive: true, force: true, maxRetries: 10, retryDelay: 500});
}
