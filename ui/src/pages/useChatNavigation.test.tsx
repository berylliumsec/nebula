import {act, renderHook, waitFor} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
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
