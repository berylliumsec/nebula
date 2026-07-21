import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { DialogProvider } from "../components/DialogSystem";
import { OverviewPage } from "./OverviewPage";
import "../styles.css";
import "../refinement.css";

vi.mock("../state/ChromeContext", () => ({
  useChrome: () => ({ setActivityOpen: vi.fn(), toolbarHost: null }),
}));

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({
    api: undefined,
    approvals: [],
    assets: [],
    coreState: "offline",
    engagement: { id: "project-1", name: "Markdown project" },
    events: [{
      id: "event-markdown",
      sequence: 7,
      kind: "task.completed",
      actor: "Nebula Core",
      occurredAt: "2026-07-14T12:00:00Z",
      summary: "**Analyst-Facing Result**\n\n- Scan prepared\n- Target: `192.168.1.1`",
      payload: {},
    }],
    findings: [],
    health: { runner: "unavailable" },
    previewMode: false,
    providers: [],
    reverifyProvider: vi.fn(),
    run: {
      id: "run-1",
      title: "Network review",
      status: "complete",
      completedTasks: 1,
      totalTasks: 1,
    },
    startMission: vi.fn(),
    stopMission: vi.fn(),
  }),
}));

describe("project overview mission activity", () => {
  it("renders event summaries as Markdown", () => {
    const { container } = render(
      <MemoryRouter>
        <DialogProvider>
          <OverviewPage />
        </DialogProvider>
      </MemoryRouter>,
    );

    const activity = container.querySelector<HTMLElement>(".mission-steps");
    expect(activity).not.toBeNull();
    expect(within(activity!).getByText("Analyst-Facing Result").tagName).toBe("STRONG");
    const eventRow = activity!.querySelector(":scope > li");
    const markdownListItem = within(activity!).getByText("Scan prepared").closest("li");
    expect(markdownListItem?.parentElement?.tagName).toBe("UL");
    expect(getComputedStyle(eventRow!).display).toBe("grid");
    expect(getComputedStyle(markdownListItem!).display).toBe("list-item");
    expect(within(activity!).getByText("192.168.1.1").tagName).toBe("CODE");
    expect(screen.queryByText(/\*\*Analyst-Facing Result\*\*/)).toBeNull();
  });
});
