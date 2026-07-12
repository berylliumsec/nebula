import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";

function renderApp(route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <ThemeProvider>
        <WorkspaceProvider>
          <App />
        </WorkspaceProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

describe("Nebula workspace", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Core offline")));
  });

  afterEach(() => vi.unstubAllGlobals());

  it("exposes every primary workspace destination", async () => {
    renderApp();
    for (const label of ["Overview", "Sessions", "Agents", "Assets", "Findings", "Evidence", "Knowledge", "Reports", "Settings"]) {
      expect(screen.getByRole("link", { name: label })).toBeVisible();
    }
    expect(await screen.findByRole("heading", { name: "Good afternoon, Jordan" })).toBeVisible();
  });

  it("navigates with the keyboard command palette", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.keyboard("{Control>}k{/Control}");
    const search = screen.getByRole("textbox", { name: "Search commands" });
    await user.type(search, "provider");
    await user.click(screen.getByRole("option", { name: /Go to Settings/ }));
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
  });

  it("applies the accessible high-contrast preference", async () => {
    const user = userEvent.setup();
    renderApp("/settings");
    await screen.findByRole("heading", { name: "Settings" });
    await user.click(screen.getByRole("button", { name: /High contrast/ }));
    expect(document.documentElement).toHaveAttribute("data-theme", "high-contrast");
    expect(localStorage.getItem("nebula.theme")).toBe("high-contrast");
  });

  it("selects the first Core engagement and run, then opens its replay stream", async () => {
    class OnlineWebSocket extends EventTarget {
      static lastUrl = "";

      constructor(url: string | URL) {
        super();
        OnlineWebSocket.lastUrl = String(url);
      }

      close() {}
    }

    const entity = {
      created_at: "2026-07-12T10:00:00Z",
      updated_at: "2026-07-12T11:00:00Z",
      revision: 1,
    };
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) {
        return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable" }), { status: 200 });
      }
      if (url.pathname.endsWith("/engagements")) {
        return new Response(JSON.stringify([{ ...entity, id: "engagement-first", name: "Live engagement", status: "active", metadata: {} }]), { status: 200 });
      }
      if (url.pathname.endsWith("/runs")) {
        return new Response(JSON.stringify([{ ...entity, id: "run-first", engagement_id: "engagement-first", objective: "Live mission", status: "running", metadata: {} }]), { status: 200 });
      }
      if (url.pathname.endsWith("/approvals")) {
        return new Response(JSON.stringify([{ ...entity, id: "approval-first", engagement_id: "engagement-first", run_id: "run-first", status: "pending", risk_class: "active_scan", exact_request: { tool_name: "scan.tcp", arguments: { ports: [443] } }, policy_rationale: "Active scan approval", requested_by: "network-specialist", requested_at: entity.created_at, expected_effects: ["Probe the target"] }]), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", OnlineWebSocket);

    renderApp();

    expect(await screen.findByRole("heading", { name: "Live engagement" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Show activity center" })).toBeVisible();
    expect(OnlineWebSocket.lastUrl).toContain("/api/v1/runs/run-first/events/ws?after=0");
    expect(fetchMock.mock.calls.map(([input]) => String(input))).toEqual(expect.arrayContaining([
      expect.stringContaining("/api/v1/runs?engagement_id=engagement-first"),
      expect.stringContaining("/api/v1/approvals?engagement_id=engagement-first"),
      expect.stringContaining("/api/v1/assets?engagement_id=engagement-first"),
      expect.stringContaining("/api/v1/findings?engagement_id=engagement-first"),
      expect.stringMatching(/\/api\/v1\/providers$/),
    ]));
  });
});
