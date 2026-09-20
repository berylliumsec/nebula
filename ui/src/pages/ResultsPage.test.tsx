import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { StructuredResultRecord, StructuredResultSummary } from "../api/types";
import { DialogProvider } from "../components/DialogSystem";
import { ResultsPage } from "./ResultsPage";

const { workspace } = vi.hoisted(() => ({
  workspace: {
    api: {
      listStructuredResults: vi.fn(),
      getStructuredResult: vi.fn(),
      deleteStructuredResult: vi.fn(),
    },
    coreState: "online",
    engagement: { id: "project-1", name: "Scratch Project", status: "active" },
  },
}));

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
vi.mock("../state/ChromeContext", () => ({ useChrome: () => ({ toolbarHost: null, trailingToolbarHost: null }) }));
vi.mock("../diagnostics", () => ({
  logCaughtDiagnostic: vi.fn(),
  DiagnosticErrorNotice: ({ title, error }: { title: string; error: string }) => <div role="alert">{title}: {error}</div>,
}));

function summary(overrides: Partial<StructuredResultSummary>): StructuredResultSummary {
  return {
    id: "result-1",
    projectId: "project-1",
    title: "A result",
    summary: "",
    producer: "dashboard.publish",
    origin: "agent",
    labels: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    revision: 1,
    stats: { byteSize: 20, nodeCount: 3, maxDepth: 1, rootType: "object", topLevelCount: 2 },
    hasHints: false,
    preview: [],
    sequence: 1,
    ...overrides,
  };
}

function record(overrides: Partial<StructuredResultRecord>): StructuredResultRecord {
  return { ...summary({}), result: { name: "edge-01" }, ...overrides };
}

let location = "";

function Harness({ entry }: { entry: string }) {
  return <MemoryRouter initialEntries={[entry]}>
    <DialogProvider>
      <Routes>
        <Route path="/projects/:projectId/results/:resourceId?" element={<><ResultsPage /><Probe /></>} />
      </Routes>
    </DialogProvider>
  </MemoryRouter>;
}

function Probe() {
  const current = useLocation();
  location = `${current.pathname}${current.search}`;
  return null;
}

describe("the project's published results", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.api.listStructuredResults.mockResolvedValue([]);
    workspace.api.getStructuredResult.mockResolvedValue(record({}));
  });

  it("explains how anything gets published when nothing has been", async () => {
    render(<Harness entry="/projects/project-1/results" />);
    expect(await screen.findByText(/Nothing published yet/)).toBeInTheDocument();
    // Both ways in: a running goal, and any client over the API.
    expect(screen.getByText(/A running goal publishes here/)).toBeInTheDocument();
    expect(screen.getByText(/structured-results/)).toBeInTheDocument();
  });

  it("keeps the snapshots of one piece of work together, newest step first", async () => {
    workspace.api.listStructuredResults.mockResolvedValue([
      summary({ id: "b", title: "Mapped the call graph", stream: "goal-1", streamLabel: "refactor-auth", sequence: 2 }),
      summary({ id: "a", title: "Read the handler", stream: "goal-1", streamLabel: "refactor-auth", sequence: 1 }),
      summary({ id: "c", title: "Standalone scan", sequence: 1 }),
    ]);
    render(<Harness entry="/projects/project-1/results" />);

    const series = await screen.findByRole("region", { name: "Stream refactor-auth" });
    const steps = within(series).getAllByRole("listitem");
    expect(steps).toHaveLength(2);
    expect(steps[0]).toHaveTextContent("2. Mapped the call graph");
    expect(steps[1]).toHaveTextContent("1. Read the handler");
    expect(screen.getByRole("region", { name: "Standalone scan" })).toBeInTheDocument();
  });

  it("opens a result into the explorer and keeps the chosen view in the URL", async () => {
    const user = userEvent.setup();
    workspace.api.listStructuredResults.mockResolvedValue([summary({ id: "result-1", title: "Host inventory" })]);
    workspace.api.getStructuredResult.mockResolvedValue(record({ id: "result-1", title: "Host inventory", result: { hosts: [{ ip: "10.0.0.1" }] } }));
    render(<Harness entry="/projects/project-1/results/result-1" />);

    expect(await screen.findByRole("heading", { name: "Host inventory", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getByRole("tab", { name: "Table" }));
    expect(location).toContain("view=table");
    expect(await screen.findByRole("columnheader", { name: /^ip/ })).toBeInTheDocument();
  });

  it("says so plainly when a link points at a result that is gone", async () => {
    workspace.api.getStructuredResult.mockRejectedValue(new Error("structured_results entity not found: ghost"));
    render(<Harness entry="/projects/project-1/results/ghost" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("structured_results entity not found");
  });

  it("asks before deleting a published result", async () => {
    const user = userEvent.setup();
    workspace.api.listStructuredResults.mockResolvedValue([summary({ id: "result-1", title: "Host inventory" })]);
    workspace.api.getStructuredResult.mockResolvedValue(record({ id: "result-1", title: "Host inventory" }));
    workspace.api.deleteStructuredResult.mockResolvedValue(undefined);
    render(<Harness entry="/projects/project-1/results/result-1" />);

    await user.click(await screen.findByRole("button", { name: "Delete Host inventory" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete result" }));
    expect(workspace.api.deleteStructuredResult).toHaveBeenCalledWith("project-1", "result-1");
  });
});
