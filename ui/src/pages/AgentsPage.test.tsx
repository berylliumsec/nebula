import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentsPage } from "./AgentsPage";

const steerRun = vi.fn().mockResolvedValue(undefined);

vi.mock("../state/ChromeContext", () => ({
  useChrome: () => ({ setActivityOpen: vi.fn() }),
}));

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({
    api: { steerRun, discussRun: vi.fn() },
    approvals: [],
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
    run: {
      id: "run-1",
      backend: "harness",
      harnessSessionId: "session-1",
      title: "Network review",
      status: "running",
      completedTasks: 1,
      totalTasks: 2,
    },
  }),
}));

describe("mission activity", () => {
  it("formats mission Markdown and copies fenced commands exactly", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<AgentsPage embedded />);

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
    render(<AgentsPage embedded />);

    await user.type(screen.getByLabelText("Steer active harness turn"), "Prioritize the login flow");
    await user.click(screen.getByRole("button", { name: "Steer" }));

    expect(steerRun).toHaveBeenCalledWith("run-1", "Prioritize the login flow");
  });
});
