import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "./client";
import { deleteButlerMemory, readButlerMemory, updateButlerMemory } from "./butlerMemory";

const item = { id: "item/1", scope: "project_lesson" as const, engagement_id: "project/a", content: "Check parser ownership",
  source_label: "Parser chat", source_ref: "chat-1", created_at: "2026-10-15T12:00:00Z", updated_at: "2026-10-15T12:00:00Z", revision: 2 };

describe("Butler memory API", () => {
  it("reads and searches in the selected project, preserving Core's source redaction", async () => {
    const request = vi.fn().mockResolvedValue({ items: [{ ...item, scope: "global_lesson", engagement_id: null, source_ref: null }],
      index_status: { state: "lexical_fallback", detail: "Vector index unavailable" } });
    const api = { request } as unknown as ApiClient;
    const signal = new AbortController().signal;
    const result = await readButlerMemory(api, "project/a", "", signal);
    expect(request).toHaveBeenCalledWith("butler/memory?engagement_id=project%2Fa&limit=50", { signal });
    expect(result.items[0]).toMatchObject({ scope: "global_lesson", engagementId: undefined, sourceRef: undefined, sourceLabel: "Parser chat" });
    expect(result.indexStatus.state).toBe("lexical_fallback");

    await readButlerMemory(api, "project/a", "  parser  ", signal);
    expect(request).toHaveBeenLastCalledWith("butler/memory/search", {
      method: "POST", body: JSON.stringify({ engagement_id: "project/a", query: "parser", limit: 50 }), signal,
    });
  });

  it("sends the saved revision for edits and deletion", async () => {
    const request = vi.fn().mockResolvedValue(item);
    const api = { request } as unknown as ApiClient;
    const mapped = (await readButlerMemory({ request: vi.fn().mockResolvedValue({ items: [item], index_status: { state: "vector_ready", detail: "" } }) } as unknown as ApiClient, "project/a")).items[0];
    await updateButlerMemory(api, "project/a", mapped, "New lesson");
    expect(request).toHaveBeenCalledWith("butler/memory/item%2F1", { method: "PATCH",
      body: JSON.stringify({ engagement_id: "project/a", content: "New lesson", expected_revision: 2 }) });
    await deleteButlerMemory(api, "project/a", mapped);
    expect(request).toHaveBeenCalledWith("butler/memory/item%2F1?engagement_id=project%2Fa&expected_revision=2", { method: "DELETE" });
  });
});
