import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {MemoryRouter} from "react-router-dom";
import {describe, expect, it, vi} from "vitest";
import {ApiError, type ApiClient} from "../api/client";
import {GIB, MEMORY_BUDGET_CHANGED, type HostCommandItem, type HostCommandView} from "../api/memoryBudgets";
import {HostCommandSettings} from "./HostCommandSettings";

const at = (secondsAgo: number) => new Date(Date.now() - secondsAgo * 1000).toISOString();
const item = (overrides: Partial<HostCommandItem>): HostCommandItem => ({
  job_id: "command-a", execution_id: "a", project_id: "p1", state: "running", detached: false, cancel_requested: false, position: null,
  command: "pytest -q tests/very/long/path", cwd: "/work", owner_kind: "chat", tool_call_id: "call-a", chat_session_id: "chat-a", background: false,
  reserved_bytes: 2 * GIB, limit_bytes: 2 * GIB, queued_at: at(120), started_at: at(65), updated_at: at(65), ...overrides,
});
function fixture(options: {conflict?: boolean} = {}) {
  const state: HostCommandView = {
    settings: {revision: 4, running_at_once: 2}, running_count: 2,
    running: [item({}), item({job_id: "command-b", execution_id: "b", project_id: "p2", command: "nohup ./serve &", detached: true, chat_session_id: "chat-b", tool_call_id: "call-b"})],
    queued: [item({job_id: "command-c", execution_id: "c", state: "queued", position: 1, command: "make build", queued_at: at(30), chat_session_id: "chat-c", tool_call_id: "call-c"})],
  };
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body));
      if (options.conflict || body.expected_revision !== state.settings.revision) throw new ApiError("Host command settings changed; reload", 409);
      state.settings = {revision: state.settings.revision + 1, running_at_once: body.running_at_once};
      return structuredClone(state.settings);
    }
    if (init?.method === "POST") {
      const id = decodeURIComponent(path.split("/")[2]);
      const found = [...state.running, ...state.queued].find(entry => entry.job_id === id)!;
      found.cancel_requested = true;
      return structuredClone(found);
    }
    return structuredClone(state);
  });
  return {api: {request} as unknown as ApiClient, request, state};
}
const projects = [{id: "p1", name: "Apple research"}, {id: "p2", name: "Firmware"}];
const renderSection = (api: ApiClient) => render(<MemoryRouter><HostCommandSettings api={api} projects={projects} /></MemoryRouter>);

describe("Host command settings", () => {
  it("lists running, detached and queued commands across projects with chat links", async () => {
    const {api} = fixture();
    renderSection(api);
    expect(await screen.findByText("2 of 2 running · 1 queued · 4 GiB reserved")).toBeVisible();
    expect(screen.getByText(/Commands from every project share one queue/)).toBeVisible();
    const running = screen.getByRole("list", {name: "Running Host commands"});
    expect(within(running).getByText("pytest -q tests/very/long/path")).toBeVisible();
    expect(within(running).getByText("Apple research")).toBeVisible();
    expect(within(running).getByText("Detached")).toBeVisible();
    expect(within(running).getAllByText(/1m 5s · 2 GiB/)[0]).toBeVisible();
    expect(within(running).getAllByRole("link", {name: "Chat"})[0]).toHaveAttribute("href", "/projects/p1/workbench?view=chat&session=chat-a");
    const queued = screen.getByRole("list", {name: "Queued Host commands"});
    expect(within(queued).getByText("make build")).toBeVisible();
    expect(within(queued).getByText(/Waiting 30s/)).toBeVisible();
    expect(within(queued).getByLabelText("Position 1")).toBeVisible();
  });

  it("saves running at once with the revision and confirms only after read-back", async () => {
    const user = userEvent.setup();
    const {api, request} = fixture();
    const changed = vi.fn(); window.addEventListener(MEMORY_BUDGET_CHANGED, changed);
    renderSection(api);
    const field = await screen.findByLabelText("Running at once");
    expect(field).toHaveAccessibleDescription("Commands beyond this wait in line; projects with fewer running commands go first.");
    await user.clear(field); await user.type(field, "6");
    await user.click(screen.getByRole("button", {name: "Save"}));
    expect(await screen.findByText("Saved and read back. Up to 6 Host commands run at once.")).toBeVisible();
    const put = request.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(put[0]).toBe("memory-budgets/host-commands/settings");
    expect(JSON.parse(String(put[1]!.body))).toEqual({expected_revision: 4, running_at_once: 6});
    const putIndex = request.mock.calls.indexOf(put);
    expect(request.mock.calls.slice(putIndex + 1).some(([path, init]) => path === "memory-budgets/host-commands" && !init?.method)).toBe(true);
    expect(changed).toHaveBeenCalled();
    window.removeEventListener(MEMORY_BUDGET_CHANGED, changed);
  });

  it("reports a stale revision as a conflict and does not claim a save", async () => {
    const user = userEvent.setup();
    const {api} = fixture({conflict: true});
    renderSection(api);
    const field = await screen.findByLabelText("Running at once");
    await user.clear(field); await user.type(field, "3");
    await user.click(screen.getByRole("button", {name: "Save"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("Host command settings changed elsewhere. Reload before saving your change.");
    expect(screen.queryByText(/Saved and read back/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Save"})).toBeDisabled();
    expect(screen.getByRole("button", {name: "Reload"})).toBeVisible();
  });

  it("stops a running command and cancels a queued one through Core", async () => {
    const user = userEvent.setup();
    const {api, request} = fixture();
    renderSection(api);
    const running = await screen.findByRole("list", {name: "Running Host commands"});
    await user.click(within(running).getAllByRole("button", {name: "Stop command"})[0]);
    expect(request.mock.calls.some(([path, init]) => init?.method === "POST" && path === "memory-budgets/host-commands/command-a/cancel")).toBe(true);
    expect(await within(running).findByText("Stopping…")).toBeVisible();
    const queued = screen.getByRole("list", {name: "Queued Host commands"});
    await user.click(within(queued).getByRole("button", {name: "Cancel queued command"}));
    expect(request.mock.calls.some(([path, init]) => init?.method === "POST" && path === "memory-budgets/host-commands/command-c/cancel")).toBe(true);
  });
});
