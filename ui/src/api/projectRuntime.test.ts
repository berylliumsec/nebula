import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  PROJECT_RUNTIME_UNAVAILABLE,
  ProjectRuntimeRecovery,
  isProjectRuntimeUnavailable,
  parseRetryAfter,
  projectRuntimeRetryDelay,
} from "./projectRuntime";
import { ApiClient, ApiError } from "./client";

vi.mock("../diagnostics", async (original) => ({
  ...(await original<typeof import("../diagnostics")>()),
  logDiagnostic: vi.fn(),
  logCaughtDiagnostic: vi.fn(),
}));

const away = (retryAfterMs?: number) => new ApiError("The project runtime is idle or starting.", 503, undefined,
  { detail: "The project runtime is idle or starting.", code: PROJECT_RUNTIME_UNAVAILABLE, retryable: true }, retryAfterMs);

describe("project runtime classification", () => {
  it("treats only a project runtime that is idle, starting or closing as transient", () => {
    expect(isProjectRuntimeUnavailable(away())).toBe(true);
    // An execution worker from before the coded response still says it is starting.
    expect(isProjectRuntimeUnavailable(new ApiError("x", 503, undefined, { detail: "Project execution is reconnecting; accepted work remains saved" }))).toBe(true);
    expect(isProjectRuntimeUnavailable(new ApiError("x", 503, undefined, { detail: "Runtime delivery is unavailable. The saved request has not been retried." }))).toBe(false);
    expect(isProjectRuntimeUnavailable(new ApiError("x", 503, undefined, { detail: "Database capacity is busy.", code: "database_busy" }))).toBe(false);
    expect(isProjectRuntimeUnavailable(new ApiError("x", 500, undefined, { code: PROJECT_RUNTIME_UNAVAILABLE }))).toBe(false);
    expect(isProjectRuntimeUnavailable(new Error("offline"))).toBe(false);
    expect(isProjectRuntimeUnavailable(undefined)).toBe(false);
  });

  it("honours Retry-After within bounds and backs off to five seconds", () => {
    expect(parseRetryAfter("2")).toBe(2000);
    expect(parseRetryAfter("Thu, 01 Jan 1970 00:00:03 GMT", 1000)).toBe(2000);
    expect(parseRetryAfter("soon")).toBeUndefined();
    expect(parseRetryAfter(null)).toBeUndefined();
    expect([0, 1, 2, 3, 9].map(attempt => projectRuntimeRetryDelay(attempt))).toEqual([1000, 2000, 4000, 5000, 5000]);
    expect(projectRuntimeRetryDelay(0, 3000)).toBe(3000);
    expect(projectRuntimeRetryDelay(3, 60_000)).toBe(10_000);
  });
});

describe("ProjectRuntimeRecovery", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  });

  it("probes the latest failed read with backoff and releases every waiting view when it answers", async () => {
    const probe = vi.fn()
      .mockResolvedValueOnce({ answered: false })
      .mockResolvedValueOnce({ answered: true });
    const recovery = new ProjectRuntimeRecovery(probe);
    const states: string[] = [];
    recovery.subscribe(state => states.push(state));
    recovery.unavailable("chat/sessions/a/bookmarks");
    recovery.unavailable("/chat/sessions/a/queue?x=1");
    const first = vi.fn(), second = vi.fn();
    void recovery.waitForRuntime().then(first);
    void recovery.waitForRuntime().then(second);
    expect(recovery.state).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(999);
    expect(probe).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(probe).toHaveBeenCalledTimes(1);
    expect(probe.mock.calls[0][0]).toBe("/chat/sessions/a/queue?x=1");
    await vi.advanceTimersByTimeAsync(1999);
    expect(probe).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(probe).toHaveBeenCalledTimes(2);
    await vi.runAllTimersAsync();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
    expect(recovery.state).toBe("available");
    expect(states).toEqual(["reconnecting", "available"]);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(probe).toHaveBeenCalledTimes(2);
  });

  it("recovers at once when a view's own read of a failed path answers", async () => {
    const probe = vi.fn().mockResolvedValue({ answered: false });
    const recovery = new ProjectRuntimeRecovery(probe);
    recovery.unavailable("chat/sessions/a/queue");
    const done = vi.fn();
    void recovery.waitForRuntime().then(done);
    recovery.answered("health");
    await Promise.resolve();
    expect(done).not.toHaveBeenCalled();
    recovery.answered("chat/sessions/a/queue?etag=1");
    await Promise.resolve();
    expect(done).toHaveBeenCalledTimes(1);
    expect(recovery.state).toBe("available");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(probe).not.toHaveBeenCalled();
  });

  it("waits at least as long as Core's Retry-After", async () => {
    const probe = vi.fn().mockResolvedValue({ answered: true });
    const recovery = new ProjectRuntimeRecovery(probe);
    recovery.unavailable("chat/sessions/a/goal", 3000);
    void recovery.waitForRuntime();
    await vi.advanceTimersByTimeAsync(2999);
    expect(probe).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(probe).toHaveBeenCalledTimes(1);
  });

  it("stops probing when no view waits any more", async () => {
    const probe = vi.fn().mockResolvedValue({ answered: false });
    const recovery = new ProjectRuntimeRecovery(probe);
    recovery.unavailable("chat/sessions/a/goal");
    const controller = new AbortController();
    const waiting = recovery.waitForRuntime(controller.signal);
    expect(recovery.state).toBe("reconnecting");
    controller.abort();
    await expect(waiting).rejects.toMatchObject({ name: "AbortError" });
    expect(recovery.state).toBe("available");
    await vi.advanceTimersByTimeAsync(20_000);
    expect(probe).not.toHaveBeenCalled();
  });

  it("does not probe while the page is hidden and probes as soon as it returns", async () => {
    const probe = vi.fn().mockResolvedValue({ answered: true });
    const recovery = new ProjectRuntimeRecovery(probe);
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    recovery.unavailable("chat/sessions/a/goal");
    const done = vi.fn();
    void recovery.waitForRuntime().then(done);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(probe).not.toHaveBeenCalled();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.runAllTimersAsync();
    expect(probe).toHaveBeenCalledTimes(1);
    expect(done).toHaveBeenCalledTimes(1);
  });

  it("releases a view at once when nothing is failing any more", async () => {
    const recovery = new ProjectRuntimeRecovery(vi.fn());
    const done = vi.fn();
    void recovery.waitForRuntime().then(done);
    await Promise.resolve(); await Promise.resolve();
    expect(done).toHaveBeenCalledTimes(1);
  });
});

describe("ApiClient project reads", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("records a project read that found no ready worker, honours Retry-After and recovers when it answers", async () => {
    vi.useFakeTimers();
    const replies: Response[] = [
      new Response(JSON.stringify({ detail: "The project runtime is idle or starting.", code: PROJECT_RUNTIME_UNAVAILABLE, retryable: true }), { status: 503, headers: { "Retry-After": "2", "Content-Type": "application/json" } }),
      new Response(JSON.stringify({ detail: "Project execution is reconnecting; accepted work remains saved", code: PROJECT_RUNTIME_UNAVAILABLE, retryable: true }), { status: 503, headers: { "Retry-After": "1" } }),
      new Response(JSON.stringify([{ message_id: "m", active: true, revision: 1 }]), { status: 200 }),
    ];
    const fetch = vi.fn(async () => replies.shift()!);
    const api = new ApiClient({ baseUrl: "http://core/api/v1", token: "t", fetch: fetch as unknown as typeof globalThis.fetch });
    const error = await api.request("chat/sessions/a/bookmarks").catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).retryAfterMs).toBe(2000);
    expect(isProjectRuntimeUnavailable(error)).toBe(true);
    const recovered = vi.fn();
    void api.projectRuntime.waitForRuntime().then(recovered);
    expect(api.projectRuntime.state).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(String((fetch.mock.calls as unknown[][])[1][0])).toBe("http://core/api/v1/chat/sessions/a/bookmarks");
    expect(recovered).not.toHaveBeenCalled();
    await vi.runAllTimersAsync();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(recovered).toHaveBeenCalledTimes(1);
    expect(api.projectRuntime.state).toBe("available");
  });
});
