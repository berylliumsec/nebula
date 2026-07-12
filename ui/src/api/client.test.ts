import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "./client";

describe("ApiClient", () => {
  it("pins requests to /api/v1 and authenticates with the configured token", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "ready" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      token: "one-time-token",
      fetch: fetchMock,
    });

    await client.health();

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/api/v1/health");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer one-time-token");
  });

  it("preserves structured API failures and request IDs", async () => {
    const client = new ApiClient({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ message: "Approval expired" }), {
          status: 409,
          headers: { "x-request-id": "request-42" },
        }),
      ),
    });

    const error = await client.decideApproval("approval-1", { decision: "approve" }).catch((value) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, requestId: "request-42", message: "Approval expired" });
  });

  it("maps snake_case Core arrays into engagement and run summaries", async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        id: "engagement-1",
        name: "ACME External",
        client_name: "ACME",
        status: "active",
        created_at: "2026-07-12T10:00:00Z",
        updated_at: "2026-07-12T11:00:00Z",
        revision: 1,
        metadata: { scope_asset_count: 12 },
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        id: "run-1",
        engagement_id: "engagement-1",
        objective: "Validate external services",
        status: "running",
        created_at: "2026-07-12T10:00:00Z",
        updated_at: "2026-07-12T11:30:00Z",
        started_at: "2026-07-12T10:05:00Z",
        completed_at: null,
        revision: 1,
        metadata: { completed_tasks: 2, total_tasks: 5, spent_usd: 1.25 },
      }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const engagements = await client.listEngagements();
    const runs = await client.listRuns("engagement-1");

    expect(engagements).toEqual({
      items: [expect.objectContaining({
        id: "engagement-1",
        clientName: "ACME",
        updatedAt: "2026-07-12T11:00:00Z",
        scopeAssetCount: 12,
      })],
      total: 1,
    });
    expect(runs.items[0]).toMatchObject({
      engagementId: "engagement-1",
      title: "Validate external services",
      completedTasks: 2,
      totalTasks: 5,
      spentUsd: 1.25,
    });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8765/api/v1/runs?engagement_id=engagement-1",
    );
  });

  it("loads only pending approvals and sends edited_arguments on decisions", async () => {
    const approval = {
      id: "approval-1",
      engagement_id: "engagement-1",
      run_id: "run-1",
      status: "pending",
      risk_class: "active_scan",
      exact_request: { tool_name: "scan.tcp", arguments: { ports: [80] } },
      target: "192.0.2.8",
      expected_effects: ["Probe one in-scope target"],
      policy_rationale: "An active scan needs operator approval",
      requested_by: "network-specialist",
      requested_at: "2026-07-12T11:00:00Z",
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([
        approval,
        { ...approval, id: "approval-2", status: "rejected" },
      ]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ...approval,
        status: "edited",
        exact_request: { ...approval.exact_request, arguments: { ports: [443] } },
      }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const pending = await client.listApprovals("engagement-1");
    const decided = await client.decideApproval("approval-1", {
      decision: "approve",
      reason: "HTTPS only",
      editedArguments: { ports: [443] },
    });

    expect(pending.items).toEqual([expect.objectContaining({
      id: "approval-1",
      risk: "active",
      toolName: "scan.tcp",
      expectedEffects: "Probe one in-scope target",
    })]);
    expect(decided).toMatchObject({ status: "approved", arguments: { ports: [443] } });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/approvals?engagement_id=engagement-1",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
      decision: "approve",
      reason: "HTTPS only",
      edited_arguments: { ports: [443] },
    });
  });

  it("loads global provider profiles without an engagement filter", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify([{
      id: "provider-vllm",
      name: "Lab vLLM",
      provider_type: "vllm",
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      capabilities: { streaming: true, tool_calling: true, vision: false },
      privacy: { local_only: true, residency: [] },
      metadata: {},
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    }]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const providers = await client.listProviders();

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8765/api/v1/providers");
    expect(providers.items[0]).toMatchObject({
      name: "Lab vLLM",
      kind: "local",
      modelCount: 1,
      privacy: "local_only",
      capabilities: ["streaming", "tool calling"],
    });
  });

  it("refreshes vLLM health and maps dynamically served models", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
      provider_id: "provider-vllm",
      healthy: true,
      models: ["security-model", "vision-model"],
      detail: null,
    }), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const health = await client.refreshProviderHealth("provider-vllm");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8765/api/v1/providers/provider-vllm/health",
    );
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(health).toEqual({
      providerId: "provider-vllm",
      healthy: true,
      models: ["security-model", "vision-model"],
      detail: undefined,
    });
  });

  it("discovers the provider catalog and creates a local vLLM profile", async () => {
    const provider = {
      id: "provider-vllm",
      name: "Local vLLM",
      provider_type: "vllm",
      endpoint: "http://127.0.0.1:8000/v1",
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      capabilities: { streaming: true },
      privacy: { local_only: true, residency: [] },
      metadata: { default_model: "security-model" },
      created_at: "2026-07-12T11:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify([{
        flavor: "vllm",
        adapter: "openai_compatible",
        display_name: "vLLM",
        local: true,
        default_base_url: "http://127.0.0.1:8000/v1",
        support_tier: "compatible",
      }]), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(provider), { status: 201 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const catalog = await client.listProviderCatalog();
    const created = await client.createProvider({
      name: "Local vLLM",
      providerType: "vllm",
      endpoint: catalog[0].defaultBaseUrl,
      local: true,
      defaultModel: "security-model",
    });

    expect(catalog[0]).toMatchObject({
      flavor: "vllm",
      displayName: "vLLM",
      local: true,
      defaultBaseUrl: "http://127.0.0.1:8000/v1",
    });
    expect(fetchMock.mock.calls[1][0]).toBe("http://127.0.0.1:8765/api/v1/providers");
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({
      provider_type: "vllm",
      endpoint: "http://127.0.0.1:8000/v1",
      is_local: true,
      model_allowlist: ["security-model"],
      privacy: { local_only: true },
    });
    expect(created).toMatchObject({ name: "Local vLLM", kind: "local" });
  });
});
