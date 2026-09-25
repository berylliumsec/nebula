import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
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
});
