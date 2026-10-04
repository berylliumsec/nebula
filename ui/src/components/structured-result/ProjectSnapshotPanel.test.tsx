import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectSnapshotPanel, type ProjectSnapshotPanelProps } from "./ProjectSnapshotPanel";
import { clampRect, MIN_HEIGHT, MIN_WIDTH, readLauncher, readRect, writeLauncher } from "./projectSnapshotGeometry";

const workspace = vi.hoisted(() => ({ current: {
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

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); setNarrow(false); });

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
  it("shows the same project totals as the dashboard, without published results", () => {
    panel();
    const dialog = screen.getByRole("dialog", { name: "Project Snapshot" });
    expect(dialog).toHaveFocus();
    expect(dialog).toHaveTextContent("Network review");
    expect(dialog).toHaveTextContent("2 of 4 tasks");
    expect(dialog).toHaveTextContent("1 approval waiting for review");
    expect(screen.getByRole("region", { name: "Project summary" })).toHaveTextContent("$0.0013");
    expect(screen.getByRole("link", { name: "Open dashboard" })).toHaveAttribute("href", "/projects/project-1");
    expect(dialog).not.toHaveTextContent("Nothing published yet");
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
