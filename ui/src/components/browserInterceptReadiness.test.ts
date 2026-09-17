import { afterEach, describe, expect, it, vi } from "vitest";
import {
  browserInterceptListenerReady,
  clearBrowserInterceptListenerReady,
  markBrowserInterceptListenerReady,
  waitForBrowserInterceptListener,
} from "./browserInterceptReadiness";

describe("browser interception listener readiness", () => {
  afterEach(() => {
    clearBrowserInterceptListenerReady();
    vi.useRealTimers();
  });

  it("releases navigation only after the durable intercept listener is ready", async () => {
    const waiting = waitForBrowserInterceptListener();
    expect(browserInterceptListenerReady()).toBe(false);
    markBrowserInterceptListenerReady();
    await expect(waiting).resolves.toBeUndefined();
    expect(browserInterceptListenerReady()).toBe(true);
  });

  it("provides recovery when listener registration never completes", async () => {
    vi.useFakeTimers();
    const waiting = waitForBrowserInterceptListener(25);
    const rejection = expect(waiting).rejects.toThrow("Browser interception is still starting");
    await vi.advanceTimersByTimeAsync(25);
    await rejection;
  });
});
