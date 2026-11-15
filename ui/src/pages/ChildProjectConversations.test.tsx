import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ChatSessionSummary, EngagementSummary, LinkedProjectChat } from "../api/types";
import { ChildProjectConversations } from "./ChildProjectConversations";

const parent = { id: "parent", name: "Program" } as EngagementSummary;

function linked(id: string, title: string, owner: string, state: LinkedProjectChat["state"] = "idle", overrides: Partial<ChatSessionSummary> = {}): LinkedProjectChat {
  return {
    session: {
      id, title, engagementId: owner, backend: "harness", toolsEnabled: true,
      mcpServerIds: [], hookIds: [], createdAt: "2026-11-12T10:00:00Z", updatedAt: "2026-11-12T10:00:00Z", revision: 1,
      ...overrides,
    },
    project: { id: owner, name: `Project ${owner}` },
    state,
  };
}

type Read = ApiClient["listLinkedProjectChatsIfChanged"];

function mount(read: Read, props: { query?: string; visible?: boolean; onMatchesChange?: (value: boolean) => void } = {}) {
  const api = { listLinkedProjectChatsIfChanged: vi.fn(read) } as unknown as ApiClient;
  const element = (query = props.query ?? "", visible = props.visible ?? true) => <MemoryRouter initialEntries={["/projects/parent/workbench?view=chat"]}><Routes>
    <Route path="/projects/parent/workbench" element={<ChildProjectConversations api={api} project={parent} query={query} visible={visible} onMatchesChange={props.onMatchesChange} />} />
    <Route path="/projects/:owner/workbench" element={<p>Owned conversation opened</p>} />
  </Routes></MemoryRouter>;
  const view = render(element());
  return { api, view, rerender: (query?: string, visible?: boolean) => view.rerender(element(query, visible)) };
}

describe("subproject conversations in the parent sidebar", () => {
  it("renders nothing when no linked project has chats", async () => {
    const matches = vi.fn();
    const { api, view } = mount(async () => ({ page: { items: [] } }), { onMatchesChange: matches });
    await waitFor(() => expect(api.listLinkedProjectChatsIfChanged).toHaveBeenCalledTimes(1));
    expect(view.container).toBeEmptyDOMElement();
    expect(screen.queryByText(/No saved chats/)).not.toBeInTheDocument();
    expect(matches).not.toHaveBeenCalledWith(true);
  });

  it("lists Core's order in one list, labels owners, pages, and opens the owning project", async () => {
    const user = userEvent.setup();
    const first = [
      linked("wait", "Needs approval", "b", "waiting"),
      linked("run", "Running", "c", "working"),
      ...Array.from({ length: 23 }, (_, index) => linked(`idle-${index}`, `Idle ${index}`, "a")),
    ];
    const read = vi.fn<Read>(async (_project, options) => options.offset === 0 && options.limit === 25
      ? { page: { items: first, nextOffset: 25 } }
      : { page: { items: [...first, linked("late", "Later chat", "a")] } });
    const { api } = mount(read);
    const list = await screen.findByRole("region", { name: "Subproject conversations" });
    expect(within(list).getByRole("button", { name: "Subproject chats" })).toHaveTextContent("25+");
    const links = within(list).getAllByRole("link");
    expect(links[0]).toHaveAccessibleName("Open Needs approval in Project b");
    expect(links[1]).toHaveAccessibleName("Open Running in Project c");
    expect(within(list).getByRole("group", { name: "Needs you" })).toHaveTextContent("Project b · Waiting for you");
    expect(api.listLinkedProjectChatsIfChanged).toHaveBeenCalledWith("parent", { query: "", offset: 0, limit: 25 }, undefined, expect.any(AbortSignal));
    await user.click(within(list).getByRole("button", { name: "Show more chats" }));
    expect(await within(list).findByRole("link", { name: "Open Later chat in Project a" })).toBeVisible();
    expect(read).toHaveBeenLastCalledWith("parent", { query: "", offset: 0, limit: 50 }, undefined, expect.any(AbortSignal));
    expect(within(list).queryByRole("button", { name: "Show more chats" })).not.toBeInTheDocument();
    await user.click(within(list).getByRole("link", { name: "Open Needs approval in Project b" }));
    expect(await screen.findByText("Owned conversation opened")).toBeVisible();
  });

  it("searches in Core and reports whether anything matched", async () => {
    const matches = vi.fn();
    const read = vi.fn<Read>(async (_project, options) => ({
      page: { items: options.query ? [] : [linked("one", "Research", "a")] },
    }));
    const { rerender } = mount(read, { onMatchesChange: matches });
    expect(await screen.findByRole("link", { name: "Open Research in Project a" })).toBeVisible();
    expect(matches).toHaveBeenLastCalledWith(true);
    rerender("nothing here");
    await waitFor(() => expect(read).toHaveBeenLastCalledWith("parent", { query: "nothing here", offset: 0, limit: 25 }, undefined, expect.any(AbortSignal)));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Subproject conversations" })).not.toBeInTheDocument());
    expect(matches).toHaveBeenLastCalledWith(false);
  });

  it("does not read while the conversation list is hidden", async () => {
    const read = vi.fn<Read>(async () => ({ page: { items: [linked("one", "Research", "a")] } }));
    const { rerender } = mount(read, { visible: false });
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    expect(read).not.toHaveBeenCalled();
    rerender("", true);
    expect(await screen.findByRole("link", { name: "Open Research in Project a" })).toBeVisible();
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("keeps a retry control after a failed read", async () => {
    const user = userEvent.setup();
    const read = vi.fn<Read>()
      .mockRejectedValueOnce(new Error("connection lost"))
      .mockResolvedValueOnce({ page: { items: [linked("recovered", "Recovered chat", "a")] } });
    mount(read);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load subproject chats: connection lost");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("link", { name: "Open Recovered chat in Project a" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
