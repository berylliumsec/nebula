import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProjectViewLease } from "./useProjectViewLease";
import { ProjectRuntimeRecovery } from "../api/projectRuntime";

vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

let visibility: DocumentVisibilityState = "visible";

function fakeApi(runtime: "ready" | "starting" | "held" = "ready") {
  const holds: { projectId: string; leaseId: string }[] = [];
  const releases: { projectId: string; leaseId: string }[] = [];
  const api = {
    holdProjectView: vi.fn(async (projectId: string, leaseId: string) => {
      holds.push({ projectId, leaseId });
      return { leaseId, projectId, expiresAt: "2026-11-14T00:00:00Z", renewAfterSeconds: 30, runtime: { state: runtime } };
    }),
    releaseProjectView: vi.fn(async (projectId: string, leaseId: string) => { releases.push({ projectId, leaseId }); }),
    projectRuntime: { nudge: vi.fn() },
  };
  return { api, holds, releases };
}

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

describe("project view lease", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  });
  afterEach(() => vi.useRealTimers());

  it("holds the open project's lease, renews it, and releases it on leaving", async () => {
    const { api, holds, releases } = fakeApi();
    const view = renderHook(({ projectId }) => useProjectViewLease(api as never, projectId), { initialProps: { projectId: "one" as string | undefined } });
    await advance(0);
    expect(holds).toHaveLength(1);
    expect(holds[0].leaseId).toMatch(/^[A-Za-z0-9_-]{8,100}$/);
    expect(api.projectRuntime.nudge).toHaveBeenCalledTimes(1);
    await advance(29_999);
    expect(holds).toHaveLength(1);
    await advance(1);
    expect(holds).toHaveLength(2);
    // The same lease is renewed, never a new one per renewal.
    expect(holds[1].leaseId).toBe(holds[0].leaseId);
    view.rerender({ projectId: "two" });
    await advance(0);
    expect(releases).toEqual([{ projectId: "one", leaseId: holds[0].leaseId }]);
    expect(holds.at(-1)?.projectId).toBe("two");
    view.rerender({ projectId: undefined });
    await advance(60_000);
    expect(holds.filter(item => item.projectId === "two")).toHaveLength(1);
    expect(releases.at(-1)?.projectId).toBe("two");
  });

  it("ends the lease when the page goes away and holds it again when restored from cache", async () => {
    const { api, holds, releases } = fakeApi();
    renderHook(() => useProjectViewLease(api as never, "one"));
    await advance(0);
    act(() => { window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })); });
    expect(releases).toEqual([{ projectId: "one", leaseId: holds[0].leaseId }]);
    await advance(60_000);
    expect(holds).toHaveLength(1);
    act(() => { window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })); });
    await advance(0);
    expect(holds).toHaveLength(2);
    expect(holds[1].leaseId).toBe(holds[0].leaseId);
  });

  it("lets the lease lapse while the page is hidden and takes it again on return", async () => {
    const { api, holds } = fakeApi();
    renderHook(() => useProjectViewLease(api as never, "one"));
    await advance(0);
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(5 * 60_000);
    expect(holds).toHaveLength(1);
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await advance(0);
    expect(holds).toHaveLength(2);
  });

  it("retries a failed hold sooner than a renewal", async () => {
    const { api, holds } = fakeApi();
    api.holdProjectView.mockRejectedValueOnce(new Error("offline"));
    renderHook(() => useProjectViewLease(api as never, "one"));
    await advance(0);
    expect(api.holdProjectView).toHaveBeenCalledTimes(1);
    expect(holds).toHaveLength(0);
    await advance(10_000);
    expect(holds).toHaveLength(1);
  });

  it("asks again soon while Core reports the worker starting, then renews normally", async () => {
    const { api, holds } = fakeApi("starting");
    renderHook(() => useProjectViewLease(api as never, "one"));
    await advance(0);
    expect(holds).toHaveLength(1);
    // 2 s, 4 s, 8 s, then every 10 s until the worker is up.
    await advance(2_000);
    expect(holds).toHaveLength(2);
    await advance(4_000);
    expect(holds).toHaveLength(3);
    await advance(8_000);
    expect(holds).toHaveLength(4);
    await advance(10_000);
    expect(holds).toHaveLength(5);
    api.holdProjectView.mockImplementation(async (projectId: string, leaseId: string) => {
      holds.push({ projectId, leaseId });
      return { leaseId, projectId, expiresAt: "2026-11-14T00:00:00Z", renewAfterSeconds: 30, runtime: { state: "ready" as const } };
    });
    await advance(10_000);
    expect(holds).toHaveLength(6);
    await advance(29_999);
    expect(holds).toHaveLength(6);
    await advance(1);
    expect(holds).toHaveLength(7);
    // Every renewal keeps the same lease.
    expect(new Set(holds.map(item => item.leaseId)).size).toBe(1);
  });

  it("tells the shared project-runtime recovery what the lease says about the worker", async () => {
    const recovery = new ProjectRuntimeRecovery(vi.fn().mockResolvedValue({ answered: true }));
    const { api } = fakeApi("starting");
    const withRecovery = { ...api, projectRuntime: recovery };
    const view = renderHook(({ projectId }) => useProjectViewLease(withRecovery as never, projectId), { initialProps: { projectId: "one" as string | undefined } });
    // Wanted from the moment the view opens: an "idle" read of this project is only Core not having seen the lease yet.
    recovery.unavailable("engagements/one/automation-policy", undefined, "idle");
    const waiting = recovery.waitForRuntime();
    expect(recovery.state).toBe("reconnecting");
    await advance(1_000);
    await waiting;
    expect(recovery.state).toBe("available");
    // Next renewal: Core still reports the worker starting. A write sent now is in progress, not stuck.
    await advance(2_000);
    recovery.writeStarted();
    expect(recovery.state).toBe("reconnecting");
    expect(recovery.waitingForWrite).toBe(true);
    recovery.writeSettled();
    expect(recovery.state).toBe("available");
    view.rerender({ projectId: undefined });
    recovery.unavailable("engagements/one/automation-policy", undefined, "idle");
    void recovery.waitForRuntime().catch(() => undefined);
    // Nothing holds the project any more: it really is idle.
    expect(recovery.state).toBe("idle");
  });

  it("marks a refused lease so an archived project is not described as starting", async () => {
    const recovery = new ProjectRuntimeRecovery(vi.fn().mockResolvedValue({ answered: false }));
    const { api } = fakeApi();
    api.holdProjectView.mockRejectedValue(Object.assign(new Error("archived"), { status: 409 }));
    renderHook(() => useProjectViewLease({ ...api, projectRuntime: recovery } as never, "one"));
    await advance(0);
    recovery.unavailable("engagements/one/notes", undefined, "idle");
    void recovery.waitForRuntime().catch(() => undefined);
    expect(recovery.state).toBe("idle");
  });

  it("holds nothing outside a project", async () => {
    const { api } = fakeApi();
    renderHook(() => useProjectViewLease(api as never, undefined));
    await advance(60_000);
    expect(api.holdProjectView).not.toHaveBeenCalled();
  });
});
