import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

it("receives authenticated Work change signals without polling", async () => {
  const fetcher = vi.fn(async () => new Response(
    "event: ready\ndata: {}\n\n: keep-alive\n\nevent: change\ndata: {\"kind\":\"work\"}\n\nevent: change\ndata: {\"kind\":\"projects\"}\n\n",
    { headers: { "content-type": "text/event-stream" } },
  ));
  const api = new ApiClient({ baseUrl: "http://localhost:8000/api/v1", token: "test-token", fetch: fetcher as typeof fetch });
  const ready = vi.fn();
  const change = vi.fn();

  await api.watchWorkChanges(change, ready, new AbortController().signal);

  expect(ready).toHaveBeenCalledOnce();
  expect(change.mock.calls).toEqual([["work"], ["projects"]]);
  expect(fetcher).toHaveBeenCalledOnce();
  const [url, options] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe("http://localhost:8000/api/v1/work/events");
  expect(new Headers(options.headers).get("Authorization")).toBe("Bearer test-token");
  expect(options.credentials).toBe("same-origin");
});
