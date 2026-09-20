import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DialogProvider } from "../components/DialogSystem";
import { FindingsPage } from "./FindingsPage";

const { workspace } = vi.hoisted(() => ({
  workspace: {
    assets: [] as never[],
    createFinding: vi.fn(),
    engagement: { id: "project-1", name: "Scratch Project", status: "active" },
    evidence: [] as never[],
    findings: [] as never[],
    reports: [] as never[],
    retryResource: vi.fn(),
    updateFinding: vi.fn(),
    updateReport: vi.fn(),
  },
}));

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
vi.mock("../state/WorkbenchDraftContext", () => ({ useWorkbenchDrafts: () => ({ clearFindingDraft: vi.fn(), findingDraft: undefined, requestNebulaDraft: vi.fn() }) }));
vi.mock("../components/ResourceRelationsPanel", () => ({ ResourceRelationsPanel: () => null }));
vi.mock("../state/ChromeContext", () => ({ useChrome: () => ({ toolbarHost: null, trailingToolbarHost: null }) }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({ error }: { error: string }) => <div role="alert">{error}</div> }));

describe("candidate finding dialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("stays open while the finding is being created and shows the failure inside", async () => {
    let reject!: (error: Error) => void;
    workspace.createFinding.mockImplementation(() => new Promise((_resolve, rejectCreate) => { reject = rejectCreate; }));
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/projects/project-1/findings"]}><DialogProvider><FindingsPage /></DialogProvider></MemoryRouter>);

    await user.click(screen.getByRole("button", { name: "New finding" }));
    const dialog = screen.getByRole("dialog", { name: "Create candidate finding" });
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Exposed admin panel");
    await user.click(within(dialog).getByRole("button", { name: "Create candidate" }));

    expect(within(dialog).getByRole("button", { name: "Creating…" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Close candidate finding dialog" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Create candidate finding" })).toBe(dialog);

    reject(new Error("Core rejected the finding."));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Core rejected the finding.");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(workspace.createFinding).toHaveBeenCalledTimes(1);
  });
});
