import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { PROJECT_RUNTIME_UNAVAILABLE, ProjectRuntimeRecovery } from "../api/projectRuntime";
import { ChatCatchUp } from "./ChatCatchUp";
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));
const record = { initialized: true, revision: 2, through_at: "2026-09-09T12:00:00Z", items: [], pending: [], truncated: false };
const props = { sessionId: "chat", ready: true, atLatest: true, onMessage: vi.fn(), onPending: vi.fn(), onTurn: vi.fn() };

it("uses authoritative pending requests, retaining another request on the same turn", async () => {
  const request = vi.fn().mockResolvedValue({ ...record, items: [{ id: "turn", kind: "pending", text: "Old approval" }], pending: [
    { id: "turn", turn_id: "turn", kind: "pending", text: "Old approval" },
    { id: "question", turn_id: "turn", kind: "pending", text: "Answer still needed" },
  ] });
  render(<ChatCatchUp {...props} pendingActions={[{id: "question", turn_id: "turn", kind: "input", text: "Answer still needed"}]} api={{ request } as unknown as ApiClient} />);
  expect(await screen.findByText("1 action needs review")).toBeVisible();
  expect(screen.queryByRole("region", { name: "Catch up on this conversation" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Review pending actions" }));
  expect(props.onPending).toHaveBeenCalled();
});

it("keeps recovery understandable and retries through the accessible refresh icon", async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(record);
  render(<ChatCatchUp {...props} api={{ request } as unknown as ApiClient} />);
  const button = await screen.findByRole("button", { name: "Reload catch-up" });
  expect(button).toHaveAttribute("title", "Reload catch-up");
  expect(button.textContent).toBe("");
  expect(screen.getByRole("status")).toHaveTextContent("Pending actions remain available");
  fireEvent.click(button);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole("button", { name: "Reload catch-up" })).toBeNull();
});

it("dismisses catch-up through the close icon and acknowledges the current revision", async () => {
  const request = vi.fn().mockResolvedValueOnce({ ...record, items: [{ id: "m", message_id: "m", kind: "message", text: "An update" }] }).mockResolvedValue({});
  render(<ChatCatchUp {...props} api={{ request } as unknown as ApiClient} />);
  const button = await screen.findByRole("button", { name: "Dismiss catch-up" });
  expect(button.textContent).toBe("");
  fireEvent.click(button);
  await waitFor(() => expect(screen.queryByRole("region", { name: "Catch up on this conversation" })).toBeNull());
  expect(request.mock.calls[1][0]).toBe("chat/sessions/chat/read-cursor");
  expect(JSON.parse(request.mock.calls[1][1].body).expected_revision).toBe(2);
});

it("opens a catch-up item and clears its acknowledged summary while keeping pending actions", async () => {
  const request = vi.fn().mockResolvedValueOnce({ ...record, items: [{ id: "m", message_id: "m", kind: "message", text: "An update" }], pending: [{ id: "p", kind: "approval", text: "Approve" }] }).mockResolvedValue({});
  const onMessage = vi.fn();
  render(<ChatCatchUp {...props} onMessage={onMessage} api={{ request } as unknown as ApiClient} />);
  fireEvent.click(await screen.findByRole("button", { name: /An update/ }));
  expect(onMessage).toHaveBeenCalledWith("m");
  await waitFor(() => expect(screen.queryByRole("region", { name: "Catch up on this conversation" })).toBeNull());
  expect(screen.getByRole("button", { name: "Review pending actions" })).toBeVisible();
  expect(JSON.parse(request.mock.calls[1][1].body).expected_revision).toBe(2);
});

it("leaves a message the transcript has not rendered unread while the viewer is at the bottom", async () => {
  const unseen = { ...record, items: [{ id: "answer", message_id: "answer", kind: "completed", text: "The second turn's answer" }] };
  const request = vi.fn().mockResolvedValueOnce(record).mockResolvedValue(unseen);
  const rendered = new Set<string>();
  render(<ChatCatchUp {...props} isShown={id => rendered.has(id)} api={{ request } as unknown as ApiClient} />);
  const acknowledged = () => request.mock.calls.some(call => call[0] === "chat/sessions/chat/read-cursor");
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  // A turn Core ran on its own saved an answer this page does not show yet.
  await waitFor(() => { document.dispatchEvent(new Event("visibilitychange")); expect(request.mock.calls.length).toBeGreaterThanOrEqual(3); });
  expect(acknowledged()).toBe(false);
  // Once the transcript renders it, reading the bottom marks it read.
  rendered.add("answer");
  await waitFor(() => { document.dispatchEvent(new Event("visibilitychange")); expect(acknowledged()).toBe(true); });
  expect(JSON.parse(request.mock.calls.find(call => call[0] === "chat/sessions/chat/read-cursor")![1].body).expected_revision).toBe(2);
});

function runtimeAway(path: string, recovery: ProjectRuntimeRecovery) {
  recovery.unavailable(path);
  // Shaped like the client's ApiError; diagnostics are mocked in this file.
  return Object.assign(new Error("idle"), { status: 503, code: PROJECT_RUNTIME_UNAVAILABLE, retryable: true });
}

it("waits without an error while the project's worker is idle and syncs again when it answers", async () => {
  // Answered only once the test readmits the worker.
  let ready = false;
  const recovery = new ProjectRuntimeRecovery(async () => ({ answered: ready }));
  const request = vi.fn(async (path: string) => {
    if (!ready) throw runtimeAway(path, recovery);
    recovery.answered(path);
    return record;
  });
  render(<ChatCatchUp {...props} api={{ request, projectRuntime: recovery } as unknown as ApiClient} />);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(recovery.state).toBe("reconnecting"));
  expect(screen.queryByRole("status")).toBeNull();
  expect(screen.queryByRole("button", { name: "Reload catch-up" })).toBeNull();
  ready = true;
  // The shared probe reads again within its first one-second backoff.
  await waitFor(() => expect(request.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 });
  await waitFor(() => expect(recovery.state).toBe("available"));
  expect(screen.queryByText(/Read state could not sync/)).toBeNull();
});

it("clears a read-state failure once a later check succeeds", async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(record);
  render(<ChatCatchUp {...props} api={{ request } as unknown as ApiClient} />);
  expect(await screen.findByText(/Read state could not sync/)).toBeVisible();
  // Returning to the page checks again without the operator pressing refresh.
  document.dispatchEvent(new Event("visibilitychange"));
  await waitFor(() => expect(screen.queryByText(/Read state could not sync/)).toBeNull());
  expect(request).toHaveBeenCalledTimes(2);
});
