import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { SecurityDashboard } from "./SecurityDashboard";

function dashboardApi() {
  const api = {
    getSecurityBrowserWorkspace: vi.fn().mockResolvedValue({
      sessions: [{ id: "headless", runtimeKind: "agent_security", status: "active" },
        { id: "visible", runtimeKind: "electron_visible", status: "active" }],
      traffic: [
        { id: "captured", sessionId: "headless", method: "GET", url: "https://example.test/", statusCode: 200 },
        { id: "normal", sessionId: "visible", method: "GET", url: "https://example.org/", statusCode: 200 },
      ], frames: [],
    }),
    getSecurityBrowserResearch: vi.fn().mockResolvedValue({
      intercepts: [], attacks: [], repeaterResults: [], repeaterTabs: [],
    }),
    getSecurityBrowserAutomation: vi.fn().mockResolvedValue({ leases: [] }),
    stopAgentSecurity: vi.fn().mockResolvedValue({ stopped_sessions: 1 }),
  };
  return api;
}

describe("SecurityDashboard", () => {
  it("counts only headless traffic and lets the operator stop a session without a desktop lease", async () => {
    const api = dashboardApi();
    const user = userEvent.setup();
    render(<SecurityDashboard api={api as unknown as ApiClient} projectId="project" />);
    expect(await screen.findByText("1 HTTP exchanges")).toBeVisible();
    expect(screen.getByRole("button", { name: "Emergency stop" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Emergency stop" }));
    await waitFor(() => expect(api.stopAgentSecurity).toHaveBeenCalledWith("project"));
    expect(screen.queryByText(/example\.org/)).toBeNull();
  });

  it("keeps a paused intercept as an explicit operator decision", async () => {
    const api = dashboardApi();
    api.getSecurityBrowserResearch.mockResolvedValue({
      intercepts: [{ id: "pause-1", sessionId: "headless", state: "paused", phase: "request", method: "GET", url: "https://example.test/" }],
      attacks: [], repeaterResults: [], repeaterTabs: [],
    });
    const decide = vi.fn().mockResolvedValue({});
    const fullApi = { ...api, decideSecurityBrowserIntercept: decide };
    const user = userEvent.setup();
    render(<SecurityDashboard api={fullApi as unknown as ApiClient} projectId="project" />);
    expect(await screen.findByText("Request paused")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Drop" }));
    await waitFor(() => expect(decide).toHaveBeenCalledWith(expect.objectContaining({ id: "pause-1" }), "drop"));
  });
});
