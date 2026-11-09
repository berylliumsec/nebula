import {expect, it, vi} from "vitest";
import {ApiClient} from "./client";

it("keeps ordinary Storage reads compatible and encodes explicit bounded pages", async () => {
  const fetch = vi.fn<typeof globalThis.fetch>().mockImplementation(async () => new Response(JSON.stringify({sources: [], devices: [], volumes: [], incidents: [], actions: [], dispatches: []})));
  const client = new ApiClient({baseUrl: "https://synthetic.test", fetch});
  await client.storageDashboard();
  await client.storageDashboard(undefined, {offset: 4, limit: 2});
  expect(String(fetch.mock.calls[0][0])).toBe("https://synthetic.test/api/v1/storage");
  expect(String(fetch.mock.calls[1][0])).toBe("https://synthetic.test/api/v1/storage?offset=4&limit=2");
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
});
