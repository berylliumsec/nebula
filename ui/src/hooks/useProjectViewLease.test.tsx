import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useProjectViewLease } from "./useProjectViewLease";

vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

let visibility: DocumentVisibilityState = "visible";

function fakeApi() {
  const holds: { projectId: string; leaseId: string }[] = [];
  const releases: { projectId: string; leaseId: string }[] = [];
  const api = {
    holdProjectView: vi.fn(async (projectId: string, leaseId: string) => {
      holds.push({ projectId, leaseId });
      return { leaseId, projectId, expiresAt: "2026-11-14T00:00:00Z", renewAfterSeconds: 30, runtime: { state: "starting" as const } };
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

  it("holds nothing outside a project", async () => {
    const { api } = fakeApi();
    renderHook(() => useProjectViewLease(api as never, undefined));
    await advance(60_000);
    expect(api.holdProjectView).not.toHaveBeenCalled();
  });
});
