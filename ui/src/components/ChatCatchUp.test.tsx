import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ChatCatchUp } from "./ChatCatchUp";
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));
const record = { initialized: true, revision: 2, through_at: "2026-09-09T12:00:00Z", items: [], pending: [], truncated: false };
const props = { sessionId: "chat", ready: true, atLatest: true, onMessage: vi.fn(), onPending: vi.fn(), onTurn: vi.fn() };

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
