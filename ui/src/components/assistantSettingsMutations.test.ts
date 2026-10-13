import { describe, expect, it, vi } from "vitest";
import { saveAssistantProjectScope } from "./assistantSettingsMutations";

describe("assistant project scope save", () => {
  it("saves all destinations with a revision while preserving unrelated fields", async () => {
    const current = {
      revision: 3, allowed_domains: ["example.test"], allowed_cidrs: [],
      allowed_urls: [], allowed_ports: [443], allow_all_targets: false,
      bypass_permissions: false, local_only: true, prohibited_actions: ["delete"],
      max_concurrency: 2, grants: [],
    };
    const observed = { ...current, revision: 4, allow_all_targets: true };
    const request = vi.fn().mockResolvedValueOnce(current).mockResolvedValueOnce(observed).mockResolvedValueOnce(observed);
    expect(await saveAssistantProjectScope({ request } as never, "project", { mode: "all_destinations" }))
      .toEqual(observed);
    const [, init] = request.mock.calls[1];
    const body = JSON.parse(init.body);
    expect(body).toMatchObject({
      allowed_domains: ["example.test"], allowed_ports: [443],
      prohibited_actions: ["delete"], max_concurrency: 2,
      allow_all_targets: true, bypass_permissions: false,
      local_only: true, expected_revision: 3,
    });
  });

  it("reports a failed readback instead of claiming a scope change", async () => {
    const current = { revision: 1, allow_all_targets: false, bypass_permissions: false };
    const request = vi.fn().mockResolvedValueOnce(current).mockResolvedValueOnce(current).mockResolvedValueOnce(current);
    await expect(saveAssistantProjectScope({ request } as never, "project", { mode: "all_destinations" }))
      .rejects.toThrow("did not retain");
  });
});
