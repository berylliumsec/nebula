import {expect, it, vi} from "vitest";
import {ApiClient} from "./client";

it("sends browser-selected bytes with content, destination and overwrite preconditions", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
    engagement_id: "project", path: "captures/sample.bin", size: 4,
    sha256: "c".repeat(64), overwritten: true,
  }), {status: 201}));
  const client = new ApiClient({baseUrl: "http://192.0.2.10:8765", token: "synthetic-token", fetch: fetchMock});
  const file = new Blob([new Uint8Array([0, 255, 42, 1])]);
  await client.uploadWorkspaceFile("project", "captures/sample.bin", file, true, undefined,
    "a".repeat(64), "c".repeat(64), "b".repeat(64));
  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toContain("workspace/file?path=captures%2Fsample.bin&overwrite=true");
  expect(init?.body).toBe(file);
  const headers = new Headers(init?.headers);
  expect(headers.get("If-Match")).toBe("a".repeat(64));
  expect(headers.get("X-Content-SHA256")).toBe("c".repeat(64));
  expect(headers.get("X-Parent-Identity")).toBe("b".repeat(64));
  expect(headers.get("Authorization")).toBe("Bearer synthetic-token");
});
