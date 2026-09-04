import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TopBar } from "./TopBar";

const workspace = vi.hoisted(() => ({
  api: { engagementContainerTerminalPublicIp: vi.fn() },
  coreError: undefined,
  engagement: { id: "project-1" },
  reconnect: vi.fn(),
  workspaceState: "ready",
}));

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));

describe("TopBar public IP", () => {
  beforeEach(() => {
    workspace.api.engagementContainerTerminalPublicIp.mockReset();
    workspace.api.engagementContainerTerminalPublicIp.mockResolvedValue({
      address: "203.0.113.42",
      observedAt: "2026-09-04T10:40:00Z",
      stale: false,
    });
  });

  it("shows the active terminal container address beside Ready", async () => {
    render(<MemoryRouter><TopBar
      activityOpen={false}
      approvalsCount={0}
      onToggleActivity={vi.fn()}
      onToggleSidebar={vi.fn()}
      onOpenPalette={vi.fn()}
      setToolbarHost={vi.fn()}
      sidebarCollapsed={false}
    /></MemoryRouter>);

    const ready = screen.getByRole("button", { name: "Nebula Core ready" });
    const ip = await screen.findByRole("button", { name: /Terminal container public IP 203\.0\.113\.42/ });
    expect(ready.nextElementSibling).toBe(ip);
    expect(ip).toHaveTextContent("IP203.0.113.42");
  });
});
