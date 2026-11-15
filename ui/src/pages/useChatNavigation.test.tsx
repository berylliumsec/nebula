import {act, renderHook, waitFor} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import {ApiError, type ApiClient} from "../api/client";
import {PROJECT_RUNTIME_UNAVAILABLE, ProjectRuntimeRecovery} from "../api/projectRuntime";
import {notifyProjectDataChanged} from "../api/projectDataEvents";
import {useChatNavigation} from "./useChatNavigation";

describe("saved bookmark refresh", () => {
  it("rereads only its project and session and removes the listener on unmount", async () => {
    const request = vi.fn().mockResolvedValue([]);
    const api = {request} as unknown as ApiClient;
    const hook = renderHook(() => useChatNavigation(api, "selected", "chat"));
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
    request.mockResolvedValue([{message_id: "message", active: true, revision: 3}]);
    act(() => {
      notifyProjectDataChanged({resource: "bookmarks", projectId: "other", sessionId: "chat"});
      notifyProjectDataChanged({resource: "bookmarks", projectId: "selected", sessionId: "other-chat"});
      notifyProjectDataChanged({resource: "results", projectId: "selected", resultId: "result"});
    });
    expect(request).toHaveBeenCalledTimes(1);
    act(() => notifyProjectDataChanged({resource: "bookmarks", projectId: "selected", sessionId: "chat"}));
    await waitFor(() => expect(hook.result.current.bookmarks).toEqual([{message_id: "message", active: true, revision: 3}]));
    expect(request).toHaveBeenCalledTimes(2);
    hook.unmount();
    act(() => notifyProjectDataChanged({resource: "bookmarks", projectId: "selected", sessionId: "chat"}));
    expect(request).toHaveBeenCalledTimes(2);
  });
});

describe("bookmarks while the project's worker is away", () => {
  it("load without an error or reload once the worker answers again", async () => {
    let ready = false;
    const recovery = new ProjectRuntimeRecovery(async () => ({answered: ready}));
    const request = vi.fn(async (path: string) => {
      if (!ready) {
        recovery.unavailable(path);
        throw new ApiError("idle", 503, undefined, {detail: "idle", code: PROJECT_RUNTIME_UNAVAILABLE, retryable: true});
      }
      recovery.answered(path);
      return [{message_id: "message", active: true, revision: 1}];
    });
    const api = {request, projectRuntime: recovery} as unknown as ApiClient;
    const hook = renderHook(() => useChatNavigation(api, "project", "chat"));
    await waitFor(() => expect(recovery.state).toBe("reconnecting"));
    expect(hook.result.current.error).toBeUndefined();
    ready = true;
    await waitFor(() => expect(hook.result.current.bookmarks).toEqual([{message_id: "message", active: true, revision: 1}]), {timeout: 4000});
    expect(hook.result.current.error).toBeUndefined();
    expect(recovery.state).toBe("available");
  });

  it("still reports a real failure", async () => {
    const request = vi.fn().mockRejectedValue(new Error("Bookmarks are corrupt"));
    const api = {request} as unknown as ApiClient;
    const hook = renderHook(() => useChatNavigation(api, "project", "chat"));
    await waitFor(() => expect(hook.result.current.error).toContain("Bookmarks are corrupt"));
  });
});
