import {act, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {beforeEach, expect, it, vi} from "vitest";
import type {StorageDashboard, StorageVolume} from "../api/types";
import {StoragePage} from "./StoragePage";

const api = {storageDashboard: vi.fn(), retryStorageDispatch: vi.fn()};
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => ({api, coreState: "online", engagements: []})}));
vi.mock("../state/ChromeContext", () => ({useChrome: () => ({toolbarHost: null, trailingToolbarHost: null})}));
vi.mock("../components/StorageActions", () => ({StorageActions: () => null}));
vi.mock("../diagnostics", () => ({logCaughtDiagnostic: vi.fn()}));

const empty = (): StorageDashboard => ({sources: [], devices: [], volumes: [], incidents: [], actions: [], dispatches: []});
const volume = (id: string): StorageVolume => ({id, source_id: "source", device_id: "disk", volume_id: id,
  label: id, path: null, state: "warning", free_bytes: 10, total_bytes: 100,
  observed_at: "2026-11-09T00:00:00Z", received_at: "2026-11-09T00:00:00Z", incident_id: null, stale: false});
function firstPage(): StorageDashboard {
  return {...empty(), sources: [{id: "source", name: "Saved source", enabled: true, engagement_id: "project",
    backend: "provider", profile_id: "profile", model: "synthetic", reporting_interval_seconds: 60,
    callback_url: null, callback_credential_ref: null, last_report_at: null, revision: 1}],
    devices: [{id: "device", source_id: "source", device_id: "disk", label: "Disk", last_report_at: null}],
    volumes: [volume("First volume")], partial: true, next_offset: 1, limit: 1};
}
const mount = () => render(<MemoryRouter><StoragePage /></MemoryRouter>);
beforeEach(() => vi.resetAllMocks());

it("keeps the normal legacy empty state without offering pagination", async () => {
  api.storageDashboard.mockResolvedValue(empty()); mount();
  expect(await screen.findByText("No storage sources")).toBeVisible();
  expect(screen.queryByRole("button", {name: "Load more storage reports"})).not.toBeInTheDocument();
});

it("loads a bounded next page, deduplicates rows and replaces the partial notice", async () => {
  const first = firstPage();
  api.storageDashboard.mockResolvedValueOnce(first).mockResolvedValueOnce({...empty(), sources: first.sources,
    devices: first.devices, volumes: [volume("Second volume")], partial: false, next_offset: null, offset: 1, limit: 1});
  mount();
  expect(await screen.findByText(/Showing a partial storage snapshot/)).toBeVisible();
  const more = screen.getByRole("button", {name: "Load more storage reports"});
  expect(more).toHaveStyle({minHeight: "44px"});
  await userEvent.click(more);
  expect(await screen.findByText("Second volume")).toBeVisible();
  expect(screen.getByText("First volume")).toBeVisible();
  expect(screen.getAllByRole("heading", {name: "Saved source"})).toHaveLength(1);
  expect(api.storageDashboard).toHaveBeenLastCalledWith(undefined, {offset: 1, limit: 1});
  expect(screen.queryByText(/Showing a partial storage snapshot/)).not.toBeInTheDocument();
});

it("retains loaded reports when a next-page read fails and permits an explicit retry", async () => {
  api.storageDashboard.mockResolvedValueOnce(firstPage()).mockRejectedValueOnce(new Error("Temporarily offline"))
    .mockResolvedValueOnce({...empty(), volumes: [volume("Recovered volume")], partial: false, next_offset: null});
  mount();
  await userEvent.click(await screen.findByRole("button", {name: "Load more storage reports"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Existing reports remain here");
  expect(screen.getByText("First volume")).toBeVisible();
  await userEvent.click(screen.getByRole("button", {name: "Load more storage reports"}));
  expect(await screen.findByText("Recovered volume")).toBeVisible();
});

it("does not append a late page after the operator refreshes the snapshot", async () => {
  let finish!: (page: StorageDashboard) => void;
  api.storageDashboard.mockResolvedValueOnce(firstPage())
    .mockImplementationOnce(() => new Promise<StorageDashboard>(resolve => {finish = resolve;}))
    .mockResolvedValueOnce({...firstPage(), volumes: [volume("Refreshed volume")], partial: false, next_offset: null});
  mount();
  await userEvent.click(await screen.findByRole("button", {name: "Load more storage reports"}));
  await userEvent.click(screen.getByRole("button", {name: "Refresh storage"}));
  expect(await screen.findByText("Refreshed volume")).toBeVisible();
  await act(async () => finish({...empty(), volumes: [volume("Late volume")], partial: false}));
  expect(screen.queryByText("Late volume")).not.toBeInTheDocument();
  expect(api.storageDashboard).toHaveBeenCalledTimes(3);
});

it("keeps unavailable saved queue state visible without treating it as success", async () => {
  const data = firstPage();
  api.storageDashboard.mockResolvedValue({...data, partial: false, incidents: [{id: "incident", source_id: "source", volume_key: "First volume",
    state: "warning", opened_at: "2026-11-09T00:00:00Z", resolved_at: null, chat_session_id: "chat", dispatch_status: "queued", dispatch_error: null, callback_error: null}],
    dispatches: [{id: "dispatch", incident_id: "incident", status: "queued", error: null, queue_status: "unavailable", queue_detail: "The linked chat belongs to another project."}]});
  mount();
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Saved queue status is unavailable"));
  expect(api.retryStorageDispatch).not.toHaveBeenCalled();
});
