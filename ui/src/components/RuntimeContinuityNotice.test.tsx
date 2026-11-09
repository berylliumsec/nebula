import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { diagnosticsFallbackErrors } from "../diagnostics";
import { RuntimeContinuityNotice } from "./RuntimeContinuityNotice";

function client(items: unknown[], persistent = true) {
  return {
    getContinuityScope: () => ({key: "core:operator", persistent, limitation: persistent ? undefined : "Drafts and pending requests are kept only in this window."}),
    getPendingRequests: vi.fn().mockResolvedValue(items),
    reconcilePendingRequests: vi.fn().mockResolvedValue(items),
    subscribeContinuity: vi.fn<(listener: () => void) => () => void>().mockReturnValue(() => {}),
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

it("records a local read failure safely and ignores a cancelled read after unmount", async () => {
  const code = "interface.continuity.saved_requests_load_failed";
  const before = diagnosticsFallbackErrors().filter(item => item.event_code === code).length;
  const canary = "canary-local-storage-path-and-draft";
  const api = client([]);
  api.getPendingRequests.mockRejectedValueOnce(new Error(canary));
  const view = render(<MemoryRouter><RuntimeContinuityNotice api={api as unknown as ApiClient} /></MemoryRouter>);
  expect(await screen.findByRole("alert")).toHaveTextContent(canary);
  const records = diagnosticsFallbackErrors().filter(item => item.event_code === code);
  expect(records).toHaveLength(before + 1);
  expect(records.at(-1)).toMatchObject({level: "error", stage: "saved-requests-load"});
  expect(JSON.stringify(records.at(-1))).not.toContain(canary);
  let rejectLate!: (error: Error) => void;
  api.getPendingRequests.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectLate = reject; }));
  act(() => api.subscribeContinuity.mock.calls[0][0]());
  view.unmount();
  await act(async () => rejectLate(new DOMException("Viewer closed", "AbortError")));
  expect(diagnosticsFallbackErrors().filter(item => item.event_code === code)).toHaveLength(before + 1);
  expect(api.request).not.toHaveBeenCalled();
});

it("retains saved work and permits retry after a failed manual check without logging request content", async () => {
  const items = [{key: "canary-request-key", path: "chat/completions", state: "uncertain", replayable: true, summary: "canary-draft-summary"}];
  const canary = "canary-check-storage-detail";
  const api = client(items);
  api.reconcilePendingRequests.mockResolvedValueOnce(items).mockRejectedValueOnce(new Error(canary)).mockResolvedValueOnce(items);
  render(<MemoryRouter><RuntimeContinuityNotice api={api as unknown as ApiClient} /></MemoryRouter>);
  expect(await screen.findByText("canary-draft-summary · Acceptance unconfirmed")).toBeVisible();
  await userEvent.click(screen.getByRole("button", {name: "Check saved requests"}));
  expect(await screen.findByRole("alert")).toHaveTextContent(canary);
  expect(screen.getByText("canary-draft-summary · Acceptance unconfirmed")).toBeVisible();
  expect(screen.getByRole("button", {name: "Check saved requests"})).toBeEnabled();
  const record = diagnosticsFallbackErrors().filter(item => item.event_code === "interface.continuity.saved_requests_check_failed").at(-1);
  expect(record).toMatchObject({level: "error", stage: "saved-requests-check"});
  expect(JSON.stringify(record)).not.toContain("canary-");
  await userEvent.click(screen.getByRole("button", {name: "Check saved requests"}));
  await waitFor(() => expect(api.reconcilePendingRequests).toHaveBeenCalledTimes(3));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(api.streamChat).not.toHaveBeenCalled();
  expect(api.resumeChatTurn).not.toHaveBeenCalled();
  expect(api.request).not.toHaveBeenCalled();
});

it("keeps completed work until dismissal succeeds and records failures without its identity", async () => {
  const api = client([{key: "canary-completed-request-key", path: "chat/completions", state: "accepted", replayable: true,
    summary: "canary-completed-summary", status: {state: "succeeded"}}]);
  api.dismissCompletedRequest.mockRejectedValueOnce(new Error("canary-storage-removal-detail"));
  render(<MemoryRouter><RuntimeContinuityNotice api={api as unknown as ApiClient} /></MemoryRouter>);
  await userEvent.click(await screen.findByText("1 request awaiting confirmation"));
  await userEvent.click(screen.getByRole("button", {name: "Dismiss completed request"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("canary-storage-removal-detail");
  expect(screen.getByText("canary-completed-summary · succeeded")).toBeVisible();
  const record = diagnosticsFallbackErrors().filter(item => item.event_code === "interface.continuity.saved_request_dismiss_failed").at(-1);
  expect(record).toMatchObject({level: "error", stage: "saved-request-dismiss"});
  expect(JSON.stringify(record)).not.toContain("canary-");
  await userEvent.click(screen.getByRole("button", {name: "Dismiss completed request"}));
  await waitFor(() => expect(screen.queryByRole("button", {name: "Dismiss completed request"})).not.toBeInTheDocument());
  expect(api.dismissCompletedRequest.mock.calls).toEqual([["canary-completed-request-key"], ["canary-completed-request-key"]]);
  expect(api.streamChat).not.toHaveBeenCalled();
  expect(api.request).not.toHaveBeenCalled();
});
