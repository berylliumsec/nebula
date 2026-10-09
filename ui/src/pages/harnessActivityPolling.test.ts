import { afterEach, describe, expect, it, vi } from "vitest";
import { startHarnessActivityPolling } from "./harnessActivityPolling";

afterEach(() => vi.useRealTimers());

describe("harness activity polling", () => {
  it("serializes slow reads, backs off when idle, and refreshes on tab return", async () => {
    vi.useFakeTimers();
    const visibility = new EventTarget() as EventTarget & {hidden: boolean};
    visibility.hidden = false;
    const replies: Array<(value: {busy: boolean}) => void> = [];
    const read = vi.fn(() => new Promise<{busy: boolean}>(resolve => replies.push(resolve)));
    const onValue = vi.fn();
    const onError = vi.fn();
    const stop = startHarnessActivityPolling({read, onValue, onError, activeTurn: false, visibility});

    expect(read).toHaveBeenCalledTimes(1);
    visibility.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(1);
    replies.shift()!({busy: false});
    await vi.advanceTimersByTimeAsync(0);
    expect(onValue).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledTimes(2);

    replies.shift()!({busy: false});
    await vi.advanceTimersByTimeAsync(9_999);
    expect(read).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(read).toHaveBeenCalledTimes(3);
    replies.shift()!({busy: true});
    await vi.advanceTimersByTimeAsync(2_000);
    expect(read).toHaveBeenCalledTimes(4);

    visibility.hidden = true;
    visibility.dispatchEvent(new Event("visibilitychange"));
    replies.shift()!({busy: true});
    await vi.advanceTimersByTimeAsync(30_000);
    expect(read).toHaveBeenCalledTimes(4);
    expect(onValue).toHaveBeenCalledTimes(3);
    visibility.hidden = false;
    visibility.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(read).toHaveBeenCalledTimes(5);
    stop();
    expect(onError).not.toHaveBeenCalled();
  });
});
