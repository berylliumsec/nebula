import { act, render, renderHook } from "@testing-library/react";
import { useLayoutEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HealthResponse } from "../api/types";
import { useCoreConnectionMonitor } from "./useCoreConnectionMonitor";

const healthy = {status: "ok"} as HealthResponse;
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("Core reachability monitoring", () => {
  it("observes an offline event by the time the ready view commits", () => {
    const api = {health: vi.fn().mockResolvedValue(healthy)};
    const update = vi.fn(), fail = vi.fn();
    function ReadyView() {
      useCoreConnectionMonitor(api, true, update, fail);
      useLayoutEffect(() => { window.dispatchEvent(new Event("offline")); }, []);
      return null;
    }
    render(<ReadyView />);
    expect(fail).toHaveBeenCalledTimes(1);
    expect(api.health).not.toHaveBeenCalled();
  });

  it("refreshes degraded health without treating optional feature loss as disconnection", async () => {
    const api = {health: vi.fn().mockResolvedValue({...healthy, status: "degraded"})};
    const update = vi.fn(), fail = vi.fn();
    renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
    expect(api.health).not.toHaveBeenCalled();
    await advance(5_000);
    expect(update).toHaveBeenCalledWith({...healthy, status: "degraded"});
    expect(fail).not.toHaveBeenCalled();
    await advance(5_000);
    expect(api.health).toHaveBeenCalledTimes(2);
  });

  it("reports loss once, ignores a late response and resumes only after a fresh probe", async () => {
    let finish!: (health: HealthResponse) => void;
    const api = {health: vi.fn(() => new Promise<HealthResponse>(resolve => {finish = resolve;}))};
    const update = vi.fn(), fail = vi.fn();
    renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
    await advance(5_000);
    act(() => window.dispatchEvent(new Event("offline")));
    await act(async () => finish(healthy));
    expect(fail).toHaveBeenCalledTimes(1);
    expect(update).not.toHaveBeenCalled();
    expect(api.health).toHaveBeenCalledTimes(1);
    api.health.mockResolvedValue(healthy);
    await advance(5_000);
    expect(api.health).toHaveBeenCalledTimes(2);
    expect(update).toHaveBeenCalledWith(healthy);
  });

  it("confirms three hung probes before reporting loss and ignores their eventual results", async () => {
    let finish!: (health: HealthResponse) => void;
    let signal!: AbortSignal;
    const api = {health: vi.fn((next?: AbortSignal) => {signal = next!; return new Promise<HealthResponse>(resolve => {finish = resolve;});})};
    const update = vi.fn(), fail = vi.fn();
    renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
    await advance(10_000);
    expect(signal.aborted).toBe(true);
    expect(fail).not.toHaveBeenCalled();
    await act(async () => finish(healthy));
    expect(update).not.toHaveBeenCalled();
    await advance(6_000);
    expect(fail).not.toHaveBeenCalled();
    await advance(6_000);
    expect(api.health).toHaveBeenCalledTimes(3);
    expect(fail).toHaveBeenCalledTimes(1);
  });

  it("keeps the workspace available when a failed probe is followed by success", async () => {
    const api = {health: vi.fn()
      .mockRejectedValueOnce(new Error("transient LAN failure"))
      .mockResolvedValue(healthy)};
    const update = vi.fn(), fail = vi.fn();
    renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
    await advance(5_000);
    expect(fail).not.toHaveBeenCalled();
    await advance(1_000);
    expect(update).toHaveBeenCalledWith(healthy);
    expect(fail).not.toHaveBeenCalled();
  });

  it("does not apply an old endpoint's failure after reconnect", async () => {
    let reject!: (error: Error) => void;
    const old = {health: vi.fn(() => new Promise<HealthResponse>((_, no) => {reject = no;}))};
    const next = {health: vi.fn().mockResolvedValue(healthy)};
    const update = vi.fn(), fail = vi.fn();
    const {rerender} = renderHook(({api}) => useCoreConnectionMonitor(api, true, update, fail), {initialProps: {api: old}});
    await advance(5_000);
    rerender({api: next});
    await act(async () => reject(new Error("old endpoint")));
    await advance(5_000);
    expect(fail).not.toHaveBeenCalled();
    expect(update).toHaveBeenCalledWith(healthy);
  });

  it("coalesces visibility/online probes and suspends during bootstrap", async () => {
    const api = {health: vi.fn().mockRejectedValue(new Error("offline"))};
    const update = vi.fn(), fail = vi.fn();
    const {rerender} = renderHook(({enabled}) => useCoreConnectionMonitor(api, enabled, update, fail), {initialProps: {enabled: false}});
    await advance(10_000);
    expect(api.health).not.toHaveBeenCalled();
    rerender({enabled: true});
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(api.health).toHaveBeenCalledTimes(1);
    expect(fail).not.toHaveBeenCalled();
    await advance(2_000);
    expect(api.health).toHaveBeenCalledTimes(3);
    expect(fail).toHaveBeenCalledTimes(1);
  });

  describe("with liveness and traffic evidence", () => {
    type Health = (signal?: AbortSignal) => Promise<HealthResponse>;
    type Liveness = (signal?: AbortSignal) => Promise<void>;
    const monitored = (overrides: {health?: Health; liveness?: Liveness; lastReachableAt?: () => number} = {}) => ({
      health: vi.fn<Health>(overrides.health ?? (async () => healthy)),
      liveness: vi.fn<Liveness>(overrides.liveness ?? (async () => undefined)),
      lastReachableAt: overrides.lastReachableAt ?? (() => 0),
    });

    it("sends no probe while the workspace's own requests are answered", async () => {
      const api = monitored({lastReachableAt: () => Date.now()});
      const update = vi.fn(), fail = vi.fn();
      renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
      await advance(25_000);
      expect(api.liveness).not.toHaveBeenCalled();
      expect(api.health).not.toHaveBeenCalled();
      await advance(5_000);
      expect(api.health).toHaveBeenCalledTimes(1);
      expect(update).toHaveBeenCalledWith(healthy);
    });

    it("probes liveness when idle and reads full health only every thirty seconds", async () => {
      const api = monitored();
      const update = vi.fn(), fail = vi.fn();
      renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
      await advance(25_000);
      expect(api.liveness).toHaveBeenCalledTimes(5);
      expect(api.health).not.toHaveBeenCalled();
      await advance(5_000);
      expect(api.health).toHaveBeenCalledTimes(1);
      expect(fail).not.toHaveBeenCalled();
    });

    it("keeps polling full health while Core reports its API not ready", async () => {
      const api = monitored({health: async () => ({...healthy, apiReady: false})});
      const update = vi.fn(), fail = vi.fn();
      renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
      await advance(30_000);
      expect(api.health).toHaveBeenCalledTimes(1);
      await advance(10_000);
      expect(api.health).toHaveBeenCalledTimes(3);
    });

    it("does not report a busy Core as lost when only full health times out", async () => {
      const api = monitored({health: () => new Promise<HealthResponse>(() => undefined)});
      const update = vi.fn(), fail = vi.fn();
      renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
      await advance(120_000);
      expect(api.health.mock.calls.length).toBeGreaterThanOrEqual(3);
      expect(api.health.mock.calls.length).toBeLessThanOrEqual(4);
      expect(fail).not.toHaveBeenCalled();
    });

    it("reports loss after three failed liveness probes and recovers through full health", async () => {
      const api = monitored({liveness: async () => { throw new Error("unreachable"); }});
      const update = vi.fn(), fail = vi.fn();
      renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
      await advance(5_000);
      await advance(1_000);
      expect(fail).not.toHaveBeenCalled();
      await advance(1_000);
      expect(api.liveness).toHaveBeenCalledTimes(3);
      expect(fail).toHaveBeenCalledTimes(1);
      expect(api.health).not.toHaveBeenCalled();
      await advance(5_000);
      expect(api.health).toHaveBeenCalledTimes(1);
      expect(update).toHaveBeenCalledWith(healthy);
    });
  });
});
