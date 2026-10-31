import {expect, it, vi} from "vitest";
import {ApiClient} from "./client";

const report = {id: "draft-1", engagement_id: "project-1", title: "Draft", status: "draft", revision: 3};
const missing = () => new Response(JSON.stringify({detail: "Report not found"}), {status: 404});
const saved = () => new Response(JSON.stringify(report), {status: 200});

it.each([false, true])("deletes the exact report revision and confirms absence after a lost response=%s", async (lost) => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(saved());
  if (lost) fetchMock.mockRejectedValueOnce(new Error("response lost"));
  else fetchMock.mockResolvedValueOnce(new Response(null, {status: 204}));
  fetchMock.mockResolvedValueOnce(missing());
  const api = new ApiClient({baseUrl: "http://localhost:8765", fetch: fetchMock});
  await expect(api.deleteReportAtRevision(report.id, report.engagement_id, report.revision)).resolves.toBeUndefined();
  const [url, init] = fetchMock.mock.calls[1];
  expect(url).toBe("http://localhost:8765/api/v1/reports/draft-1");
  expect(init?.method).toBe("DELETE");
  expect(new Headers(init?.headers).get("If-Match")).toBe("3");
});

it("refuses a stale revision before sending DELETE", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(saved());
  const api = new ApiClient({baseUrl: "http://localhost:8765", fetch: fetchMock});
  await expect(api.deleteReportAtRevision(report.id, report.engagement_id, report.revision - 1)).rejects.toThrow("changed");
  expect(fetchMock).toHaveBeenCalledOnce();
});

it("reports a Core conflict while the report remains readable", async () => {
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(saved())
    .mockResolvedValueOnce(new Response(JSON.stringify({detail: "report is referenced by a render"}), {status: 409}))
    .mockResolvedValueOnce(saved());
  const api = new ApiClient({baseUrl: "http://localhost:8765", fetch: fetchMock});
  await expect(api.deleteReportAtRevision(report.id, report.engagement_id, report.revision)).rejects.toThrow();
  expect(fetchMock).toHaveBeenCalledTimes(3);
});
