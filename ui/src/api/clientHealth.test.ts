import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

describe("ApiClient health", () => {
  it.each([
    [{ api_ready: false, api_state: "not_responding" }, true, "not_responding"],
    [{ api_ready: false, api_state: "unavailable" }, false, "unavailable"],
    [{ api_ready: true, api_state: "ready" }, true, "ready"],
    [{ api_ready: false }, false, undefined],
  ])("keeps memory recovery for held admission only: %j", async (wire, apiReady, apiState) => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status: "degraded", version: "3.0.0", mode: "team", ...wire }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const health = await client.health();

    expect(health.apiReady).toBe(apiReady);
    expect(health.apiState).toBe(apiState);
  });
});
