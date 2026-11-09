import "fake-indexeddb/auto";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createStore, del } from "idb-keyval";
import { ApiClient } from "./client";
import { PendingRequestStore, RequestNotSentError } from "./durableRequests";
import type { ChatStreamEvent } from "./types";

const identity = {core_id: "core", principal_id: "operator"};
const json = (value: unknown, status = 200, headers = {}) => new Response(JSON.stringify(value), {status, headers: {"Content-Type": "application/json", ...headers}});
const receipt = () => json({state: "accepted", receipt: {command_id: "command", project_id: "project", sequence: 1, accepted_at_ms: 1}}, 202, {"Retry-After": "0.001", "X-Nebula-Command-Id": "command"});
const request = {backend: "provider" as const, providerId: "provider", model: "model", engagementId: "project", messages: [{role: "user" as const, content: "Once"}]};
const done = {type: "done", provider_id: "provider", model: "model", session_id: "session", turn_id: "turn", message: {id: "answer", role: "assistant", content: "Finished"}};
const stream = (...events: unknown[]) => new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(""), {headers: {"Content-Type": "text/event-stream", "X-Nebula-Command-Id": "command"}});
beforeEach(async () => { await del("immutable-attempts/v1", createStore("nebula-pending-requests", "requests")); });
afterEach(() => { vi.restoreAllMocks(); });

describe("durable client transport", () => {
  it("waits through both receipt shapes and returns the actual mutation result", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity))
      .mockResolvedValueOnce(receipt())
      .mockResolvedValueOnce(json({state: "running", command_id: "command"}, 202, {"Retry-After": "0.001"}))
      .mockResolvedValueOnce(json({id: "approval", status: "approved"}));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    expect(await client.request("approvals/approval/decision", {method: "POST", body: '{"decision":"approve"}'})).toEqual({id: "approval", status: "approved"});
    expect(new Headers(fetch.mock.calls[1][1]?.headers).get("Idempotency-Key")).toMatch(/^[a-f0-9]{48}$/);
    expect(fetch.mock.calls.slice(2).every(([, init]) => init?.method === "GET")).toBe(true);
    expect(await client.getPendingRequests()).toEqual([]);
  });

  it("uses the same immutable key for an explicit retry after an unknown outcome", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity))
      .mockRejectedValueOnce(new TypeError("connection lost"))
      .mockResolvedValueOnce(json({saved: true}));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    const init = {method: "PUT", body: '{"value": 1}'};
    await expect(client.request("settings", init)).rejects.toThrow("connection lost");
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(await client.getPendingRequests()).toHaveLength(1);
    await client.request("settings", init);
    expect(new Headers(fetch.mock.calls[1][1]?.headers).get("Idempotency-Key")).toBe(new Headers(fetch.mock.calls[2][1]?.headers).get("Idempotency-Key"));
  });

  it("reconciles an unanswered submission on reopen using only authenticated reads", async () => {
    const firstFetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockRejectedValueOnce(new TypeError("lost"));
    const first = new ApiClient({fetch: firstFetch}); await first.initializeContinuity();
    await expect(first.request("chat/completions", {method: "POST", body: JSON.stringify({engagement_id: "project", messages: [{role: "user", content: "Once"}]})})).rejects.toThrow();
    const reopenedFetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity))
      .mockResolvedValueOnce(json({command_id: "command", project_id: "project", state: "running", turn_id: "turn", session_id: "session", stream_after: 0, stream_ready: true}));
    const reopened = new ApiClient({token: "rotated", fetch: reopenedFetch}); await reopened.initializeContinuity();
    expect(await reopened.reconcilePendingRequests()).toEqual([expect.objectContaining({commandId: "command", status: expect.objectContaining({session_id: "session"})})]);
    expect(reopenedFetch.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
    expect(new Headers(reopenedFetch.mock.calls[1][1]?.headers).get("Authorization")).toBe("Bearer rotated");
    expect(String(reopenedFetch.mock.calls[1][0])).toContain("/runtime/requests/lookup?idempotency_key=");
  });

  it("keeps an unknown request when lookup has not found it yet", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockRejectedValueOnce(new TypeError("lost"))
      .mockResolvedValueOnce(json({detail: "unavailable"}, 404));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    await expect(client.request("settings", {method: "PUT", body: "{}"})).rejects.toThrow();
    expect(await client.reconcilePendingRequests()).toHaveLength(1);
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
  });

  it("rejects before sending when durable persistence is unavailable", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(json(identity));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    vi.spyOn(PendingRequestStore.prototype, "prepare").mockRejectedValue(new RequestNotSentError("Nothing was sent. Enable storage."));
    await expect(client.request("settings", {method: "PUT", body: "{}"})).rejects.toThrow("Nothing was sent");
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("recovers lost chat acceptance with lookup and response GET instead of resubmission", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockRejectedValueOnce(new TypeError("lost"))
      .mockResolvedValueOnce(json({command_id: "command", project_id: "project", state: "running", turn_id: "turn", session_id: "session"}))
      .mockResolvedValueOnce(stream(done));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    expect((await client.streamChat(request, () => {}))?.message.content).toBe("Finished");
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(String(fetch.mock.calls[3][0])).toContain("/runtime/requests/command/response");
  });

  it("uses the resumed command response boundary when resume acceptance is lost", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockRejectedValueOnce(new TypeError("lost"))
      .mockResolvedValueOnce(json({command_id: "command", project_id: "project", state: "running", turn_id: "turn", session_id: "session", stream_after: 8}))
      .mockResolvedValueOnce(stream({...done, sequence: 9, epoch: "durable-v1"}));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    await client.resumeChatTurn("turn", request, () => {});
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(fetch.mock.calls.some(([url]) => String(url).includes("/turns/turn/events"))).toBe(false);
    expect(String(fetch.mock.calls[3][0])).toContain("/runtime/requests/command/response");
  });

  it("rejects an oversized incomplete SSE frame without unbounded buffering or reposting", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockResolvedValueOnce(new Response(`data: ${"x".repeat(1024 * 1024)}`));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    await expect(client.streamChat(request, () => {})).rejects.toThrow("safe frame size");
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(await client.getPendingRequests()).toHaveLength(1);
  });

  it("retains a command that needs reconciliation instead of treating 409 as a rejected submission", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity))
      .mockResolvedValueOnce(json({state: "uncertain", command_id: "command", detail: "Needs reconciliation"}, 409, {"X-Nebula-Command-Id": "command"}));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    await expect(client.request("approvals/approval/decision", {method: "POST", body: "{}"})).rejects.toMatchObject({status: 409});
    expect(await client.getPendingRequests()).toHaveLength(1);
  });

  it("uses a visible ephemeral limitation for legacy servers without identity", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json({detail: "not found"}, 404));
    const client = new ApiClient({fetch});
    expect(await client.initializeContinuity()).toMatchObject({persistent: false, limitation: expect.stringContaining("only in this window")});
  });

  it("retains submission identity when a viewer closes before any headers arrive", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity))
      .mockImplementationOnce((_input, init) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Viewer closed", "AbortError")), {once: true});
      }));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    const controller = new AbortController();
    const pending = client.streamChat(request, () => {}, controller.signal);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    controller.abort();
    await expect(pending).rejects.toMatchObject({name: "AbortError"});
    expect(await client.getPendingRequests()).toHaveLength(1);
    expect(fetch.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("reports completed work accurately if local receipt cleanup fails", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity))
      .mockResolvedValueOnce(json({saved: true}, 200, {"X-Nebula-Command-Id": "command"}));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    vi.spyOn(PendingRequestStore.prototype, "remove").mockRejectedValue(new RequestNotSentError("Storage blocked"));
    await expect(client.request("settings", {method: "PUT", body: "{}"})).rejects.toMatchObject({name: "PendingRequestError", message: expect.stringContaining("Core completed the request")});
    expect(await client.getPendingRequests()).toHaveLength(1);
  });

  it("keeps ordinary direct-stream events alongside receipt acceptance", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockResolvedValueOnce(stream(done));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    const events: ChatStreamEvent[] = [];
    await client.streamChat(request, event => events.push(event));
    expect(events.map(event => event.type)).toEqual(["accepted", "done"]);
  });

  it("resolves binary export receipts without replacing the requested media type", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockResolvedValueOnce(receipt())
      .mockResolvedValueOnce(new Response("zip-bytes", {headers: {"Content-Type": "application/zip"}}));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    expect(await (await client.exportDiagnostics()).text()).toBe("zip-bytes");
    expect(new Headers(fetch.mock.calls[1][1]?.headers).get("Accept")).toBe("application/zip");
    expect(new Headers(fetch.mock.calls[2][1]?.headers).get("Accept")).toBe("application/zip");
    expect(new Headers(fetch.mock.calls[1][1]?.headers).has("Idempotency-Key")).toBe(true);
  });

  it("keeps raw upload bytes and preconditions while resolving the saved receipt", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(json(identity)).mockResolvedValueOnce(receipt())
      .mockResolvedValueOnce(json({engagement_id: "project", path: "file", size: 5, sha256: "hash", overwritten: true}));
    const client = new ApiClient({fetch}); await client.initializeContinuity();
    const file = new Blob(["proof"]);
    expect((await client.uploadWorkspaceFile("project", "file", file, true, undefined, "expected")).size).toBe(5);
    expect(fetch.mock.calls[1][1]?.body).toBe(file);
    const headers = new Headers(fetch.mock.calls[1][1]?.headers);
    expect(headers.get("If-Match")).toBe("expected");
    expect(headers.get("Content-Type")).toBe("application/octet-stream");
    expect(headers.has("Idempotency-Key")).toBe(true);
  });

  it.each(["work", "notebook"])("bounds incomplete %s event frames", async kind => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(new Response("x".repeat(1024 * 1024 + 1)));
    const client = new ApiClient({fetch});
    const signal = new AbortController().signal;
    const pending = kind === "work" ? client.watchWorkChanges(() => {}, () => {}, signal)
      : client.watchNotebookRun("project", "run", 0, () => {}, signal);
    await expect(pending).rejects.toThrow("safe frame size");
    expect(fetch).toHaveBeenCalledOnce();
  });
});
