import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { ProjectWorkOverview } from "./ProjectWorkOverview";

const { workspace, live } = vi.hoisted(() => {
  const live = { onChange: undefined as undefined | (() => void) };
  const request = vi.fn();
  return { workspace: { api: { request, watchWorkChanges: vi.fn((onChange: () => void, _onReady: () => void, signal: AbortSignal) => {
    live.onChange = onChange;
    return new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
  }) }, coreState: "online" }, live };
});
vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));

const item = (id: string, status: string, updated: string | null) => ({
  id, engagement_id: "project-1", title: id, status, priority: "normal",
  assignee_session_id: null, source_kind: "manual", source_id: null, last_update_at: updated,
});
const update = (summary: string) => ({
  id: "update-1", engagement_id: "project-1", item_id: "active", summary,
  next_step: "Review the copy", blocker: "Waiting for approval", created_at: "2026-10-04T17:11:00Z",
  source_session_id: "chat-1", source_engagement_id: "project-1",
});

beforeEach(() => { vi.clearAllMocks(); live.onChange = undefined; });

it("shows the newest saved active progress and refreshes it when Core signals a check-in", async () => {
  let current = update("The draft is in progress");
  workspace.api.request.mockImplementation(async (path: string) => path.endsWith("/work")
    ? [item("done", "done", "2026-10-04T18:00:00Z"), item("active", "in_progress", "2026-10-04T17:11:00Z"), item("unstarted", "ready", null)]
    : [current]);
  render(<MemoryRouter><ProjectWorkOverview projectId="project-1" /></MemoryRouter>);
  expect(await screen.findByText("The draft is in progress")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open work item" })).toHaveAttribute("href", "/projects/project-1/work/active");
  expect(screen.getByRole("link", { name: "Open conversation" })).toHaveAttribute("href", "/projects/project-1/workbench?view=chat&session=chat-1");
  const other = screen.getByRole("complementary", { name: "Other active work" });
  expect(within(other).getByText("unstarted")).toBeVisible();
  expect(within(other).getByText(/No check-in yet/)).toBeVisible();
  expect(screen.queryByText("done")).not.toBeInTheDocument();
  current = update("The saved draft has advanced");
  await act(async () => { live.onChange?.(); });
  expect(await screen.findByText("The saved draft has advanced")).toBeVisible();
});

it("recovers from a failed read and clears the previous project when switching", async () => {
  let fail = true;
  workspace.api.request.mockImplementation(async (path: string) => {
    if (fail) throw new Error("Temporary read failure");
    if (path.startsWith("engagements/project-2/")) return [item("second", "ready", null)];
    return [];
  });
  const user = userEvent.setup();
  const view = render(<MemoryRouter><ProjectWorkOverview projectId="project-1" /></MemoryRouter>);
  expect(await screen.findByRole("alert")).toHaveTextContent("Temporary read failure");
  fail = false;
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByText("No current progress yet")).toBeVisible();
  view.rerender(<MemoryRouter><ProjectWorkOverview projectId="project-2" /></MemoryRouter>);
  await waitFor(() => expect(workspace.api.request).toHaveBeenCalledWith("engagements/project-2/work", expect.any(Object)));
  expect(await screen.findByText("second")).toBeVisible();
  expect(screen.queryByText("Temporary read failure")).not.toBeInTheDocument();
});
