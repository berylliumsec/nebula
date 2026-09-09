import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ChatQueuePanel } from "./ChatQueuePanel";
import type { useChatQueue } from "../pages/useChatQueue";

function controller(status = "queued", error?: string) {
  return { queue: { revision: 1, paused: false, items: [{ id: "one", key: "one", status, request: { messages: [{ role: "user", content: "A very long queued message ".repeat(30) }] } }] }, busy: false, error, mutate: vi.fn().mockResolvedValue(true), reload: vi.fn() } as unknown as ReturnType<typeof useChatQueue>;
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
