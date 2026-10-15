import type { ApiClient } from "./client";

export type ButlerMemoryScope = "global_preference" | "global_lesson" | "project_lesson";

export interface ButlerMemoryItem {
  id: string;
  scope: ButlerMemoryScope;
  engagementId?: string;
  content: string;
  sourceLabel: string;
  sourceRef?: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
}

export interface ButlerMemoryResult {
  items: ButlerMemoryItem[];
  indexStatus: { state: "vector_ready" | "lexical_fallback"; detail: string };
}

type WireItem = {
  id: string;
  scope: ButlerMemoryScope;
  engagement_id: string | null;
  content: string;
  source_label: string;
  source_ref: string | null;
  created_at: string;
  updated_at: string;
  revision: number;
};

type WireResult = { items: WireItem[]; index_status: ButlerMemoryResult["indexStatus"] };

function fromWire(item: WireItem): ButlerMemoryItem {
  return {
    id: item.id,
    scope: item.scope,
    engagementId: item.engagement_id ?? undefined,
    content: item.content,
    sourceLabel: item.source_label,
    sourceRef: item.source_ref ?? undefined,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
    revision: item.revision,
  };
}

/** Memory reads are always scoped to the selected project, even for global items. */
export async function readButlerMemory(api: ApiClient, projectId: string, query = "", signal?: AbortSignal): Promise<ButlerMemoryResult> {
  const wire = query.trim()
    ? await api.request<WireResult>("butler/memory/search", {
      method: "POST", body: JSON.stringify({ engagement_id: projectId, query: query.trim(), limit: 50 }), signal,
    })
    : await api.request<WireResult>(`butler/memory?engagement_id=${encodeURIComponent(projectId)}&limit=50`, { signal });
  return { items: wire.items.map(fromWire), indexStatus: wire.index_status };
}

export async function updateButlerMemory(api: ApiClient, projectId: string, item: ButlerMemoryItem, content: string): Promise<ButlerMemoryItem> {
  const wire = await api.request<WireItem>(`butler/memory/${encodeURIComponent(item.id)}`, {
    method: "PATCH", body: JSON.stringify({ engagement_id: projectId, content, expected_revision: item.revision }),
  });
  return fromWire(wire);
}

export async function deleteButlerMemory(api: ApiClient, projectId: string, item: ButlerMemoryItem): Promise<void> {
  await api.request<void>(`butler/memory/${encodeURIComponent(item.id)}?engagement_id=${encodeURIComponent(projectId)}&expected_revision=${item.revision}`, {
    method: "DELETE",
  });
}
