import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { StructuredResultRecord, StructuredResultSummary } from "../../api/types";
import { ChatResultStream } from "./ChatResultStream";

vi.mock("../../diagnostics", () => ({
  logCaughtDiagnostic: vi.fn(),
  DiagnosticErrorNotice: ({ title, error }: { title: string; error: string }) => <div role="alert">{title}: {error}</div>,
}));

const listStructuredResults = vi.fn();
const getStructuredResult = vi.fn();
const api = { listStructuredResults, getStructuredResult } as unknown as ApiClient;

function summary(overrides: Partial<StructuredResultSummary>): StructuredResultSummary {
  return {
    id: "snapshot-1",
    projectId: "project-1",
    title: "Snapshot",
    summary: "",
    producer: "dashboard.publish",
    origin: "agent",
    labels: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    revision: 1,
    stats: { byteSize: 40, nodeCount: 4, maxDepth: 2, rootType: "object", topLevelCount: 2 },
    hasHints: false,
    preview: [],
    stream: "goal-1",
    streamLabel: "refactor-auth",
    sequence: 1,
    chatSessionId: "session-1",
    ...overrides,
  };
}

function record(overrides: Partial<StructuredResultRecord>): StructuredResultRecord {
  return { ...summary({}), result: { language: "python", code: "def login():\n    return None" }, ...overrides };
}

const show = () => render(<MemoryRouter><ChatResultStream api={api} projectId="project-1" sessionId="session-1" /></MemoryRouter>);

describe("what the assistant published beside the conversation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listStructuredResults.mockResolvedValue([]);
    getStructuredResult.mockResolvedValue(record({}));
  });

  it("asks Core only for this conversation's results", async () => {
    show();
    await screen.findByText(/Nothing published yet/);
    expect(listStructuredResults).toHaveBeenCalledWith(
      "project-1",
      expect.objectContaining({ chatSessionId: "session-1" }),
      expect.anything(),
    );
  });

  it("opens on the newest snapshot and explores it in place", async () => {
    listStructuredResults.mockResolvedValue([
      summary({ id: "step-2", title: "Mapped the call graph", sequence: 2 }),
      summary({ id: "step-1", title: "Read the handler", sequence: 1 }),
    ]);
    getStructuredResult.mockResolvedValue(record({ id: "step-2", title: "Mapped the call graph" }));
    show();

    expect(await screen.findByText("Showing the newest snapshot. Choose an earlier step above to stay on it.")).toBeInTheDocument();
    expect(getStructuredResult).toHaveBeenCalledWith("project-1", "step-2", expect.anything());
    // Multi-line text reads as a block, with the sibling language named.
    const block = (await screen.findByText(/def login/)).closest(".structured-block");
    expect(block).not.toBeNull();
    expect(within(block as HTMLElement).getByText("python")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Tree" })).toBeInTheDocument();
  });

  it("stays on an earlier step once the operator chooses one", async () => {
    const user = userEvent.setup();
    listStructuredResults.mockResolvedValue([
      summary({ id: "step-2", title: "Mapped the call graph", sequence: 2 }),
      summary({ id: "step-1", title: "Read the handler", sequence: 1 }),
    ]);
    show();

    const series = await screen.findByRole("region", { name: "Stream refactor-auth" });
    await user.click(within(series).getByRole("button", { name: /Read the handler/ }));
    expect(getStructuredResult).toHaveBeenLastCalledWith("project-1", "step-1", expect.anything());
    expect(screen.queryByText(/Showing the newest snapshot/)).not.toBeInTheDocument();
  });

  it("offers the full surface without making it the only way in", async () => {
    listStructuredResults.mockResolvedValue([summary({})]);
    show();
    expect(await screen.findByRole("link", { name: /Open in Results/ })).toHaveAttribute(
      "href",
      "/projects/project-1/results/snapshot-1",
    );
  });

  it("reports a read failure without blanking what it already showed", async () => {
    listStructuredResults.mockRejectedValue(new Error("Core is offline"));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Core is offline");
  });
});
