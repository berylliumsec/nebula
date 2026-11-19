import {act, render, screen} from "@testing-library/react";
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {GIB, type HostCommandItem, type HostCommandView} from "../api/memoryBudgets";
import {HOST_COMMAND_POLL_MS, HostCommandStatus, mayBeHostCommand} from "./HostCommandStatus";

const NOW = Date.parse("2026-11-19T12:00:00Z");
const item = (overrides: Partial<HostCommandItem> = {}): HostCommandItem => ({
  job_id: "command-exec-1", execution_id: "exec-1", project_id: "project", state: "queued", detached: false, cancel_requested: false,
  position: 3, command: "make test", cwd: "/work", owner_kind: "chat", tool_call_id: "call-1", chat_session_id: "chat", background: false,
  reserved_bytes: 2 * GIB, limit_bytes: 2 * GIB, queued_at: new Date(NOW - 42_000).toISOString(), started_at: new Date(NOW - 75_000).toISOString(), updated_at: new Date(NOW - 75_000).toISOString(),
  ...overrides,
});
const view = (entry?: HostCommandItem): HostCommandView => ({
  settings: {revision: 1, running_at_once: 8}, running_count: entry && entry.state !== "queued" ? 1 : 0,
  running: entry && entry.state !== "queued" ? [entry] : [], queued: entry?.state === "queued" ? [entry] : [],
});
function fixture(responses: (HostCommandItem | undefined)[]) {
  let index = 0;
  const request = vi.fn(async (_path: string, options?: RequestInit) => {
    if (options?.method === "POST") return item({cancel_requested: true});
    const next = responses[Math.min(index, responses.length - 1)]; index++;
    return view(next);
  });
  return {api: {request} as unknown as ApiClient, request};
}
const flush = () => act(async () => {await Promise.resolve(); await Promise.resolve();});
const reads = (request: ReturnType<typeof vi.fn>) => request.mock.calls.filter(([, options]) => !options?.method).length;

describe("HostCommandStatus", () => {
  beforeEach(() => {vi.useFakeTimers({shouldAdvanceTime: false}); vi.setSystemTime(NOW);});
  afterEach(() => {vi.useRealTimers();});

  it("shows the place in line and waited time, and cancels a queued command", async () => {
    const {api, request} = fixture([item()]);
    render(<HostCommandStatus api={api} toolCallId="call-1" />);
    await flush();
    expect(request.mock.calls[0][0]).toBe("memory-budgets/host-commands?tool_call_id=call-1");
    expect(screen.getByText("Waiting for Host capacity · 3rd in line · 42s")).toBeVisible();
    act(() => {screen.getByRole("button", {name: "Cancel queued command"}).click();});
    await flush();
    const post = request.mock.calls.find(([, options]) => options?.method === "POST");
    expect(post?.[0]).toBe("memory-budgets/host-commands/command-exec-1/cancel");
  });

  it("shows running elapsed time and memory with a stop control", async () => {
    const {api, request} = fixture([item({state: "running", position: null})]);
    render(<HostCommandStatus api={api} toolCallId="call-1" />);
    await flush();
    expect(screen.getByText("Running · 1m 15s · 2 GiB")).toBeVisible();
    act(() => {screen.getByRole("button", {name: "Stop command"}).click();});
    await flush();
    expect(request.mock.calls.some(([path, options]) => options?.method === "POST" && path === "memory-budgets/host-commands/command-exec-1/cancel")).toBe(true);
  });

  it("explains a detached process that still holds capacity", async () => {
    const {api} = fixture([item({state: "running", detached: true, position: null})]);
    render(<HostCommandStatus api={api} toolCallId="call-1" />);
    await flush();
    expect(screen.getByText("Exited · detached process still running · 2 GiB")).toBeVisible();
    expect(screen.getByText("Its capacity stays held until the detached process exits or you stop it.")).toBeVisible();
    expect(screen.getByRole("button", {name: "Stop command"})).toBeEnabled();
  });

  it("stops polling once the command leaves the queue", async () => {
    const {api, request} = fixture([item(), undefined]);
    render(<HostCommandStatus api={api} toolCallId="call-1" />);
    await flush();
    expect(screen.getByText(/Waiting for Host capacity/)).toBeVisible();
    await act(async () => {vi.advanceTimersByTime(HOST_COMMAND_POLL_MS);}); await flush();
    expect(screen.queryByText(/Waiting for Host capacity/)).not.toBeInTheDocument();
    const count = reads(request);
    await act(async () => {vi.advanceTimersByTime(HOST_COMMAND_POLL_MS * 5);}); await flush();
    expect(reads(request)).toBe(count);
  });

  it("gives up quietly when the call is not a Host command", async () => {
    const {api, request} = fixture([undefined]);
    const {container} = render(<HostCommandStatus api={api} toolCallId="call-1" />);
    for (let tick = 0; tick < 6; tick++) {await act(async () => {vi.advanceTimersByTime(HOST_COMMAND_POLL_MS);}); await flush();}
    expect(reads(request)).toBe(3);
    expect(container).toBeEmptyDOMElement();
  });

  it("only considers active Host run_command calls", () => {
    expect(mayBeHostCommand({capability: "run_command", status: "running"})).toBe(true);
    expect(mayBeHostCommand({capability: "run_command", status: "queued", receipt: {execution_mode: "host"}})).toBe(true);
    expect(mayBeHostCommand({capability: "run_command", status: "running", receipt: {execution_mode: "container"}})).toBe(false);
    expect(mayBeHostCommand({capability: "run_command", status: "completed"})).toBe(false);
    expect(mayBeHostCommand({capability: "web.search", status: "running"})).toBe(false);
  });
});
