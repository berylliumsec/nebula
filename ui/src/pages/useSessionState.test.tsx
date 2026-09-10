import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { acceptSessionState, isPendingRequest, useSessionState, type SessionState } from "./useSessionState";

const state = (session_id = "s", revision = 2): SessionState => ({
  schema: "nebula.session-state/v1", session_id, revision, turn_id: "t", harness_turn_id: "h",
  execution: "continuing", busy: true, detail: "Decision recorded", connection: "connected",
  actions: ["check_status", "stop"], pending: [], decisions: [],
});

describe("authoritative session state", () => {
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
    const api = {request} as unknown as ApiClient;
    const {result, rerender} = renderHook(({id}) => useSessionState(api, id, true), {initialProps: {id: "s"}});
    rerender({id: "new"});
    await waitFor(() => expect(result.current.state?.session_id).toBe("new"));
    await act(async () => finish(state("s", 99)));
    expect(result.current.state?.session_id).toBe("new");
  });
  it("keeps status on failure and explicitly retries without mutating work", async () => {
    const request = vi.fn().mockResolvedValueOnce(state()).mockRejectedValueOnce(new Error("offline")).mockResolvedValue(state("s", 3));
    const api = {request} as unknown as ApiClient;
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
});
