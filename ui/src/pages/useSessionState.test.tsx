import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { PROJECT_RUNTIME_UNAVAILABLE, ProjectRuntimeRecovery } from "../api/projectRuntime";
import { acceptSessionState, isPendingRequest, pendingApprovalId, useSessionState, type SessionState } from "./useSessionState";

const state = (session_id = "s", revision = 2): SessionState => ({
  schema: "nebula.session-state/v1", session_id, revision, turn_id: "t", harness_turn_id: "h",
  execution: "continuing", busy: true, detail: "Decision recorded", connection: "connected",
  actions: ["check_status", "stop"], pending: [], decisions: [],
});

/** A client whose conditional read always answers with the fake's value. */
function conditional(request: (path: string, init: RequestInit) => Promise<unknown>): ApiClient {
  return {request, requestIfChanged: (path: string, _etag: unknown, init: RequestInit) => request(path, init).then((value: unknown) => ({value}))} as unknown as ApiClient;
}

describe("authoritative session state", () => {
  it("selects the next exact approval on its owning turn, not a question or another turn", () => {
    const snapshot = state();
    snapshot.pending = [{id: "other", turn_id: "other-turn", kind: "approval", text: "Review"}, {id: "question", turn_id: "t", kind: "input", text: "Answer"}, {id: "first", turn_id: "t", kind: "approval", text: "Review"}, {id: "second", turn_id: "t", kind: "approval", text: "Review"}];
    expect(pendingApprovalId(snapshot, "t")).toBe("first");
    snapshot.pending = snapshot.pending.filter(item => item.id !== "first");
    expect(pendingApprovalId(snapshot, "t")).toBe("second");
    expect(pendingApprovalId(snapshot, "missing")).toBeUndefined();
    expect(pendingApprovalId(undefined, "t")).toBeUndefined();
  });
  it("uses the current request set rather than a cached approval or decision list", () => {
    const snapshot = state();
    expect(isPendingRequest(undefined, "request")).toBe(true);
    expect(isPendingRequest(undefined, undefined)).toBe(false);
    expect(isPendingRequest(snapshot, "request")).toBe(false);
    snapshot.pending = [{id: "second", turn_id: "t", kind: "approval", text: "Review"}];
    expect(isPendingRequest(snapshot, "request")).toBe(false);
    expect(isPendingRequest(snapshot, "second")).toBe(true);
  });
  it("ignores old revisions and responses belonging to another conversation", () => {
    const current = state();
    expect(acceptSessionState(current, state("s", 1), "s")).toBe(current);
    expect(acceptSessionState(current, state("other", 3), "s")).toBe(current);
    expect(acceptSessionState(current, state(), "s")).toBe(current);
    expect(acceptSessionState(current, state("s", 3), "s")?.revision).toBe(3);
  });
  it("does not leak a delayed snapshot across conversation switches", async () => {
    let finish: (value: SessionState) => void = () => {};
    const request = vi.fn().mockImplementationOnce(() => new Promise(resolve => {finish = resolve;})).mockResolvedValue(state("new"));
    const api = conditional(request);
    const {result, rerender} = renderHook(({id}) => useSessionState(api, id, true), {initialProps: {id: "s"}});
    rerender({id: "new"});
    await waitFor(() => expect(result.current.state?.session_id).toBe("new"));
    await act(async () => finish(state("s", 99)));
    expect(result.current.state?.session_id).toBe("new");
  });
  it("keeps status on failure and explicitly retries without mutating work", async () => {
    const request = vi.fn().mockResolvedValueOnce(state()).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(state("s", 3));
    const api = conditional(request);
    const {result} = renderHook(() => useSessionState(api, "s", true));
    await waitFor(() => expect(result.current.state?.revision).toBe(2));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.state?.revision).toBe(2);
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.state?.revision).toBe(3));
    expect(result.current.error).toBeUndefined();
    expect(request.mock.calls.every(([, options]) => !options.method)).toBe(true);
  });
  it("keeps an unchanged snapshot, backs off while idle and stops reading in a hidden tab", async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = "visible";
    Object.defineProperty(document, "visibilityState", {configurable: true, get: () => visibility});
    try {
      const idle = {...state("s", 4), execution: "complete", busy: false, actions: ["check_status" as const]};
      const etags: (string | undefined)[] = [];
      const requestIfChanged = vi.fn(async (_path: string, etag: string | undefined) => {
        etags.push(etag);
        return etag === '"state-4"' ? undefined : {value: idle, etag: '"state-4"'};
      });
      let renders = 0;
      const api = {requestIfChanged} as unknown as ApiClient;
      const {result} = renderHook(() => { renders += 1; return useSessionState(api, "s", true); });
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const first = result.current.state;
      expect(first?.revision).toBe(4);
      const settled = renders;
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      // 2, 4 then 6 s waits: seven reads in 30 s instead of sixteen.
      expect(requestIfChanged).toHaveBeenCalledTimes(7);
      expect(etags.slice(1).every(tag => tag === '"state-4"')).toBe(true);
      expect(result.current.state).toBe(first);
      expect(renders).toBe(settled);

      visibility = "hidden";
      document.dispatchEvent(new Event("visibilitychange"));
      act(() => result.current.refresh());
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(requestIfChanged).toHaveBeenCalledTimes(7);
      visibility = "visible";
      document.dispatchEvent(new Event("visibilitychange"));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(requestIfChanged).toHaveBeenCalledTimes(8);
    } finally {
      vi.useRealTimers();
      Object.defineProperty(document, "visibilityState", {configurable: true, get: () => "visible"});
    }
  });
  it("waits without a sync error while the project's worker is away and reads again when it answers", async () => {
    let away = true;
    const recovery = new ProjectRuntimeRecovery(async () => ({answered: !away}));
    const request = vi.fn(async (path: string) => {
      if (away) {
        recovery.unavailable(path);
        throw Object.assign(new Error("idle"), {status: 503, code: PROJECT_RUNTIME_UNAVAILABLE});
      }
      recovery.answered(path);
      return state();
    });
    const api = {...conditional(request), projectRuntime: recovery} as unknown as ApiClient;
    const {result} = renderHook(() => useSessionState(api, "s", true));
    await waitFor(() => expect(recovery.state).toBe("reconnecting"));
    expect(result.current.error).toBeUndefined();
    away = false;
    await waitFor(() => expect(result.current.state?.revision).toBe(2), {timeout: 4000});
    expect(result.current.error).toBeUndefined();
  });
  it("stops waiting for one conversation's worker when another conversation opens", async () => {
    const probe = vi.fn(async () => ({answered: false}));
    const recovery = new ProjectRuntimeRecovery(probe);
    const request = vi.fn(async (path: string) => {
      if (path.includes("/away/")) {
        recovery.unavailable(path);
        throw Object.assign(new Error("idle"), {status: 503, code: PROJECT_RUNTIME_UNAVAILABLE});
      }
      return state("ready-chat");
    });
    const api = {...conditional(request), projectRuntime: recovery} as unknown as ApiClient;
    const {result, rerender} = renderHook(({id}) => useSessionState(api, id, true), {initialProps: {id: "away"}});
    await waitFor(() => expect(recovery.state).toBe("reconnecting"));
    rerender({id: "ready-chat"});
    await waitFor(() => expect(result.current.state?.session_id).toBe("ready-chat"));
    // Nothing waits any more, so no notice and no probe of the other project.
    await waitFor(() => expect(recovery.state).toBe("available"));
    const probes = probe.mock.calls.length;
    await new Promise(resolve => setTimeout(resolve, 2500));
    expect(probe.mock.calls.length).toBe(probes);
  });
});
