import {expect, it, vi} from "vitest";
import {ApiClient} from "./client";

const setup = (projectId?: string) => ({
  application_stage: "ready", stage_detail: "Ready", retryable: false, recovery_actions: [],
  core: {status: "ready"}, terminal: {status: "ready", candidates: [], image_preparation: {
    phase: "ready", project_id: projectId, progress_indeterminate: false, can_cancel: false, can_retry: false,
  }}, assistant: {status: "needs_model"},
});
const json = (value: unknown) => new Response(JSON.stringify(value), {headers: {"Content-Type": "application/json"}});

it("keeps host detection separate from the exact selected Project setup read", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async input => json(setup(new URL(String(input)).searchParams.get("project_id") ?? undefined)));
  const api = new ApiClient({baseUrl: "https://core.test", fetch});
  const controller = new AbortController();
  await api.setupStatus(controller.signal);
  expect((await api.setupStatus(controller.signal, "project/a+b")).terminal.imagePreparation.projectId).toBe("project/a+b");
  expect(fetch.mock.calls.map(([input]) => String(input))).toEqual([
    "https://core.test/api/v1/setup/status",
    "https://core.test/api/v1/setup/status?project_id=project%2Fa%2Bb",
  ]);
  expect(fetch.mock.calls.every(([, init]) => init?.signal === controller.signal)).toBe(true);
});

it("refuses image state from another Project before exposing its operation", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(json(setup("another-project")));
  const api = new ApiClient({fetch});
  await expect(api.setupStatus(undefined, "selected-project")).rejects.toThrow("another Project");
  expect(fetch).toHaveBeenCalledOnce();
  expect(fetch.mock.calls[0][1]?.method ?? "GET").toBe("GET");
});

it("binds image preparation, retry and cancellation to the selected Project", async () => {
  const projectId = "selected/project";
  const operationId = "00000000-0000-4000-8000-000000000001";
  const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async () => json({
    operation: "image_preparation", accepted: true, idempotent: false, operation_id: operationId, setup: setup(projectId),
  }));
  const api = new ApiClient({baseUrl: "https://core.test", fetch});
  await api.prepareSetupImage(projectId);
  await api.retrySetupImage(projectId);
  await api.cancelSetupImage(operationId, projectId);
  expect(fetch.mock.calls.map(([input, init]) => [new URL(String(input)).pathname, JSON.parse(String(init?.body))])).toEqual([
    ["/api/v1/setup/image/prepare", {project_id: projectId}],
    ["/api/v1/setup/image/retry", {project_id: projectId}],
    ["/api/v1/setup/image/cancel", {project_id: projectId, operation_id: operationId}],
  ]);
});

it("requests observed terminal capacity for the exact Project", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(json({active_sessions: 2, available_sessions: 1, max_active_sessions: 3}));
  const api = new ApiClient({baseUrl: "https://core.test", fetch});
  const controller = new AbortController();
  expect(await api.containerTerminalCapacity("project/a+b", controller.signal)).toEqual({activeSessions: 2, availableSessions: 1, maxActiveSessions: 3});
  expect(String(fetch.mock.calls[0][0])).toBe("https://core.test/api/v1/container-terminal/capacity?engagement_id=project%2Fa%2Bb");
  expect(fetch.mock.calls[0][1]?.signal).toBe(controller.signal);
});
