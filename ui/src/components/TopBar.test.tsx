import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TopBar } from "./TopBar";

const workspace = vi.hoisted(() => ({
  api: { engagementContainerTerminalPublicIp: vi.fn() },
  coreError: undefined,
  engagement: { id: "project-1" },
  reconnect: vi.fn(),
  workspaceState: "ready",
}));

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
const copy = vi.hoisted(() => vi.fn());
vi.mock("./selection", () => ({copySelectionText: copy}));
afterEach(cleanup);

describe("TopBar page title", () => {
  beforeEach(() => workspace.api.engagementContainerTerminalPublicIp.mockResolvedValue(undefined));

  it.each([
    ["/projects/project-1/workbench", "Workbench"],
    ["/projects/project-1/findings", "Findings"],
    ["/projects/project-1/findings/finding-7", "Findings"],
    ["/projects/project-1/reports/report-2", "Reports"],
    ["/projects/project-1", "Project"],
    ["/projects/project-1/assets/asset-3", "Project"],
    ["/projects/project-1/evidence", "Project"],
    ["/projects/project-1/sources", "Project"],
    ["/knowledge", "Project"],
    ["/library/doc-1", "Library"],
    ["/settings", "Settings"],
    ["/", "Workbench"],
  ])("names the page that owns %s", (route, label) => {
    const { container } = render(<MemoryRouter initialEntries={[route]}><TopBar activityOpen={false} approvalsCount={0} onToggleActivity={vi.fn()} onToggleSidebar={vi.fn()} onOpenPalette={vi.fn()} setToolbarHost={vi.fn()} setTrailingToolbarHost={vi.fn()} sidebarCollapsed={false} /></MemoryRouter>);
    expect(container.querySelector(".top-bar-title strong")).toHaveTextContent(label);
    expect(screen.getByRole("group", { name: `${label} actions` })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: `${label} primary action` })).toBeInTheDocument();
  });
});

describe("TopBar public IP", () => {
  beforeEach(() => {
    copy.mockReset();
    copy.mockResolvedValue(undefined);
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

  it.each([false, true])("keeps the complete address available in a focused disclosure (copy failure %s)", async (failure) => {
    if (failure) copy.mockRejectedValue(new Error("Clipboard unavailable"));
    render(<MemoryRouter><TopBar activityOpen={false} approvalsCount={0} onToggleActivity={vi.fn()} onToggleSidebar={vi.fn()} onOpenPalette={vi.fn()} setToolbarHost={vi.fn()} sidebarCollapsed /></MemoryRouter>);
    const trigger = await screen.findByRole("button", {name: /Terminal container public IP 203\.0\.113\.42/});
    await userEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", {name: "Terminal network address"});
    expect(within(dialog).getByRole("textbox", {name: "Public IP address"})).toHaveValue("203.0.113.42");
    await userEvent.click(within(dialog).getByRole("button", {name: "Copy address"}));
    expect(copy).toHaveBeenCalledWith("203.0.113.42");
    if (failure) expect(await within(dialog).findByRole("alert")).toHaveTextContent("Clipboard unavailable");
    else expect(await within(dialog).findByRole("status")).toHaveTextContent("Address copied");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
