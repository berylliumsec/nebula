import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { sameJson, startVisiblePoll, type PollOutcome } from "./visiblePoll";

let visibility: DocumentVisibilityState = "visible";

function setVisibility(next: DocumentVisibilityState) {
  visibility = next;
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("visible polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads nothing while the page is hidden and reads at once when it returns", async () => {
    const read = vi.fn(async (): Promise<PollOutcome> => "active");
    const controller = new AbortController();
    startVisiblePoll({ read, intervalMs: 2_000, signal: controller.signal });
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(1);

    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(1);

    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(read).toHaveBeenCalledTimes(3);
    controller.abort();
  });

  it("backs off while idle, up to its ceiling, and returns to the base cadence on activity", async () => {
    let outcome: PollOutcome = "idle";
    const at: number[] = [];
    const read = vi.fn(async (): Promise<PollOutcome> => { at.push(Date.now()); return outcome; });
    const controller = new AbortController();
    const start = Date.now();
    startVisiblePoll({ read, intervalMs: 1_000, maxIntervalMs: 4_000, signal: controller.signal });
    await vi.advanceTimersByTimeAsync(15_000);
    // Reads at 0, then waits of 2, 4, 4, 4 seconds.
    expect(at.map(value => value - start)).toEqual([0, 2_000, 6_000, 10_000, 14_000]);

    outcome = "active";
    await vi.advanceTimersByTimeAsync(4_000);
    const beforeActive = at.length;
    await vi.advanceTimersByTimeAsync(3_000);
    expect(at.length - beforeActive).toBe(3);
    controller.abort();
  });

  it("pokes read now, reset shortens the wait, and stop or abort ends the poll", async () => {
    const read = vi.fn(async (): Promise<PollOutcome> => "idle");
    const controller = new AbortController();
    const poll = startVisiblePoll({ read, intervalMs: 1_000, maxIntervalMs: 8_000, signal: controller.signal });
    await vi.advanceTimersByTimeAsync(7_000);
    const settled = read.mock.calls.length;
    poll.poke();
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(settled + 1);
    // The next wait after a poke's idle answer is the first backoff step.
    await vi.advanceTimersByTimeAsync(2_000);
    expect(read).toHaveBeenCalledTimes(settled + 2);
    await vi.advanceTimersByTimeAsync(1_000);
    poll.reset();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(read).toHaveBeenCalledTimes(settled + 3);

    controller.abort();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(read).toHaveBeenCalledTimes(settled + 3);

    const stopping = vi.fn(async (): Promise<PollOutcome> => "stop");
    startVisiblePoll({ read: stopping, intervalMs: 1_000, signal: new AbortController().signal });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(stopping).toHaveBeenCalledTimes(1);
  });

  it("runs one read at a time and repeats a poke that arrived mid-read", async () => {
    let finish = () => {};
    const read = vi.fn(() => new Promise<PollOutcome>(resolve => { finish = () => resolve("active"); }));
    const controller = new AbortController();
    const poll = startVisiblePoll({ read, intervalMs: 5_000, signal: controller.signal });
    await vi.advanceTimersByTimeAsync(0);
    poll.poke();
    poll.poke();
    expect(read).toHaveBeenCalledTimes(1);
    finish();
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(2);
    controller.abort();
  });

  it("compares content, not identity", () => {
    expect(sameJson({ a: [1, 2] }, { a: [1, 2] })).toBe(true);
    expect(sameJson({ a: [1, 2] }, { a: [2, 1] })).toBe(false);
    expect(sameJson(undefined, undefined)).toBe(true);
  });
});
