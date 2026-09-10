import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HealthResponse } from "../api/types";
import { useCoreConnectionMonitor } from "./useCoreConnectionMonitor";

const healthy = {status: "ok"} as HealthResponse;
const advance = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("Core reachability monitoring", () => {
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

  it("bounds hung probes and ignores their eventual result", async () => {
    let finish!: (health: HealthResponse) => void;
    let signal!: AbortSignal;
    const api = {health: vi.fn((next?: AbortSignal) => {signal = next!; return new Promise<HealthResponse>(resolve => {finish = resolve;});})};
    const update = vi.fn(), fail = vi.fn();
    renderHook(() => useCoreConnectionMonitor(api, true, update, fail));
    await advance(10_000);
    expect(signal.aborted).toBe(true);
    expect(fail).toHaveBeenCalledTimes(1);
    await act(async () => finish(healthy));
    expect(update).not.toHaveBeenCalled();
    api.health.mockResolvedValue(healthy);
    await advance(5_000);
    expect(update).toHaveBeenCalledWith(healthy);
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
    expect(fail).toHaveBeenCalledTimes(1);
  });
});
