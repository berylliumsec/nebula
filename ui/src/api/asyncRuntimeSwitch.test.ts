import {describe, expect, it, vi} from "vitest";
import {ApiClient} from "./client";

describe("pending runtime selection", () => {
  it("keeps the active binding separate from the saved next runtime", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "chat", engagement_id: "project", title: "Same goal", backend: "harness", model: "old-model",
      harness_profile_id: "old-harness", revision: 3, metadata: {pending_runtime_switch: {
        id: "selection", status: "waiting_for_idle", reason: "Waiting for the current response.",
        binding: {backend: "provider", provider_profile_id: "new-provider", model: "new-model"},
      }},
    }), {status: 200}));
    const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
    const saved = await client.getChatSession("chat");
    expect(saved.backend).toBe("harness");
    expect(saved.model).toBe("old-model");
    expect(saved.pendingRuntimeSwitch).toMatchObject({id: "selection", backend: "provider", profileId: "new-provider", model: "new-model", status: "waiting_for_idle"});
  });

  it("preserves the action needed when a delayed selection needs review", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      id: "chat", engagement_id: "project", title: "Same goal", backend: "harness", model: "old-model",
      revision: 4, metadata: {pending_runtime_switch: {id: "selection", status: "needs_review", reason: "Review context compaction.",
        binding: {backend: "provider", provider_profile_id: "new-provider", model: "new-model"}}},
    }), {status: 200}));
    const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
    expect((await client.getChatSession("chat")).pendingRuntimeSwitch).toMatchObject({status: "needs_review", reason: "Review context compaction."});
  });

  it("reports deferred activation without reporting the target as active", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      session_id: "chat", session_revision: 2, current_model: "old-model", target_provider_id: "provider", target_model: "new-model",
      compatible: true, activation: "after_active_work", activation_reason: "Waiting for saved follow-ups.",
    }), {status: 200}));
    const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
    expect(await client.preflightChatRuntimeSwitch("chat", {providerId: "provider", model: "new-model", toolsEnabled: false, expectedSessionRevision: 2})).toMatchObject({compatible: true, activation: "after_active_work", activationReason: "Waiting for saved follow-ups."});
  });
});
