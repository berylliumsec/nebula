import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { currentSnapshotItem, ProjectSnapshotPanel, type ProjectSnapshotPanelProps } from "./ProjectSnapshotPanel";
import { clampRect, MIN_HEIGHT, MIN_WIDTH, readLauncher, readRect, writeLauncher } from "./projectSnapshotGeometry";

const workApi = vi.hoisted(() => ({ request: vi.fn(), watchWorkChanges: vi.fn() }));
const workspace = vi.hoisted(() => ({ current: {
  api: workApi,
  engagement: { id: "project-1", name: "Network review" },
  assets: [{ id: "asset-1" }],
  findings: [{ id: "finding-1", status: "validated", severity: "high" }],
  run: { title: "Review perimeter", status: "running", completedTasks: 2, totalTasks: 4, spentUsd: 0.00125 },
  approvals: [{ id: "approval-1" }],
} }));
vi.mock("../../state/WorkspaceContext", () => ({ useWorkspace: () => workspace.current }));

const viewport = { left: 0, top: 0, width: 1280, height: 800 };

function panel(overrides: Partial<ProjectSnapshotPanelProps> = {}) {
  const props: ProjectSnapshotPanelProps = {
    projectId: "project-1",
    minimized: false,
    onMinimize: vi.fn(),
    onRestore: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<MemoryRouter><ProjectSnapshotPanel {...props} /></MemoryRouter>) };
}

function setNarrow(narrow: boolean) {
  vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
    matches: narrow && query.includes("760px"),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

const workItem = {
  id: "work-1", engagement_id: "project-1", title: "Project task", status: "in_progress", priority: "normal",
  assignee_session_id: "session-1", source_kind: "chat", source_id: "session-1", last_update_at: "2026-10-04T17:11:00Z",
};
const workUpdate = {
  id: "update-1", engagement_id: "project-1", item_id: "work-1", summary: "Stage: review. The current task is complete.",
  next_step: "Review the next task.", blocker: null,
  created_at: "2026-10-04T17:11:00Z", source_session_id: "session-1", source_engagement_id: "project-1",
};

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); setNarrow(false);
  workApi.request.mockImplementation(async (path: string) => path.startsWith("work/updates?") ? [] : path.endsWith("/updates") ? [workUpdate] : [workItem]);
  workApi.watchWorkChanges.mockImplementation((_onChange: unknown, _onReady: unknown, signal: AbortSignal) => new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true })));
});

describe("where the Project Snapshot sits", () => {
  it("never shrinks past its minimum or leaves the screen", () => {
    expect(clampRect({ x: 0, y: 0, width: 10, height: 10 }, viewport)).toEqual({ x: 12, y: 12, width: MIN_WIDTH, height: MIN_HEIGHT });
    expect(clampRect({ x: -500, y: 9000, width: 5000, height: 5000 }, viewport)).toEqual({ x: 12, y: 12, width: 1256, height: 776 });
  });

  it("opens under the toolbar and clear of the composer when there is room", () => {
    const rect = readRect({ left: 0, top: 0, width: 1440, height: 900 }, { below: 140, above: 632 });
    expect(rect.y).toBe(152);
    expect(rect.y + rect.height).toBe(620);
  });

  it("survives unreadable or inaccessible saved geometry", () => {
    localStorage.setItem("nebula.project-snapshot.geometry", "{not json");
    expect(readRect(viewport).width).toBe(480);
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(readLauncher()).toBeUndefined();
    expect(() => writeLauncher({ x: 10, y: 10 })).not.toThrow();
    getItem.mockRestore();
    setItem.mockRestore();
  });
});

describe("the floating Project Snapshot", () => {
  it("leads with Work's saved current progress and keeps dashboard totals below", async () => {
    panel();
    const dialog = screen.getByRole("dialog", { name: "Project Snapshot" });
    expect(dialog).toHaveFocus();
    expect(dialog).toHaveTextContent("Network review");
    const progress = await screen.findByRole("region", { name: "Current progress" });
    expect(progress).toHaveTextContent(workUpdate.summary);
    expect(progress).toHaveTextContent(`Next: ${workUpdate.next_step}`);
    expect(progress).toHaveTextContent("In progress · normal priority");
    expect(progress.getElementsByTagName("time")[0]).toHaveAttribute("dateTime", workUpdate.created_at);
    expect(screen.getByRole("link", { name: "Open work item" })).toHaveAttribute("href", "/projects/project-1/work/work-1");
    expect(screen.getByRole("link", { name: "Open conversation" })).toHaveAttribute("href", "/projects/project-1/workbench?view=chat&session=session-1");
    expect(dialog).toHaveTextContent("2 of 4 tasks");
    expect(dialog).toHaveTextContent("1 approval waiting for review");
    expect(screen.getByRole("region", { name: "Project summary" })).toHaveTextContent("$0.0013");
    expect(screen.getByRole("link", { name: "Open dashboard" })).toHaveAttribute("href", "/projects/project-1");
    expect(dialog).not.toHaveTextContent("Nothing published yet");
  });

  it("refreshes the saved summary when Work changes", async () => {
    panel();
    await screen.findByText(workUpdate.summary);
    workApi.request.mockImplementation(async (path: string) => path.endsWith("/updates")
      ? [{ ...workUpdate, summary: "Proof joins are updated in Core." }]
      : [workItem]);
    const onChange = workApi.watchWorkChanges.mock.calls[0][0] as () => void;
    onChange();
    expect(await screen.findByText("Proof joins are updated in Core.")).toBeVisible();
  });

  it("shows a conversation-linked child project's latest Work check-in", async () => {
    const childItem = { ...workItem, id: "child-work", engagement_id: "child-project", source_kind: "import", source_id: null, assignee_session_id: null };
    const linked = { ...workUpdate, id: "linked", engagement_id: "child-project", item_id: childItem.id };
    const newest = { ...linked, id: "newest", summary: "The latest saved check-in.", source_session_id: null, source_engagement_id: null };
    workApi.request.mockImplementation(async (path: string) => path.startsWith("work/updates?") ? [linked]
      : path.endsWith("/updates") ? [newest]
        : path.endsWith("/work/child-work") ? childItem : []);
    panel({ sessionId: "session-1" });
    const progress = await screen.findByRole("region", { name: "Current progress" });
    expect(progress).toHaveTextContent(newest.summary);
    expect(screen.getByRole("link", { name: "Open work item" })).toHaveAttribute("href", "/projects/child-project/work/child-work");
    expect(screen.getByRole("link", { name: "Open conversation" })).toHaveAttribute("href", "/projects/project-1/workbench?view=chat&session=session-1");
    expect(workApi.request).not.toHaveBeenCalledWith("engagements/project-1/work", expect.anything());
  });

  it("explains an empty or failed Work read and offers retry", async () => {
    workApi.request.mockResolvedValueOnce([]);
    const { unmount } = panel();
    expect(await screen.findByText("No Work check-in has been saved for this project.")).toBeVisible();
    unmount();
    workApi.request.mockRejectedValueOnce(new Error("Core unavailable"));
    panel();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load Work progress.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(workUpdate.summary)).toBeVisible();
  });

  it("minimizes, restores on Escape, and closes on the operator's word", async () => {
    const user = userEvent.setup();
    const { props } = panel();
    await user.keyboard("{Escape}");
    expect(props.onMinimize).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Close Project Snapshot" }));
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("moves and resizes from the keyboard", async () => {
    const user = userEvent.setup();
    panel();
    const dialog = screen.getByRole("dialog", { name: "Project Snapshot" });
    const before = { left: parseFloat(dialog.style.left), width: parseFloat(dialog.style.width) };
    screen.getByRole("button", { name: "Move Project Snapshot" }).focus();
    await user.keyboard("{ArrowLeft}");
    expect(parseFloat(dialog.style.left)).toBe(before.left - 24);
    screen.getByRole("button", { name: "Resize Project Snapshot" }).focus();
    await user.keyboard("{ArrowLeft}");
    expect(parseFloat(dialog.style.width)).toBe(before.width - 24);
  });

  it("is a sheet on a phone", () => {
    setNarrow(true);
    panel();
    const dialog = screen.getByRole("dialog", { name: "Project Snapshot" });
    expect(dialog).toHaveClass("sheet");
    expect(screen.queryByRole("button", { name: "Move Project Snapshot" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resize Project Snapshot" })).not.toBeInTheDocument();
  });
});

it("chooses the current conversation's check-in ahead of another active item", () => {
  const other = { ...workItem, id: "work-2", assignee_session_id: "session-2", source_id: "session-2", last_update_at: "2026-10-04T18:00:00Z" };
  expect(currentSnapshotItem([other, workItem], "session-1")?.id).toBe("work-1");
  expect(currentSnapshotItem([other, workItem])?.id).toBe("work-2");
});

describe("the minimized Project Snapshot", () => {
  it("hands focus to the way back", async () => {
    const user = userEvent.setup();
    const { props } = panel({ minimized: true });
    const show = screen.getByRole("button", { name: "Show Project Snapshot, Project status at a glance" });
    expect(show).toHaveFocus();
    await user.click(show);
    expect(props.onRestore).toHaveBeenCalledOnce();
  });

  it("moves from the keyboard and stays where it was put", async () => {
    const user = userEvent.setup();
    panel({ minimized: true });
    screen.getByRole("button", { name: "Move minimized Project Snapshot" }).focus();
    await user.keyboard("{ArrowUp}");
    await waitFor(() => expect(localStorage.getItem("nebula.project-snapshot.launcher")).not.toBeNull());
  });
});
