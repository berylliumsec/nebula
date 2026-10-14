import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import type { ChatAttentionItem } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { MobileAttentionInbox } from "./MobileAttentionInbox";

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: vi.fn() }));

const pending: ChatAttentionItem = {
  kind: "approval", actionId: "approval-1", engagementId: "project-1",
  engagementName: "Project one", sessionId: "chat-1", sessionTitle: "Agent review",
  requestedAt: "2026-10-14T12:00:00Z",
};

function mount(read: () => Promise<ChatAttentionItem[]>, onOpenChat = vi.fn()) {
  vi.mocked(useWorkspace).mockReturnValue({
    api: {listChatAttention: read}, workspaceState: "ready",
  } as unknown as ReturnType<typeof useWorkspace>);
  render(<MobileAttentionInbox onOpenChat={onOpenChat} />);
  return onOpenChat;
}

afterEach(() => vi.clearAllMocks());

it("reads Core again after a decision and never leaves a resolved action in the badge", async () => {
  let actionPending = true;
  const read = vi.fn(async () => actionPending ? [pending] : []);
  const onOpenChat = mount(read);
  await screen.findByRole("button", {name: "Needs you, 1 pending"});
  await userEvent.click(screen.getByRole("button", {name: "Needs you, 1 pending"}));
  await userEvent.click(screen.getByRole("button", {name: /Approval needed.*Agent review.*Project one/}));
  expect(onOpenChat).toHaveBeenCalledWith(pending);
  actionPending = false;
  act(() => window.dispatchEvent(new Event("nebula:chat-attention-changed")));
  await waitFor(() => expect(screen.getByRole("button", {name: "Needs you"})).toBeVisible());
  await userEvent.click(screen.getByRole("button", {name: "Needs you"}));
  expect(screen.getByText("All caught up")).toBeVisible();
});

it("removes stale pending actions on read failure and recovers through Retry", async () => {
  let failing = false;
  let resolved = false;
  const read = vi.fn(async () => {
    if (failing) throw new Error("Core offline");
    return resolved ? [] : [pending];
  });
  mount(read);
  await screen.findByRole("button", {name: "Needs you, 1 pending"});
  failing = true;
  act(() => window.dispatchEvent(new Event("nebula:chat-attention-changed")));
  await waitFor(() => expect(screen.getByRole("button", {name: "Needs you, unavailable"})).toBeVisible());
  await userEvent.click(screen.getByRole("button", {name: "Needs you, unavailable"}));
  expect(screen.getByRole("alert")).toHaveTextContent("Could not load pending actions");
  failing = false;
  resolved = true;
  await userEvent.click(screen.getByRole("button", {name: "Retry"}));
  await screen.findByText("All caught up");
  expect(read).toHaveBeenCalledTimes(4);
});
