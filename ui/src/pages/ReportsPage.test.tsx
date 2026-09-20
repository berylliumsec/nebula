import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReportSummary } from "../api/types";
import { DialogProvider } from "../components/DialogSystem";
import { ReportsPage } from "./ReportsPage";

const { workspace } = vi.hoisted(() => ({
  workspace: {
    activeOperator: undefined,
    api: undefined,
    createOperatorProfile: vi.fn(),
    createReport: vi.fn(),
    engagement: { id: "project-1", name: "Scratch Project", status: "active" },
    findings: [] as never[],
    harnesses: [] as never[],
    observations: [] as never[],
    providers: [] as never[],
    reports: [] as ReportSummary[],
    signOffReport: vi.fn(),
    updateReport: vi.fn(),
  },
}));

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
vi.mock("../components/ResourceRelationsPanel", () => ({ ResourceRelationsPanel: () => null }));
vi.mock("../state/ChromeContext", () => ({ useChrome: () => ({ toolbarHost: null, trailingToolbarHost: null }) }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({ error }: { error: string }) => <div role="alert">{error}</div> }));

function report(id: string, title: string, overrides: Partial<ReportSummary> = {}): ReportSummary {
  return {
    id,
    engagementId: "project-1",
    title,
    status: "draft",
    executiveSummary: "",
    findingIds: [],
    observationIds: [],
    noteTransforms: [],
    artifactIds: [],
    createdAt: "2026-09-20T09:00:00Z",
    updatedAt: "2026-09-20T09:00:00Z",
    revision: 1,
    ...overrides,
  };
}

function NavigateButton({ to }: { to: string }) {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(to)}>Navigate to {to}</button>;
}

function RouterPath() {
  return <output aria-label="Router path">{useLocation().pathname}</output>;
}

function renderReports(route: string) {
  return render(<MemoryRouter initialEntries={[route]}><DialogProvider>
    <Routes><Route path="/projects/:projectId/reports/:resourceId?" element={<>
      <ReportsPage />
      <NavigateButton to="/projects/project-1/reports/report-2" />
      <RouterPath />
    </>} /></Routes>
  </DialogProvider></MemoryRouter>);
}

describe("report editor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspace.reports = [report("report-1", "Perimeter assessment")];
  });

  it("keeps keystrokes typed while a save is in flight and leaves them unsaved", async () => {
    let resolve!: (value: ReportSummary) => void;
    workspace.updateReport.mockImplementation(() => new Promise<ReportSummary>((resolveSave) => { resolve = resolveSave; }));
    const user = userEvent.setup();
    renderReports("/projects/project-1/reports/report-1");

    const summary = screen.getByPlaceholderText(/Summarize scope/);
    await user.type(summary, "Perimeter is exposed");
    await user.click(screen.getByRole("button", { name: "Save report" }));
    expect(workspace.updateReport).toHaveBeenCalledWith("report-1", expect.objectContaining({ executiveSummary: "Perimeter is exposed", expectedRevision: 1 }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();

    await user.type(summary, " on port 443");
    const saved = report("report-1", "Perimeter assessment", { executiveSummary: "Perimeter is exposed", revision: 2 });
    workspace.reports = [saved];
    resolve(saved);

    await waitFor(() => expect(screen.getByRole("button", { name: "Save report" })).toBeEnabled());
    expect(summary).toHaveValue("Perimeter is exposed on port 443");
    expect(screen.getByText("Unsaved changes")).toBeVisible();
  });

  it("asks before a route change discards unsaved edits, like the in-page list does", async () => {
    workspace.reports = [report("report-1", "Perimeter assessment"), report("report-2", "Internal assessment")];
    const user = userEvent.setup();
    renderReports("/projects/project-1/reports/report-1");

    await user.type(screen.getByRole("textbox", { name: "Report title" }), " v2");
    await user.click(screen.getByRole("button", { name: "Navigate to /projects/project-1/reports/report-2" }));

    const prompt = await screen.findByRole("dialog", { name: "Discard unsaved changes?" });
    await user.click(within(prompt).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.getByLabelText("Router path")).toHaveTextContent("/projects/project-1/reports/report-1"));
    expect(screen.getByRole("textbox", { name: "Report title" })).toHaveValue("Perimeter assessment v2");

    await user.click(screen.getByRole("button", { name: "Navigate to /projects/project-1/reports/report-2" }));
    const secondPrompt = await screen.findByRole("dialog", { name: "Discard unsaved changes?" });
    await user.click(within(secondPrompt).getByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Report title" })).toHaveValue("Internal assessment"));
    expect(screen.getByLabelText("Router path")).toHaveTextContent("/projects/project-1/reports/report-2");
  });
});
