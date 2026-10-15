import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

describe("recent conversation pages", () => {
  it("reads only one page until older history is requested", async () => {
    const wire = {
      id: "session-1", engagement_id: "engagement/one", title: "Recent work", backend: "provider",
      provider_profile_id: "provider-1", model: "model-1", metadata: {}, revision: 1,
      created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T11:00:00Z",
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(Array.from({ length: 100 }, (_, index) => ({ ...wire, id: `session-${index}` }))), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([wire]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
    const first = await client.listChatSessionsPage("engagement/one");
    expect(first.items).toHaveLength(100);
    expect(first.nextOffset).toBe(100);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const older = await client.listChatSessionsPage("engagement/one", first.nextOffset);
    expect(older.items.map(item => item.title)).toEqual(["Recent work"]);
    expect(older.nextOffset).toBeUndefined();
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual([
      "http://127.0.0.1:8765/api/v1/chat-sessions?engagement_id=engagement%2Fone&offset=0&limit=100&recent_first=true",
      "http://127.0.0.1:8765/api/v1/chat-sessions?engagement_id=engagement%2Fone&offset=100&limit=100&recent_first=true",
    ]);
  });
});
