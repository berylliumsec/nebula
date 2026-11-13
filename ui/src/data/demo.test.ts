import { describe, expect, it, vi } from "vitest";
import { previewApproval, previewEvents } from "./demo";

describe("generic browser approval examples", () => {
  it("previews navigation with the retained browser tool contract", () => {
    expect(previewApproval.toolName).toBe("browser.navigate");
    expect(previewApproval.risk).toBe("active");
    expect(previewApproval.arguments).toEqual({
      tab_id: "preview-tab", url: previewApproval.target,
    });
    expect(previewEvents[0].summary).toBe("Browser navigation is waiting for approval");
  });

  it("uses the current wire risk and clears the illustrated approval after a decision", async () => {
    // Keep the recording fixture out of the production TypeScript graph.
    const { installMissionAdapter } = await vi.importActual<{
      installMissionAdapter: (page: unknown, projectId: string) => Promise<void>;
    }>("../../tests/usage/usage.adapters");
    type MockRoute = {
      request: () => { url: () => string; method: () => string };
      fulfill: ReturnType<typeof vi.fn>;
      fallback: ReturnType<typeof vi.fn>;
    };
    let handler!: (route: MockRoute) => Promise<void>;
    const page = {
      addInitScript: vi.fn().mockResolvedValue(undefined),
      route: vi.fn(async (_pattern: string, callback: typeof handler) => { handler = callback; }),
    };
    await installMissionAdapter(page, "project-example");
    async function request(path: string, method = "GET") {
      const fulfill = vi.fn();
      await handler({
        request: () => ({ url: () => `http://core.test/api/v1/${path}`, method: () => method }),
        fulfill, fallback: vi.fn(),
      });
      return JSON.parse(fulfill.mock.calls[0][0].body);
    }
    const [approval] = await request("approvals");
    expect(approval.risk_class).toBe("network_action");
    expect(approval.exact_request).toEqual({
      tool_name: "browser.navigate",
      arguments: { tab_id: "usage-tab", url: "https://api.northstar.test/" },
      argument_editing: false,
    });
    expect(approval.target).toBe(approval.exact_request.arguments.url);
    expect(await request("approvals/usage-approval/decision", "POST")).toMatchObject({
      status: "approved", revision: 2,
    });
    expect(await request("approvals")).toEqual([]);
  });
});
