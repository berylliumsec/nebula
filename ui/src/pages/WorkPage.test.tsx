import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkPage } from "./WorkPage";

const { workspace, saved, live } = vi.hoisted(() => {
  const project: { id: string; name: string; status: string; workEnabled: boolean; parentEngagementId?: string } = { id: "project-1", name: "Documentation portal", status: "active", workEnabled: true };
  const saved = {
    enabled: true,
    agents: [] as Array<{ session_id: string; engagement_id: string; title: string; state: "working" | "waiting"; turn_id: string }>,
    item: { id: "item-1", engagement_id: project.id, title: "Build the search page", description: "Add filters", status: "in_progress", priority: "normal", assignee_session_id: "session-1", source_kind: "chat", source_id: "session-1", created_at: "2026-01-01T12:00:00Z", updated_at: "2026-01-01T12:00:00Z", last_update_at: null } as Record<string, unknown>,
    items: [] as Array<Record<string, unknown>>,
    updates: [] as Array<Record<string, unknown>>,
  };
  const live = {
    onChange: undefined as undefined | ((kind: "work" | "projects") => void),
    onReady: undefined as undefined | (() => void),
  };
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path.startsWith("work/items?")) {
      const query = new URLSearchParams(path.split("?")[1]);
      const offset = Number(query.get("offset") ?? 0);
      const limit = Number(query.get("limit") ?? 500);
      return saved.items.slice(offset, offset + limit);
    }
    if (path === "work/agents") return saved.agents;
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
    live,
    workspace: { api: { request, listChatSessions: vi.fn(async () => ({ items: [{ id: "session-1", title: "Search implementation" }] })), listChatSessionActivity: vi.fn(async () => [{ sessionId: "session-1", state: "working" }]), watchWorkChanges: vi.fn((onChange: typeof live.onChange, onReady: typeof live.onReady, signal: AbortSignal) => {
      live.onChange = onChange; live.onReady = onReady;
      return new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
    }) },
      coreState: "online", engagement: project, engagements: [project], retryResource: vi.fn(async () => undefined) },
  };
});

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));

function openItem() {
  return render(<MemoryRouter initialEntries={["/projects/project-1/work/item-1"]}><Routes><Route path="/projects/:projectId/work/:itemId?" element={<WorkPage />} /></Routes></MemoryRouter>);
}

describe("Work operator journey", () => {
  beforeEach(() => {
    saved.enabled = true; saved.agents = []; saved.updates = []; saved.item.status = "in_progress"; saved.item.last_update_at = null;
    live.onChange = undefined; live.onReady = undefined;
    saved.items = [saved.item]; workspace.engagements = [workspace.engagement];
    vi.clearAllMocks();
  });

  it("shows the assigned active conversation, saves a check-in, and keeps the item selected", async () => {
    const user = userEvent.setup();
    openItem();
    expect(await screen.findByRole("heading", { name: "Build the search page" })).toBeVisible();
    expect(screen.getByText("Search implementation · Working")).toBeVisible();
    expect(screen.getByRole("link", { name: "Search implementation" })).toHaveAttribute("href", "/projects/project-1/workbench?view=chat&session=session-1");
    const form = screen.getByRole("heading", { name: "Post an update" }).closest("form")!;
    await user.type(within(form).getByRole("textbox", { name: "Progress" }), "Filters render in search results");
    await user.selectOptions(within(form).getByRole("combobox", { name: "Status" }), "review");
    await user.click(within(form).getByRole("button", { name: "Save update" }));
    expect(await screen.findByText("Filters render in search results")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Build the search page" })).toBeVisible();
  });

  it("starts with agent tools on and keeps saved Work when they are disabled", async () => {
    const user = userEvent.setup();
    openItem();
    await user.click(await screen.findByRole("button", { name: "Disable agent tools" }));
    expect(await screen.findByRole("button", { name: "Enable agent tools" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Build the search page" })).toBeVisible();
  });

  it("shows active conversations even before an agent links a Work item", async () => {
    saved.agents = [{ session_id: "session-2", engagement_id: "project-1", title: "Review documentation", state: "working", turn_id: "turn-2" }];
    render(<MemoryRouter initialEntries={["/work"]}><Routes><Route path="/work" element={<WorkPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("Review documentation")).toBeVisible();
    expect(screen.getByText(/No Work item linked/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Review documentation" })).toHaveAttribute("href", "/projects/project-1/workbench?view=chat&session=session-2");
    expect(workspace.api.request).toHaveBeenCalledWith("work/agents", expect.any(Object));
    expect(workspace.api.listChatSessionActivity).not.toHaveBeenCalled();
  });

  it("waits for the operator to refresh instead of polling", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/work"]}><Routes><Route path="/work" element={<WorkPage />} /></Routes></MemoryRouter>);
    await screen.findByText("No agents are working right now.");
    const loadedCalls = workspace.api.request.mock.calls.length;
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(60_000);
      expect(workspace.api.request).toHaveBeenCalledTimes(loadedCalls);
    } finally {
      vi.useRealTimers();
    }
    await user.click(screen.getByRole("button", { name: "Refresh Work" }));
    expect(workspace.api.request.mock.calls.length).toBeGreaterThan(loadedCalls);
  });

  it("shows a posted agent update when Core signals a change", async () => {
    openItem();
    expect(await screen.findByText("No check-ins yet.")).toBeVisible();
    saved.updates = [{ id: "external-update", engagement_id: "project-1", item_id: "item-1", summary: "External agent update", status: "review", actor_kind: "agent", actor_id: "session-1", source_session_id: "session-1", created_at: "2026-01-01T14:00:00Z" }];
    await act(async () => { live.onReady?.(); live.onChange?.("work"); });
    expect(await screen.findByText("External agent update")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open conversation" })).toHaveAttribute("href", "/projects/project-1/workbench?view=chat&session=session-1");
    expect(screen.getByRole("status")).toHaveTextContent("Live");
  });

  it("links a child project check-in to its parent conversation", async () => {
    saved.updates = [{
      id: "parent-update", engagement_id: "project-1", item_id: "item-1",
      summary: "Research checkpoint", status: "in_progress", actor_kind: "agent",
      actor_id: "parent-session", source_session_id: "parent-session",
      source_engagement_id: "parent-project", created_at: "2026-01-01T14:00:00Z",
    }];
    openItem();
    expect(await screen.findByText("Research checkpoint")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open conversation" })).toHaveAttribute(
      "href", "/projects/parent-project/workbench?view=chat&session=parent-session"
    );
  });

  it("pages Work items and lets the operator find a project in a large import", async () => {
    const user = userEvent.setup();
    saved.items = Array.from({ length: 501 }, (_, index) => ({ ...saved.item, id: `import-${index}`, title: `Imported item ${index}`, assignee_session_id: null }));
    workspace.engagements = [workspace.engagement, ...Array.from({ length: 100 }, (_, index) => ({
      id: `project-${index + 2}`, name: `Sample project ${index + 1}`, status: "active", workEnabled: false,
    }))];
    render(<MemoryRouter initialEntries={["/work"]}><Routes><Route path="/work" element={<WorkPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("Showing 80 rows. Search to narrow the list.")).toBeVisible();
    expect(workspace.api.request).toHaveBeenCalledWith("work/items?offset=500&limit=500", expect.any(Object));
    expect(screen.queryByRole("link", { name: /Sample project 100/ })).not.toBeInTheDocument();
    await user.type(screen.getByRole("searchbox", { name: "Search projects" }), "Sample project 100");
    expect(screen.getByRole("link", { name: /Sample project 100/ })).toBeVisible();
  });

  it("groups subprojects under their parent and finds them by name", async () => {
    const user = userEvent.setup();
    workspace.engagements = [workspace.engagement, { id: "child-1", name: "Review plan", status: "active", workEnabled: true, parentEngagementId: "project-1" }];
    render(<MemoryRouter initialEntries={["/work"]}><Routes><Route path="/work" element={<WorkPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("1 subprojects · Agent tools on")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Review plan/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show subprojects of Documentation portal" }));
    expect(screen.getByRole("link", { name: /Review plan/ })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Hide subprojects of Documentation portal" }));
    await user.type(screen.getByRole("searchbox", { name: "Search projects" }), "Review plan");
    expect(screen.getByRole("link", { name: /Documentation portal/ })).toBeVisible();
    expect(screen.getByRole("link", { name: /Review plan/ })).toBeVisible();
  });
});
