import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DialogProvider } from "./DialogSystem";
import { SideNav } from "./SideNav";

const { project, workspace } = vi.hoisted(() => {
  const project = { id: "project-1", name: "Scratch Project", status: "active" };
  return {
    project,
    workspace: {
      api: {},
      coreState: "online",
      createEngagement: vi.fn(),
      activeOperator: undefined,
      engagement: project as typeof project | undefined,
      engagements: [project],
      archivedEngagements: [],
      setEngagementArchived: vi.fn(),
      deleteArchivedEngagement: vi.fn(),
    },
  };
});

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
afterEach(() => {
  cleanup();
  workspace.engagement = project;
  workspace.engagements = [project];
});

function CurrentPath() {
  return <output aria-label="Current path">{useLocation().pathname}</output>;
}

function renderSideNav(route: string, variant: "standard" | "zero" = "standard") {
  render(<MemoryRouter initialEntries={[route]}><DialogProvider>
    <SideNav collapsed={false} open={false} setOpen={vi.fn()} onNavigate={vi.fn()} variant={variant} />
    <CurrentPath />
  </DialogProvider></MemoryRouter>);
  return screen.getByRole("complementary", { name: "Primary navigation" });
}

function expectOnlyCurrent(navigation: HTMLElement, label: string) {
  const links = within(navigation).getAllByRole("link");
  const current = links.filter((link) => link.getAttribute("aria-current") === "page");
  const highlighted = links.filter((link) => link.classList.contains("active"));
  expect(current.map((link) => link.getAttribute("aria-label"))).toEqual([label]);
  expect(highlighted).toEqual(current);
}

describe("SideNav current page", () => {
  it.each([
    ["/projects/project-1/workbench", "Workbench"],
    ["/projects/project-1/findings", "Findings"],
    ["/projects/project-1/findings/finding-7", "Findings"],
    ["/projects/project-1/reports", "Reports"],
    ["/projects/project-1/reports/report-2", "Reports"],
    ["/projects/project-1", "Project"],
    ["/projects/project-1/assets", "Project"],
    ["/projects/project-1/assets/asset-3", "Project"],
    ["/projects/project-1/evidence", "Project"],
    ["/projects/project-1/evidence/evidence-4", "Project"],
    ["/projects/project-1/sources", "Project"],
    ["/projects/project-1/sources/source-5", "Project"],
    ["/library", "Library"],
    ["/library/doc-1", "Library"],
    ["/settings", "Settings"],
  ])("marks the section that owns %s", (route, label) => {
    for (const variant of ["standard", "zero"] as const) {
      expectOnlyCurrent(renderSideNav(route, variant), label);
      cleanup();
    }
  });

  it("keeps section links pointed at the section root from a detail page", async () => {
    const navigation = renderSideNav("/projects/project-1/findings/finding-7");
    const findings = within(navigation).getByRole("link", { name: "Findings" });
    expect(findings).toHaveAttribute("href", "/projects/project-1/findings");

    await userEvent.click(within(navigation).getByRole("link", { name: "Project" }));
    expect(screen.getByRole("status", { name: "Current path" })).toHaveTextContent(/^\/projects\/project-1$/);
    expectOnlyCurrent(navigation, "Project");

    await userEvent.click(findings);
    expect(screen.getByRole("status", { name: "Current path" })).toHaveTextContent(/^\/projects\/project-1\/findings$/);
    expectOnlyCurrent(navigation, "Findings");
  });

  it.each(["/assets", "/evidence", "/knowledge"])("marks Project on legacy %s when no project is loaded", (route) => {
    workspace.engagement = undefined;
    workspace.engagements = [];
    const navigation = renderSideNav(route);
    expectOnlyCurrent(navigation, "Project");
    expect(within(navigation).getByRole("link", { name: "Project" })).toHaveAttribute("href", "/project");
  });
});

function SwitcherHarness() {
  const [open, setOpen] = useState(false);
  return <SideNav collapsed={false} open={open} setOpen={setOpen} onNavigate={vi.fn()} />;
}

describe("SideNav project switcher", () => {
  it("moves focus into the switcher and hands it back to the trigger on Escape", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench"]}><DialogProvider><SwitcherHarness /></DialogProvider></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: "Switch project" });

    await user.click(trigger);
    const switcher = screen.getByRole("dialog", { name: "Project switcher" });
    await waitFor(() => expect(switcher.contains(document.activeElement)).toBe(true));

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Project switcher" })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });
});

describe("SideNav new project form", () => {
  it("keeps the form open until creation settles and shows a late failure", async () => {
    let reject!: (error: Error) => void;
    workspace.createEngagement.mockImplementationOnce(() => new Promise((_resolve, rejectCreate) => { reject = rejectCreate; }));
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench"]}><DialogProvider><SwitcherHarness /></DialogProvider></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Switch project" }));
    const switcher = screen.getByRole("dialog", { name: "Project switcher" });
    await user.click(within(switcher).getByRole("button", { name: "New project" }));
    await user.type(within(switcher).getByLabelText("Name"), "Quarterly review");
    await user.click(within(switcher).getByRole("button", { name: "Create" }));

    expect(within(switcher).getByRole("button", { name: "Creating…" })).toBeDisabled();
    expect(within(switcher).getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(within(switcher).getByRole("button", { name: "Close project switcher" })).toBeDisabled();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Switch project" }));
    expect(screen.getByRole("dialog", { name: "Project switcher" })).toBe(switcher);

    reject(new Error("Core rejected the project."));
    expect(await within(switcher).findByRole("alert")).toHaveTextContent("Core rejected the project.");
    expect(within(switcher).getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(workspace.createEngagement).toHaveBeenCalledTimes(1);
  });
});
