import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

describe("OpenRouter account credits API", () => {
  it("keeps the management credential separate when creating a provider", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "openrouter-provider",
          revision: 1,
          name: "OpenRouter",
          provider_type: "openrouter",
          secret_ref: "env:INFERENCE_KEY",
          openrouter_management_secret_ref: "env:MANAGEMENT_KEY",
          model_allowlist: [],
        }),
        { status: 201 },
      ),
    );
    const client = new ApiClient({ baseUrl: "https://nebula.test", fetch: fetchMock });
    const provider = await client.createProvider({
      name: "OpenRouter",
      providerType: "openrouter",
      local: false,
      credentialEnv: "INFERENCE_KEY",
      openrouterManagementSecretRef: "env:MANAGEMENT_KEY",
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.secret_ref).toBe("env:INFERENCE_KEY");
    expect(body.openrouter_management_secret_ref).toBe("env:MANAGEMENT_KEY");
    expect(provider.openrouterManagementSecretRef).toBe("env:MANAGEMENT_KEY");
  });

  it("maps an observed account balance without using a key limit", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          provider_id: "openrouter-provider",
          status: "observed",
          checked_at: "2026-10-15T10:00:00Z",
          observed_at: "2026-10-15T10:00:01Z",
          total_credits_usd: 100.5,
          total_usage_usd: 80.5,
          remaining_usd: 20,
          reason_code: null,
        }),
        { status: 200 },
      ),
    );
    const client = new ApiClient({ baseUrl: "https://nebula.test", fetch: fetchMock });
    const snapshot = await client.refreshOpenRouterCredits("openrouter-provider");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "https://nebula.test/api/v1/providers/openrouter-provider/openrouter/credits/refresh",
    );
    expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
    expect(snapshot).toMatchObject({
      status: "observed",
      totalCreditsUsd: 100.5,
      totalUsageUsd: 80.5,
      remainingUsd: 20,
    });
  });
});
