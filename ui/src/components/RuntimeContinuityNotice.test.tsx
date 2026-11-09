import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { RuntimeContinuityNotice } from "./RuntimeContinuityNotice";

function client(items: unknown[], persistent = true) {
  return {
    getContinuityScope: () => ({key: "core:operator", persistent, limitation: persistent ? undefined : "Drafts and pending requests are kept only in this window."}),
    getPendingRequests: vi.fn().mockResolvedValue(items),
    reconcilePendingRequests: vi.fn().mockResolvedValue(items),
    subscribeContinuity: vi.fn().mockReturnValue(() => {}),
    dismissCompletedRequest: vi.fn().mockResolvedValue(undefined),
    streamChat: vi.fn(), resumeChatTurn: vi.fn(), request: vi.fn(),
  };
}

it("reopens saved work with a read-only check and an exact conversation link", async () => {
  const api = client([{key: "key", path: "chat/completions", state: "accepted", replayable: true, summary: "Once", status: {state: "running", project_id: "project", session_id: "session"}}]);
  render(<MemoryRouter><RuntimeContinuityNotice api={api as unknown as ApiClient} /></MemoryRouter>);
  await waitFor(() => expect(api.reconcilePendingRequests).toHaveBeenCalledOnce());
  await userEvent.click(await screen.findByText("1 request awaiting confirmation"));
  expect(screen.getByRole("link", {name: "Open saved conversation"})).toHaveAttribute("href", "/projects/project/workbench?view=chat&session=session");
  await userEvent.click(screen.getByRole("button", {name: "Check saved requests"}));
  expect(api.reconcilePendingRequests).toHaveBeenCalledTimes(2);
  expect(api.streamChat).not.toHaveBeenCalled();
  expect(api.resumeChatTurn).not.toHaveBeenCalled();
  expect(api.request).not.toHaveBeenCalled();
});

it("keeps unresolved decisions visible and offers no dismissal", async () => {
  const api = client([{key: "key", path: "approvals/id/decision", state: "uncertain", replayable: true, status: {state: "uncertain"}}]);
  render(<MemoryRouter><RuntimeContinuityNotice api={api as unknown as ApiClient} /></MemoryRouter>);
  expect(await screen.findByText("Approval decision · uncertain")).toBeVisible();
  expect(screen.queryByRole("button", {name: "Dismiss completed request"})).not.toBeInTheDocument();
});

it("shows the persistence limitation without migrating or submitting drafts", async () => {
  const api = client([], false);
  render(<MemoryRouter><RuntimeContinuityNotice api={api as unknown as ApiClient} /></MemoryRouter>);
  expect(await screen.findByText("Drafts and pending requests are kept only in this window.")).toBeVisible();
  expect(api.reconcilePendingRequests).not.toHaveBeenCalled();
});
