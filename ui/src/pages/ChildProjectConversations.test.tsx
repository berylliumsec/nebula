import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ChatSessionSummary, EngagementSummary } from "../api/types";
import { ChildProjectConversations } from "./ChildProjectConversations";

const child = { id: "child", name: "Child project" } as EngagementSummary;

function chat(id: string, title: string, overrides: Partial<ChatSessionSummary> = {}): ChatSessionSummary {
  return {
    id, title, engagementId: child.id, backend: "harness", toolsEnabled: true,
    mcpServerIds: [], hookIds: [], createdAt: "2026-11-12T10:00:00Z", updatedAt: "2026-11-12T10:00:00Z", revision: 1,
    ...overrides,
  };
}

function mount(api: ApiClient, query = "") {
  return render(<MemoryRouter initialEntries={["/projects/parent/workbench?view=chat"]}><Routes>
    <Route path="/projects/parent/workbench" element={<ChildProjectConversations api={api} project={child} query={query} />} />
    <Route path="/projects/child/workbench" element={<p>Child conversation opened</p>} />
  </Routes></MemoryRouter>);
}

describe("child project conversations in parent sidebar", () => {
  it("lists child-owned chats, searches the full list, and opens the child route", async () => {
    const user = userEvent.setup();
    const items = Array.from({ length: 10 }, (_, index) => chat(`chat-${index}`, `Research chat ${index}`));
    items.push(chat("example", "Example chat", { archivedAt: "2026-11-12T11:00:00Z" }));
    items.push(chat("foreign", "Other project", { engagementId: "other" }));
    items.push(chat("side", "Side chat", { isSideChat: true }));
    const api = {
      listChatSessions: vi.fn(async () => ({ items })),
      listChatSessionActivity: vi.fn(async () => [{ sessionId: "chat-0", state: "working" }]),
    } as unknown as ApiClient;
    const view = mount(api);
    const section = await screen.findByRole("region", { name: `${child.name} conversations` });
    expect(await within(section).findByText("11")).toBeVisible();
    expect(within(section).getByRole("button", { name: `${child.name} chats` })).toHaveAttribute("aria-expanded", "true");
    expect(within(section).getAllByRole("link")).toHaveLength(8);
    expect(within(section).queryByText("Other project")).not.toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Show more chats (3)" }));
    expect(within(section).getByRole("link", { name: `Open Example chat in ${child.name}` }))
      .toHaveAttribute("href", "/projects/child/workbench?view=chat&session=example");
    view.rerender(<MemoryRouter initialEntries={["/projects/parent/workbench?view=chat"]}><ChildProjectConversations api={api} project={child} query="Example" /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: `Open Example chat in ${child.name}` })).toBeVisible();
    expect(screen.queryByRole("link", { name: `Open Research chat 0 in ${child.name}` })).not.toBeInTheDocument();
  });

  it("keeps a retry control after a failed read", async () => {
    const user = userEvent.setup();
    const api = {
      listChatSessions: vi.fn()
        .mockRejectedValueOnce(new Error("connection lost"))
        .mockResolvedValueOnce({ items: [chat("recovered", "Recovered chat")] }),
      listChatSessionActivity: vi.fn(async () => []),
    } as unknown as ApiClient;
    mount(api);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load chats: connection lost");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("link", { name: `Open Recovered chat in ${child.name}` })).toBeVisible();
  });
});
