import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { PROJECT_RUNTIME_UNAVAILABLE, ProjectRuntimeRecovery } from "../api/projectRuntime";
import { useChatQueue, type ChatQueue } from "./useChatQueue";

let visibility: DocumentVisibilityState = "visible";

function queueWith(status?: string, paused = false): ChatQueue {
  return {
    revision: status ? 2 : 0,
    paused,
    items: status ? [{ id: "item", key: "k", status, request: { messages: [{ role: "user", content: "Next" }] } }] : [],
  };
}

/** A Core fake that answers 304 whenever the caller already holds the current tag. */
function fakeCore(current: () => ChatQueue) {
  // Every read counts, whichever client method made it.
  const reads: (string | undefined)[] = [];
  const api = {
    request: vi.fn(async () => { reads.push(undefined); return structuredClone(current()); }),
    requestIfChanged: vi.fn(async (_path: string, etag: string | undefined) => {
      reads.push(etag);
      const tag = `"${JSON.stringify(current())}"`;
      return etag === tag ? undefined : { value: structuredClone(current()), etag: tag };
    }),
  } as unknown as ApiClient;
  return { api, reads };
}

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("follow-up queue polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  });
  afterEach(() => vi.useRealTimers());

  it("keeps an unchanged queue's identity and backs off while nothing moves", async () => {
    const { api, reads } = fakeCore(() => queueWith());
    let renders = 0;
    const { result } = renderHook(() => { renders += 1; return useChatQueue(api, "s"); });
    await advance(0);
    const first = result.current.queue;
    expect(first?.items).toEqual([]);
    const settledRenders = renders;

    await advance(30_000);
    // 2 s, then 4, 8 and 10 s waits instead of fifteen reads at 2 s.
    expect(reads.length).toBeLessThanOrEqual(6);
    // Every later read is conditional on what the page already shows.
    expect(reads.slice(1).every(Boolean)).toBe(true);
    expect(result.current.queue).toBe(first);
    expect(renders).toBe(settledRenders);
  });

  it("keeps the 2 s cadence while Core is dispatching", async () => {
    const { api, reads } = fakeCore(() => queueWith("queued"));
    renderHook(() => useChatQueue(api, "s"));
    await advance(0);
    await advance(10_000);
    expect(reads.length).toBe(6);
  });

  it("does not read while the page is hidden and catches up when it returns", async () => {
    let queue = queueWith();
    const { api, reads } = fakeCore(() => queue);
    const { result } = renderHook(() => useChatQueue(api, "s"));
    await advance(0);
    expect(reads.length).toBe(1);

    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(60_000);
    expect(reads.length).toBe(1);

    queue = queueWith("needs_review", true);
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(0);
    expect(reads.length).toBe(2);
    expect(result.current.queue?.items[0]?.status).toBe("needs_review");
  });

  it("waits quietly for an idle project's worker and reads again as soon as it answers", async () => {
    // A retired project worker: Core answers project reads 503 until the
    // operator's next action readmits one. That is not a queue failure.
    let away = true;
    const recovery = new ProjectRuntimeRecovery(async () => ({ answered: !away }));
    const reads: string[] = [];
    const api = {
      projectRuntime: recovery,
      requestIfChanged: vi.fn(async (path: string) => {
        reads.push(path);
        if (away) {
          recovery.unavailable(path);
          throw new ApiError("idle", 503, undefined, { detail: "idle", code: PROJECT_RUNTIME_UNAVAILABLE, retryable: true });
        }
        recovery.answered(path);
        return { value: queueWith("queued"), etag: "tag" };
      }),
    } as unknown as ApiClient;
    const { result } = renderHook(() => useChatQueue(api, "s"));
    await advance(0);
    expect(result.current.error).toBeUndefined();
    await advance(3_000);
    expect(result.current.error).toBeUndefined();
    const beforeReady = reads.length;
    away = false;
    // The shared probe notices the worker within its backoff and the queue reads at once.
    await advance(5_000);
    expect(reads.length).toBeGreaterThan(beforeReady);
    expect(result.current.queue?.items[0]?.status).toBe("queued");
    expect(result.current.error).toBeUndefined();
    expect(recovery.state).toBe("available");
  });

  it("clears a failed read once a later read succeeds, unchanged or not", async () => {
    let fail = true;
    const api = {
      requestIfChanged: vi.fn(async (_path: string, etag: string | undefined) => {
        if (fail) throw new Error("Core read failed");
        return etag === "tag" ? undefined : { value: queueWith(), etag: "tag" };
      }),
    } as unknown as ApiClient;
    const { result } = renderHook(() => useChatQueue(api, "s"));
    await advance(0);
    expect(result.current.error).toContain("Core read failed");
    fail = false;
    await advance(2_000);
    expect(result.current.error).toBeUndefined();
  });
});
