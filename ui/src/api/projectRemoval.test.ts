import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

it.each([true, false])("saves project archive=%s with Core's revision contract", async (archived) => {
  const original = { id: "project/1", name: "Project", status: "paused", revision: 7, metadata: {} };
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(new Response(JSON.stringify(original)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...original, status: archived ? "archived" : "active", revision: 8 })));
  const api = new ApiClient({ baseUrl: "http://localhost:8765", fetch: fetchMock });
  const saved = await api.setEngagementArchived(original.id, archived);
  expect(saved.status).toBe(archived ? "archived" : "active");
  const [url, init] = fetchMock.mock.calls[1];
  expect(url).toBe("http://localhost:8765/api/v1/engagements/project%2F1");
  expect(init?.method).toBe("PATCH");
  expect(JSON.parse(String(init?.body))).toEqual({ changes: { status: archived ? "archived" : "active" }, expected_revision: 7 });
});

it("retries a saved removal without writing another revision", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify({
    id: "project-1", name: "Project", status: "archived", revision: 8, metadata: {},
  })));
  const api = new ApiClient({ baseUrl: "http://localhost:8765", fetch: fetchMock });
  expect((await api.setEngagementArchived("project-1", true)).status).toBe("archived");
  expect(fetchMock).toHaveBeenCalledOnce();
});
