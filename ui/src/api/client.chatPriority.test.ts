import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

it("saves pins and color flags with the session revision", async () => {
  const wire = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 4, id: "session-1", engagement_id: "engagement-1", title: "Scope review", metadata: { pinned: true, flag_color: "red" } };
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(new Response(JSON.stringify(wire), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...wire, revision: 5, metadata: {} }), { status: 200 }));
  const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
  expect(await client.setChatSessionPriority("session-1", { pinned: true, flagColor: "red" }, 3)).toMatchObject({ pinned: true, flagColor: "red", revision: 4 });
  expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "PATCH", body: JSON.stringify({ pinned: true, flag_color: "red", expected_revision: 3 }) });
  expect(await client.setChatSessionPriority("session-1", { pinned: false, flagColor: null }, 4)).toMatchObject({ pinned: false, flagColor: undefined, revision: 5 });
});
