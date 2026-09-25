import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ChatQueuePanel } from "./ChatQueuePanel";
import { SelectionActionsProvider } from "./selection";
import type { useChatQueue } from "../pages/useChatQueue";

function controller(status = "queued", error?: string, turnId?: string) {
  return { queue: { revision: 1, paused: false, items: [{ id: "one", key: "one", status, turn_id: turnId, request: { messages: [{ role: "user", content: "A very long queued message ".repeat(30) }] } }] }, busy: false, error, mutate: vi.fn().mockResolvedValue(true), reload: vi.fn() } as unknown as ReturnType<typeof useChatQueue>;
}
it("keeps queued text and management controls behind a compact disclosure", () => {
  const queue = controller();
  const view = render(<ChatQueuePanel queue={queue} onRefreshConversation={vi.fn()} />);
  const details = view.container.querySelector("details")!;
  expect(details.open).toBe(false);
  expect(screen.getByText("1 follow-up · Queued")).toBeVisible();
  details.open = true;
  fireEvent(details, new Event("toggle"));
  fireEvent.click(screen.getByRole("button", { name: "Pause queue" }));
  expect(queue.mutate).toHaveBeenCalledWith({ action: "pause" });
});
it("exposes queue errors and review actions immediately", () => {
  const view = render(<ChatQueuePanel queue={controller("needs_review", "Queue unavailable")} onRefreshConversation={vi.fn()} />);
  expect(view.container.querySelector("details")!.open).toBe(true);
  expect(screen.getByRole("alert")).toHaveTextContent("Queue unavailable");
  expect(screen.getByRole("button", { name: "Retry as new message" })).toBeVisible();
});
it("removes a dispatched follow-up from the visible queue once Core links its turn", () => {
  const {rerender} = render(<ChatQueuePanel queue={controller("sending")} onRefreshConversation={vi.fn()} />);
  expect(screen.getByRole("region", {name: "Core follow-up queue"})).toHaveTextContent("1 follow-up · Sending");

  rerender(<ChatQueuePanel queue={controller("sending", undefined, "turn-one")} onRefreshConversation={vi.fn()} />);
  expect(screen.queryByRole("region", {name: "Core follow-up queue"})).not.toBeInTheDocument();

  rerender(<ChatQueuePanel queue={controller("needs_review", undefined, "turn-one")} onRefreshConversation={vi.fn()} />);
  expect(screen.getByRole("region", {name: "Core follow-up queue"})).toHaveTextContent("Needs attention");
});
it("keeps selected-text actions off the queued-message editor and its Save control", () => {
  const queue = controller();
  const view = render(<SelectionActionsProvider onAsk={vi.fn()}><ChatQueuePanel queue={queue} onRefreshConversation={vi.fn()} /></SelectionActionsProvider>);
  const details = view.container.querySelector("details")!;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  fireEvent.click(screen.getByRole("button", { name: "Edit queued message 1" }));
  // Replacing the text starts by selecting all of it, as a phone's Select All does.
  const editor = screen.getByRole("textbox", { name: "Edit queued text" }) as HTMLTextAreaElement;
  editor.setSelectionRange(0, editor.value.length);
  fireEvent.select(editor);
  fireEvent.change(editor, { target: { value: "Edited queued first task" } });

  expect(screen.queryByRole("toolbar", { name: "Selected text actions" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Save queued edit" }));
  expect(queue.mutate).toHaveBeenCalledWith(expect.objectContaining({ action: "edit", item_id: "one" }));
});
