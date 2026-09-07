import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";

export interface ChatBookmark { message_id: string; active: boolean; revision: number }
export interface ChatSearchHit { message_id: string; session_id: string; title: string; role: string; excerpt: string; sequence: number }
export interface ChatSearchPage { items: ChatSearchHit[]; next_offset: number | null }

export function useChatNavigation(api: ApiClient | undefined, projectId: string | undefined, sessionId: string) {
  const [bookmarks, setBookmarks] = useState<ChatBookmark[]>([]);
  const [error, setError] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    setBookmarks([]);
    if (!api || !sessionId) return;
    const controller = new AbortController();
    void api.request<ChatBookmark[]>(`chat/sessions/${encodeURIComponent(sessionId)}/bookmarks`, {signal: controller.signal}).then(setBookmarks).catch((e: unknown) => {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Bookmarks could not be loaded.");
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
    } catch (e) { setError(e instanceof Error ? e.message : "Bookmark could not be saved."); }
  };
  const search = (q: string, bookmarked: boolean, currentOnly: boolean, offset = 0) => {
    if (!api || !projectId) return Promise.resolve<ChatSearchPage>({items: [], next_offset: null});
    const params = new URLSearchParams({q, bookmarked: String(bookmarked), offset: String(offset)});
    if (currentOnly && sessionId) params.set("session_id", sessionId);
    return api.request<ChatSearchPage>(`chat/projects/${encodeURIComponent(projectId)}/search?${params}`);
  };
  return {bookmarks, toggleBookmark, search, error, reload: () => { setError(undefined); setRefresh(value => value + 1); }};
}
