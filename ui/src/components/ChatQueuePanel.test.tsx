import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ChatQueuePanel } from "./ChatQueuePanel";
import { queueHeading } from "./QueuedMessages";
import { SelectionActionsProvider } from "./selection";
import type { QueueItem, useChatQueue } from "../pages/useChatQueue";

const longText = "A very long queued message ".repeat(30).trim();
function controller(status = "queued", error?: string, turnId?: string, items?: QueueItem[], paused = false) {
  return { queue: { revision: 1, paused, items: items ?? [{ id: "one", key: "one", status, turn_id: turnId, request: { messages: [{ role: "user", content: longText }] } }] }, busy: false, error, mutate: vi.fn().mockResolvedValue(true), reload: vi.fn() } as unknown as ReturnType<typeof useChatQueue>;
}
const row = (id: string, content: string, status = "queued"): QueueItem => ({ id, key: id, status, request: { messages: [{ role: "user", content }] } });

it("shows queued messages docked above the composer with one heading line and quiet controls", () => {
  const queue = controller();
  render(<ChatQueuePanel queue={queue} onRefreshConversation={vi.fn()} />);
  const region = screen.getByRole("region", { name: "Core follow-up queue" });
  expect(within(region).getByRole("status")).toHaveTextContent("1 queued · sent in order after this response");
  // One line per message; the full text stays available as its title.
  expect(within(region).getByTitle(longText)).toBeVisible();
  fireEvent.click(within(region).getByRole("button", { name: "Pause queue" }));
  expect(queue.mutate).toHaveBeenCalledWith({ action: "pause" });
  fireEvent.click(within(region).getByRole("button", { name: "Clear queue" }));
  expect(queue.mutate).toHaveBeenCalledWith({ action: "clear" });
});

it("collapses to its heading on request but reopens when a message needs review", async () => {
  const user = userEvent.setup();
  const view = render(<ChatQueuePanel queue={controller("queued")} onRefreshConversation={vi.fn()} />);
  const toggle = screen.getByRole("button", { expanded: true });
  await user.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("button", { name: "Edit queued message 1" })).toBeNull();
  view.rerender(<ChatQueuePanel queue={controller("needs_review", "Queue unavailable")} onRefreshConversation={vi.fn()} />);
  expect(screen.getByRole("button", { expanded: true })).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("Queue unavailable");
  expect(screen.getByRole("button", { name: "Retry as new message" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Reload queue" })).toBeVisible();
});

it("removes a dispatched follow-up from the visible queue once Core links its turn", () => {
  const {rerender} = render(<ChatQueuePanel queue={controller("sending")} onRefreshConversation={vi.fn()} />);
  expect(screen.getByRole("region", {name: "Core follow-up queue"})).toHaveTextContent("1 queued · sending");

  rerender(<ChatQueuePanel queue={controller("sending", undefined, "turn-one")} onRefreshConversation={vi.fn()} />);
  expect(screen.queryByRole("region", {name: "Core follow-up queue"})).not.toBeInTheDocument();

  rerender(<ChatQueuePanel queue={controller("needs_review", undefined, "turn-one")} onRefreshConversation={vi.fn()} />);
  expect(screen.getByRole("region", {name: "Core follow-up queue"})).toHaveTextContent("needs attention");
  expect(screen.getByRole("button", { name: "Inspect conversation" })).toBeVisible();
});

it("sends a waiting message now, reorders and removes it", () => {
  const queue = controller("queued", undefined, undefined, [row("a", "First"), row("b", "Second")]);
  const onSendNow = vi.fn();
  render(<ChatQueuePanel queue={queue} onRefreshConversation={vi.fn()} onSendNow={onSendNow} interrupts />);
  const sendNow = screen.getByRole("button", { name: "Send queued message 2 now" });
  expect(sendNow).toHaveAttribute("title", "Stop the current response and send this now");
  fireEvent.click(sendNow);
  expect(onSendNow).toHaveBeenCalledWith(expect.objectContaining({ id: "b" }));
  expect(screen.getByRole("button", { name: "Move queued message 1 up" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Move queued message 1 down" }));
  expect(queue.mutate).toHaveBeenCalledWith({ action: "reorder", order: ["b", "a"] });
  fireEvent.click(screen.getByRole("button", { name: "Remove queued message 2" }));
  expect(queue.mutate).toHaveBeenCalledWith({ action: "remove", item_id: "b" });
});

it("offers no Send now without a way to send it and none for a message Core is sending", () => {
  render(<ChatQueuePanel queue={controller("queued", undefined, undefined, [row("a", "First", "sending"), row("b", "Second")])} onRefreshConversation={vi.fn()} />);
  expect(screen.queryByRole("button", { name: /now$/ })).toBeNull();
});

it("names what waits on the operator first in its heading", () => {
  expect(queueHeading([row("a", "x")], true, "after this response")).toBe("Queue paused");
  expect(queueHeading([row("a", "x", "needs_review")], true, "after this response")).toBe("Queue paused · 1 needs attention");
  expect(queueHeading([row("a", "x", "failed"), row("b", "y")], false, "after this response")).toBe("1 queued message needs attention");
  expect(queueHeading([row("a", "x"), row("b", "y")], false, "after this response")).toBe("2 queued · sent in order after this response");
});

it("keeps selected-text actions off the queued-message editor and saves on Enter", () => {
  const queue = controller();
  render(<SelectionActionsProvider onAsk={vi.fn()}><ChatQueuePanel queue={queue} onRefreshConversation={vi.fn()} /></SelectionActionsProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Edit queued message 1" }));
  // Replacing the text starts by selecting all of it, as a phone's Select All does.
  const editor = screen.getByRole("textbox", { name: "Edit queued text" }) as HTMLTextAreaElement;
  expect(editor).toHaveFocus();
  editor.setSelectionRange(0, editor.value.length);
  fireEvent.select(editor);
  fireEvent.change(editor, { target: { value: "Edited queued first task" } });

  expect(screen.queryByRole("toolbar", { name: "Selected text actions" })).toBeNull();
  fireEvent.keyDown(editor, { key: "Enter" });
  expect(queue.mutate).toHaveBeenCalledWith(expect.objectContaining({ action: "edit", item_id: "one", request: expect.objectContaining({ messages: [expect.objectContaining({ content: "Edited queued first task" })] }) }));
});

it("cancels an edit with Escape", () => {
  const queue = controller();
  render(<ChatQueuePanel queue={queue} onRefreshConversation={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Edit queued message 1" }));
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Edit queued text" }), { key: "Escape" });
  expect(screen.queryByRole("textbox", { name: "Edit queued text" })).toBeNull();
  expect(queue.mutate).not.toHaveBeenCalled();
});
