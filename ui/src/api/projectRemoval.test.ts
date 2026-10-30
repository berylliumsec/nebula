import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

it("passes Butler's project-create retry ID to Core", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
    id: "project-1", name: "Project", status: "draft", revision: 1, metadata: {},
  }), {status: 201}));
  const api = new ApiClient({baseUrl: "http://localhost:8765", fetch: fetchMock});
  await api.createEngagement({name: "Project", requestId: "same-action"});
  const [, init] = fetchMock.mock.calls[0];
  expect(JSON.parse(String(init?.body))).toMatchObject({
    name: "Project", request_id: "same-action", host_access_acknowledged: false,
  });
});

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

it("deletes an archive using the current revision", async () => {
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(new Response(JSON.stringify({ id: "p", status: "archived", revision: 8 })))
    .mockResolvedValueOnce(new Response(null, { status: 204 }));
  const api = new ApiClient({ baseUrl: "http://localhost:8765", fetch: fetchMock });
  await api.deleteArchivedEngagement("p");
  const [, init] = fetchMock.mock.calls[1];
  expect(init?.method).toBe("DELETE");
  expect(new Headers(init?.headers).get("If-Match")).toBe("8");
});

it("refuses deletion if another operator restored the project", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify({ id: "p", status: "active", revision: 9 })));
  const api = new ApiClient({ baseUrl: "http://localhost:8765", fetch: fetchMock });
  await expect(api.deleteArchivedEngagement("p")).rejects.toThrow("Archive the project");
  expect(fetchMock).toHaveBeenCalledOnce();
});

it("refreshes safely when a saved deletion is retried", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Not found" }), { status: 404 }));
  const api = new ApiClient({ baseUrl: "http://localhost:8765", fetch: fetchMock });
  await expect(api.deleteArchivedEngagement("p")).resolves.toBeUndefined();
  expect(fetchMock).toHaveBeenCalledOnce();
});
