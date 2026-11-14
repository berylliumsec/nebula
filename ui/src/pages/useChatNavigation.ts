import { logCaughtDiagnostic } from "../diagnostics";
import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import {PROJECT_DATA_CHANGED_EVENT, type ProjectDataChange} from "../api/projectDataEvents";
import { isProjectRuntimeUnavailable } from "../api/projectRuntime";
import { useProjectRuntimeRecovery } from "../hooks/useProjectRuntime";

export interface ChatBookmark { message_id: string; active: boolean; revision: number }
export interface ChatSearchHit { message_id: string; session_id: string; title: string; role: string; excerpt: string; sequence: number }
export interface ChatSearchPage { items: ChatSearchHit[]; next_offset: number | null }

export function useChatNavigation(api: ApiClient | undefined, projectId: string | undefined, sessionId: string) {
  const [bookmarks, setBookmarks] = useState<ChatBookmark[]>([]);
  const [error, setError] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  // Nonzero while the project's worker is idle or starting; bookmarks read again when it answers.
  const [runtimeWait, setRuntimeWait] = useState(0);
  useProjectRuntimeRecovery(api, runtimeWait, () => { setRuntimeWait(0); setRefresh(value => value + 1); });
  useEffect(() => {
    const changed = (event: Event) => {
      const detail = (event as CustomEvent<ProjectDataChange>).detail;
      if (detail?.resource === "bookmarks" && detail.projectId === projectId && detail.sessionId === sessionId) setRefresh(value => value + 1);
    };
    window.addEventListener(PROJECT_DATA_CHANGED_EVENT, changed);
    return () => window.removeEventListener(PROJECT_DATA_CHANGED_EVENT, changed);
  }, [projectId, sessionId]);
  useEffect(() => {
    setBookmarks([]);
    setRuntimeWait(0);
    if (!api || !sessionId) return;
    const controller = new AbortController();
    void api.request<ChatBookmark[]>(`chat/sessions/${encodeURIComponent(sessionId)}/bookmarks`, {signal: controller.signal}).then(items => {
      if (controller.signal.aborted) return;
      setBookmarks(items);
      setError(undefined);
    }).catch((e: unknown) => {
      if (controller.signal.aborted) return;
      // diagnostic-expected: the client recorded this read; the shared reconnecting notice explains the wait.
      if (isProjectRuntimeUnavailable(e)) { setRuntimeWait(value => value || 1); return; }
      void logCaughtDiagnostic("interface.assistant_chat.read_failed", "Assistant chat state could not be read.", e, "assistant_chat");
      setError(e instanceof Error ? e.message : "Bookmarks could not be loaded.");
    });
    return () => controller.abort();
  }, [api, sessionId, refresh]);
  const toggleBookmark = async (messageId: string) => {
    if (!api || !sessionId) return;
    const current = bookmarks.find(item => item.message_id === messageId);
    setError(undefined);
    try {
      await api.request(`chat/sessions/${encodeURIComponent(sessionId)}/bookmarks/${encodeURIComponent(messageId)}`, {method: "PUT", body: JSON.stringify({active: !current?.active, expected_revision: current?.revision ?? 0})});
      setRefresh(value => value + 1);
    } catch (e) { void logCaughtDiagnostic("interface.assistant_chat.operation_failed", "An assistant chat operation failed.", e, "assistant_chat"); setError(e instanceof Error ? e.message : "Bookmark could not be saved."); }
  };
  const search = (q: string, bookmarked: boolean, currentOnly: boolean, offset = 0) => {
    if (!api || !projectId) return Promise.resolve<ChatSearchPage>({items: [], next_offset: null});
    const params = new URLSearchParams({q, bookmarked: String(bookmarked), offset: String(offset)});
    if (currentOnly && sessionId) params.set("session_id", sessionId);
    return api.request<ChatSearchPage>(`chat/projects/${encodeURIComponent(projectId)}/search?${params}`);
  };
  return {bookmarks, toggleBookmark, search, error, reload: () => { setError(undefined); setRefresh(value => value + 1); }};
}
