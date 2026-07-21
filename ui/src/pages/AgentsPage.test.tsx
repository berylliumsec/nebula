import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DialogProvider } from "../components/DialogSystem";
import { AgentsPage } from "./AgentsPage";

const steerRun = vi.fn().mockResolvedValue(undefined);
const selectMission = vi.fn();
const workspace = {
  api: { steerRun, discussRun: vi.fn() },
  approvals: [],
  coreState: "offline" as const,
  deleteMission: vi.fn(),
  engagement: { id: "engagement-1" },
  events: [{
    id: "event-command",
    sequence: 18,
    kind: "task.completed",
    actor: "Nebula Core",
    occurredAt: "2026-07-14T12:00:00Z",
    summary: "**Summary**\n\n- Scan prepared\n\n```bash\nnmap -sV 192.168.1.1\n```",
    payload: {},
  }],
  previewMode: false,
  providers: [],
  reverifyProvider: vi.fn(),
  startMission: vi.fn(),
  stopMission: vi.fn(),
  selectMission,
  runs: [] as Array<Record<string, unknown>>,
  run: {
    id: "run-1",
    backend: "harness" as const,
    harnessSessionId: "session-1",
    title: "Network review",
    status: "running" as const,
    completedTasks: 1,
    totalTasks: 2,
  },
};

vi.mock("../state/ChromeContext", () => ({
  useChrome: () => ({ setActivityOpen: vi.fn() }),
}));

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => workspace,
}));

describe("mission activity", () => {
  it("formats mission Markdown and copies fenced commands exactly", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<DialogProvider><AgentsPage embedded /></DialogProvider>);

    const activity = screen.getByRole("heading", { name: "Activity" }).closest("section");
    expect(activity).not.toBeNull();
    expect(within(activity!).getByText("Summary").tagName).toBe("STRONG");
    expect(within(activity!).getByText("Scan prepared").closest("li")?.parentElement?.tagName).toBe("UL");
    expect(within(activity!).queryByText(/\*\*Summary\*\*/)).toBeNull();

    await user.click(within(activity!).getByRole("button", { name: "Copy exact code" }));
    expect(writeText).toHaveBeenCalledWith("nmap -sV 192.168.1.1\n");
    expect(within(activity!).queryByRole("button", { name: /Review and run/ })).toBeNull();
  });

  it("steers the active harness turn", async () => {
    const user = userEvent.setup();
    steerRun.mockClear();
    render(<DialogProvider><AgentsPage embedded /></DialogProvider>);

    await user.type(screen.getByLabelText("Steer active harness turn"), "Prioritize the login flow");
    await user.click(screen.getByRole("button", { name: "Steer" }));

    expect(steerRun).toHaveBeenCalledWith("run-1", "Prioritize the login flow");
  });

  it("keeps mission controls visible and technical activity collapsed", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><AgentsPage embedded /></DialogProvider>);

    expect(screen.getByRole("region", { name: "Mission controls" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Stop mission" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Delete mission" })).toBeDisabled();
    expect(screen.getByText("Specialists are actively working through the plan.")).toBeVisible();
    expect(screen.getByLabelText("50% of recorded mission tasks complete")).toBeVisible();
    expect(screen.getByText(/Full loaded mission timeline/)).not.toBeVisible();
    await user.click(screen.getByText(/expand for technical timeline/i));
    expect(screen.getByText(/Full loaded mission timeline/)).toBeVisible();
  });

  it("allows an individual mission to be selected", async () => {
    const user = userEvent.setup();
    selectMission.mockClear();
    workspace.runs = [{ ...workspace.run, updatedAt: "2026-07-14T12:00:00Z" }];
    render(<DialogProvider><AgentsPage embedded /></DialogProvider>);

    await user.click(screen.getByRole("button", { name: /Network review/ }));
    expect(selectMission).toHaveBeenCalledWith("run-1");
    workspace.runs = [];
  });

  it("keeps a hundred-mission history searchable and progressively rendered", async () => {
    const user = userEvent.setup();
    workspace.runs = Array.from({ length: 100 }, (_, index) => ({
      ...workspace.run,
      id: `run-${index + 1}`,
      title: `Mission ${index + 1}`,
      status: index % 2 ? "complete" : "failed",
      updatedAt: new Date(Date.UTC(2026, 6, 18, 12, index)).toISOString(),
    }));
    render(<DialogProvider><AgentsPage embedded /></DialogProvider>);

    const history = screen.getByRole("navigation", { name: "Mission history" });
    expect(within(history).getAllByRole("button")).toHaveLength(12);
    expect(screen.getByText("Showing 12 of 100")).toBeVisible();
    await user.type(screen.getByRole("searchbox", { name: "Search missions" }), "Mission 99");
    expect(within(history).getAllByRole("button")).toHaveLength(1);
    expect(within(history).getByRole("button", { name: /Mission 99/ })).toBeVisible();
    workspace.runs = [];
  });

  it("links a failed mission and its final result to Diagnostics", () => {
    const previousStatus = workspace.run.status;
    const previousEvents = workspace.events;
    (workspace.run as { status: string }).status = "failed";
    workspace.events = [{
      id: "event-failed",
      sequence: 19,
      kind: "run.failed",
      actor: "Nebula Core",
      occurredAt: "2026-07-14T12:05:00Z",
      summary: "Mission failed during provider execution.",
      payload: {},
    }];
    render(<DialogProvider><AgentsPage embedded /></DialogProvider>);

    const links = screen.getAllByRole("link", { name: "View diagnostics" });
    expect(links).toHaveLength(2);
    links.forEach((link) => expect(link).toHaveAttribute("href", "/settings#diagnostics-settings"));
    (workspace.run as { status: string }).status = previousStatus;
    workspace.events = previousEvents;
  });
});
