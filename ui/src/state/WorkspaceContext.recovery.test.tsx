import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useLayoutEffect } from "react";
import { WorkspaceProvider, useWorkspace } from "./WorkspaceContext";

const fixture = vi.hoisted(() => ({ methods: {} as Record<string, ReturnType<typeof vi.fn>>, streams: [] as Array<any>, onCommit: undefined as (() => void) | undefined }));
vi.mock("../api/runtime", () => ({ resolveApiRuntime: async () => ({ state: "ready", baseUrl: "http://core/api/v1" }) }));
vi.mock("../api/client", () => ({ ApiClient: class {
  baseUrl = "http://core/api/v1";
  constructor() { Object.assign(this, fixture.methods); }
} }));
vi.mock("../api/events", () => ({ NebulaEventStream: class {
  options: any;
  constructor(options: any) { this.options = options; fixture.streams.push(this); }
  connect() { this.options.onStateChange("connecting"); }
  disconnect() { this.options.onStateChange("closed"); }
} }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn(), setDiagnosticsAvailability: vi.fn(), setBrowserDiagnosticIngress: vi.fn() }));

const run = (id = "run-1", completedTasks = 2) => ({ id, engagementId: "project", title: id, status: "running", completedTasks, totalTasks: 5, updatedAt: "2026-09-09T12:00:00Z" });
function Probe() {
  const value = useWorkspace();
  useLayoutEffect(() => { fixture.onCommit?.(); });
  return <><output data-testid="state">{JSON.stringify({
    workspace: value.workspaceState, status: value.resourceStatus,
    library: value.libraryItems, harnesses: value.harnesses,
    run: value.run, runs: value.runs, events: value.events,
  })}</output>{(["library", "harnesses", "activity"] as const).map((resource) =>
    <button key={resource} onClick={() => void value.retryResource(resource).catch(() => {})}>Retry {resource}</button>)}</>;
}
const state = () => JSON.parse(screen.getByTestId("state").textContent!);
const mount = async () => {
  render(<WorkspaceProvider><Probe /></WorkspaceProvider>);
  await waitFor(() => expect(["ready", "degraded"]).toContain(state().workspace));
};

beforeEach(() => {
  fixture.onCommit = undefined;
  fixture.streams.length = 0;
  localStorage.clear();
  window.history.replaceState({}, "", "/");
  localStorage.setItem("nebula.engagement", "project");
  fixture.methods = Object.fromEntries([
    "listApprovals", "listAssets", "listFindings", "listEvidence", "listObservations", "listKnowledgeSources", "listReports", "listLibraryItems", "listProviders",
  ].map((name) => [name, vi.fn().mockResolvedValue({ items: [] })]));
  Object.assign(fixture.methods, {
    getToken: vi.fn(), health: vi.fn().mockResolvedValue({ status: "ok" }),
    listEngagements: vi.fn().mockResolvedValue({ items: [{ id: "project", name: "Project", status: "active" }] }),
    listHarnesses: vi.fn().mockResolvedValue([]), listProviderCatalog: vi.fn().mockResolvedValue([]),
    listOperatorProfiles: vi.fn().mockResolvedValue([]),
    setupStatus: vi.fn().mockResolvedValue({ core: { status: "ready" }, terminal: { status: "ready" } }),
    listRuns: vi.fn().mockResolvedValue({ items: [run()] }),
  });
});

describe("workspace resource recovery", () => {
  it("does not let a queued catalog-health effect overwrite observed connection loss", async () => {
    await mount();
    fixture.methods.listLibraryItems.mockResolvedValue({items: [{id: "refreshed"}]});
    fixture.onCommit = () => {
      if (state().library[0]?.id !== "refreshed") return;
      fixture.onCommit = undefined;
      window.dispatchEvent(new Event("offline"));
    };
    fireEvent.click(screen.getByText("Retry library"));
    await waitFor(() => expect(state().library).toEqual([{id: "refreshed"}]));
    expect(state().workspace).toBe("failed");
    act(() => window.dispatchEvent(new Event("online")));
    await waitFor(() => expect(state().workspace).toBe("ready"));
  });

  it("reconnects after a browser network transition without discarding the selected workspace", async () => {
    window.history.replaceState({}, "", "/?mission=run-1");
    fixture.methods.listLibraryItems.mockResolvedValue({items: [{id: "retained"}]});
    await mount();
    act(() => window.dispatchEvent(new Event("offline")));
    await waitFor(() => expect(state().workspace).toBe("failed"));
    expect(state().library).toEqual([{id: "retained"}]);
    act(() => window.dispatchEvent(new Event("online")));
    await waitFor(() => expect(state().workspace).toBe("ready"));
    expect(state().run.id).toBe("run-1");
    expect(new URLSearchParams(location.search).get("mission")).toBe("run-1");
    expect(fixture.methods.health).toHaveBeenCalledTimes(3);
  });

  it("retries the global Library even without a selected project", async () => {
    fixture.methods.listEngagements.mockResolvedValue({ items: [] });
    fixture.methods.listLibraryItems.mockRejectedValueOnce(new Error("temporary"));
    await mount();
    expect(state().status.library.state).toBe("failed");
    fixture.methods.listLibraryItems.mockResolvedValue({ items: [{ id: "library-item" }] });
    fireEvent.click(screen.getByText("Retry library"));
    await waitFor(() => expect(state().library).toEqual([{ id: "library-item" }]));
    expect(fixture.methods.listLibraryItems).toHaveBeenCalledTimes(2);
    expect(state().status.library.state).toBe("ready");
    expect(state().workspace).toBe("ready");
  });

  it("exposes a failed harness catalog and restores its choices on retry", async () => {
    fixture.methods.listHarnesses.mockRejectedValueOnce(new Error("catalog unavailable"));
    await mount();
    expect(state().workspace).toBe("degraded");
    expect(state().status.harnesses.state).toBe("failed");
    fixture.methods.listHarnesses.mockResolvedValue([{ id: "harness" }]);
    fireEvent.click(screen.getByText("Retry harnesses"));
    await waitFor(() => expect(state().harnesses).toEqual([{ id: "harness" }]));
    expect(state().status.harnesses.state).toBe("ready");
    expect(state().workspace).toBe("ready");
  });

  it("restores mission history, URL selection, and the stream after activity fails", async () => {
    window.history.replaceState({}, "", "/?mission=run-1");
    fixture.methods.listRuns.mockRejectedValueOnce(new Error("unavailable"));
    await mount();
    expect(state().runs).toEqual([]);
    expect(fixture.streams).toHaveLength(0);
    fixture.methods.listRuns.mockResolvedValue({ items: [run(), run("run-2")] });
    fireEvent.click(screen.getByText("Retry activity"));
    await waitFor(() => expect(fixture.streams).toHaveLength(1));
    expect(state().runs).toHaveLength(2);
    expect(state().run.id).toBe("run-1");
    expect(fixture.streams[0].options.cursor.runId).toBe("run-1");
    expect(new URLSearchParams(location.search).get("mission")).toBe("run-1");
  });
});

describe("mission snapshot and replay integration", () => {
  const completion = { id: "event", runId: "run-1", kind: "task.completed", sequence: 3, occurredAt: "2026-09-09T11:00:00Z", payload: {} };
  it("keeps historical replay out of snapshot counters and refreshes on live progress", async () => {
    await mount();
    const stream = fixture.streams.at(-1)!;
    act(() => stream.options.onEvent(completion));
    expect(state().run.completedTasks).toBe(2);
    expect(state().run.updatedAt).toBe("2026-09-09T12:00:00Z");
    await act(async () => stream.options.onReplayComplete());
    expect(state().run.completedTasks).toBe(2);
    fixture.methods.listRuns.mockResolvedValue({ items: [run("run-1", 3)] });
    await act(async () => stream.options.onEvent({ ...completion, sequence: 4, kind: "run.progress" }));
    expect(state().run.completedTasks).toBe(3);
    expect(state().runs[0].completedTasks).toBe(3);
    act(() => stream.options.onStateChange("reconnecting"));
    act(() => stream.options.onEvent(completion));
    expect(state().run.completedTasks).toBe(3);
    await act(async () => stream.options.onReplayComplete());
    expect(state().run.completedTasks).toBe(3);
  });

  it("refetches when another event arrives during a snapshot request", async () => {
    await mount();
    const stream = fixture.streams.at(-1)!;
    let finish!: (value: unknown) => void;
    fixture.methods.listRuns.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    act(() => stream.options.onReplayComplete());
    act(() => stream.options.onEvent({ ...completion, kind: "run.progress" }));
    fixture.methods.listRuns.mockResolvedValue({ items: [run("run-1", 4)] });
    await act(async () => finish({ items: [run("run-1", 3)] }));
    await waitFor(() => expect(state().run.completedTasks).toBe(4));
  });
});
