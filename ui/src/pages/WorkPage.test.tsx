import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkPage } from "./WorkPage";

const { workspace, saved } = vi.hoisted(() => {
  const project = { id: "project-1", name: "Documentation portal", status: "active", workEnabled: false };
  const saved = {
    enabled: false,
    item: { id: "item-1", engagement_id: project.id, title: "Build the search page", description: "Add filters", status: "in_progress", priority: "normal", assignee_session_id: "session-1", source_kind: "chat", source_id: "session-1", created_at: "2026-01-01T12:00:00Z", updated_at: "2026-01-01T12:00:00Z", last_update_at: null } as Record<string, unknown>,
    items: [] as Array<Record<string, unknown>>,
    updates: [] as Array<Record<string, unknown>>,
  };
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.startsWith("work/items?")) {
      const query = new URLSearchParams(path.split("?")[1]);
      const offset = Number(query.get("offset") ?? 0);
      const limit = Number(query.get("limit") ?? 500);
      return saved.items.slice(offset, offset + limit);
    }
    if (path.endsWith("/work/setting") && init?.method === "PATCH") { saved.enabled = !saved.enabled; return { work_enabled: saved.enabled }; }
    if (path.endsWith("/work/item-1/updates") && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      const update = { id: "update-1", engagement_id: project.id, item_id: "item-1", summary: body.summary, next_step: body.next_step, blocker: body.blocker, status: body.status, actor_kind: "operator", actor_id: "operator", source_session_id: null, source_turn_id: null, source_run_id: null, created_at: "2026-01-01T13:00:00Z" };
      saved.updates = [update]; saved.item.status = body.status; saved.item.last_update_at = update.created_at;
      return update;
    }
    if (path.endsWith("/work/item-1/updates")) return saved.updates;
    if (path.endsWith("/work")) return [saved.item];
    return [];
  });
  return {
    saved,
    workspace: { api: { request, listChatSessions: vi.fn(async () => ({ items: [{ id: "session-1", title: "Search implementation" }] })), listChatSessionActivity: vi.fn(async () => [{ sessionId: "session-1", state: "working" }]) },
      coreState: "online", engagement: project, engagements: [project] },
  };
});

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));

function openItem() {
  return render(<MemoryRouter initialEntries={["/projects/project-1/work/item-1"]}><Routes><Route path="/projects/:projectId/work/:itemId?" element={<WorkPage />} /></Routes></MemoryRouter>);
}

describe("Work operator journey", () => {
  beforeEach(() => {
    saved.enabled = false; saved.updates = []; saved.item.status = "in_progress"; saved.item.last_update_at = null;
    saved.items = [saved.item]; workspace.engagements = [workspace.engagement];
    vi.clearAllMocks();
  });

  it("shows the assigned active conversation, saves a check-in, and keeps the item selected", async () => {
    const user = userEvent.setup();
    openItem();
    expect(await screen.findByRole("heading", { name: "Build the search page" })).toBeVisible();
    expect(screen.getByText("Search implementation · Working")).toBeVisible();
    const form = screen.getByRole("heading", { name: "Post an update" }).closest("form")!;
    await user.type(within(form).getByRole("textbox", { name: "Progress" }), "Filters render in search results");
    await user.selectOptions(within(form).getByRole("combobox", { name: "Status" }), "review");
    await user.click(within(form).getByRole("button", { name: "Save update" }));
    expect(await screen.findByText("Filters render in search results")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Build the search page" })).toBeVisible();
  });

  it("enables project agent tools without removing saved Work", async () => {
    const user = userEvent.setup();
    openItem();
    await user.click(await screen.findByRole("button", { name: "Enable agent tools" }));
    expect(await screen.findByRole("button", { name: "Agent tools on" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Build the search page" })).toBeVisible();
  });

  it("shows active conversations even before an agent links a Work item", async () => {
    workspace.api.listChatSessions.mockResolvedValueOnce({ items: [
      { id: "session-1", title: "Search implementation" },
      { id: "session-2", title: "Review documentation" },
    ] });
    workspace.api.listChatSessionActivity.mockResolvedValueOnce([
      { sessionId: "session-1", state: "working" },
      { sessionId: "session-2", state: "working" },
    ]);
    render(<MemoryRouter initialEntries={["/work"]}><Routes><Route path="/work" element={<WorkPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("Review documentation")).toBeVisible();
    expect(screen.getByText(/No Work item linked/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Review documentation" })).toHaveAttribute("href", "/projects/project-1/workbench?session=session-2");
  });

  it("pages Work items and lets the operator find a project in a large import", async () => {
    const user = userEvent.setup();
    saved.items = Array.from({ length: 501 }, (_, index) => ({ ...saved.item, id: `import-${index}`, title: `Imported item ${index}`, assignee_session_id: null }));
    workspace.engagements = [workspace.engagement, ...Array.from({ length: 100 }, (_, index) => ({
      id: `project-${index + 2}`, name: `Sample project ${index + 1}`, status: "active", workEnabled: false,
    }))];
    render(<MemoryRouter initialEntries={["/work"]}><Routes><Route path="/work" element={<WorkPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("Showing 80 of 101 projects. Search to narrow the list.")).toBeVisible();
    expect(workspace.api.request).toHaveBeenCalledWith("work/items?offset=500&limit=500", expect.any(Object));
    expect(screen.queryByRole("link", { name: /Sample project 100/ })).not.toBeInTheDocument();
    await user.type(screen.getByRole("searchbox", { name: "Search projects" }), "Sample project 100");
    expect(screen.getByRole("link", { name: /Sample project 100/ })).toBeVisible();
  });
});
