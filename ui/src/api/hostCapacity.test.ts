import {describe, expect, it, vi} from "vitest";
import {ApiClient} from "./client";

describe("saved host admission state", () => {
  it.each(["Waiting for host CPU capacity", "Waiting for host memory capacity", null])("restores %s from Core", async (reason) => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "turn", session_id: "chat", revision: 2, status: "queued",
      admission_reason: reason, queued_at: "2026-11-03T12:00:00Z", capacity_lane: "direct",
    }), {status: 200, headers: {"content-type": "application/json"}}));
    const api = new ApiClient({baseUrl: "http://core.test/api/v1", fetch});
    const turn = await api.getPendingChatTurn("chat");
    expect(turn?.status).toBe("queued");
    expect(turn?.admissionReason).toBe(reason ?? undefined);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
