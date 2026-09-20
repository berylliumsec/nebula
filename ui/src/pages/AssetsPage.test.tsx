import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DialogProvider } from "../components/DialogSystem";
import { AssetsPage } from "./AssetsPage";

const { workspace } = vi.hoisted(() => ({
  workspace: {
    addAsset: vi.fn(),
    assets: [] as never[],
    engagement: { id: "project-1", name: "Scratch Project", status: "active" },
    findings: [] as never[],
  },
}));

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => workspace }));
vi.mock("../components/ResourceRelationsPanel", () => ({ ResourceRelationsPanel: () => null }));
vi.mock("../state/ChromeContext", () => ({ useChrome: () => ({ toolbarHost: null, trailingToolbarHost: null }) }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn(), DiagnosticErrorNotice: ({ error }: { error: string }) => <div role="alert">{error}</div> }));

describe("add asset dialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("stays open while the asset is being added and shows the failure inside", async () => {
    let reject!: (error: Error) => void;
    workspace.addAsset.mockImplementation(() => new Promise((_resolve, rejectAdd) => { reject = rejectAdd; }));
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/projects/project-1/assets"]}><DialogProvider><AssetsPage /></DialogProvider></MemoryRouter>);

    await user.click(screen.getByRole("button", { name: "Add asset" }));
    const dialog = screen.getByRole("dialog", { name: "Add asset" });
    await user.type(within(dialog).getByRole("textbox", { name: "Name" }), "gateway.internal");
    await user.click(within(dialog).getByRole("button", { name: "Add asset" }));

    expect(within(dialog).getByRole("button", { name: "Adding…" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Close asset dialog" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Add asset" })).toBe(dialog);

    reject(new Error("Core rejected the asset."));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Core rejected the asset.");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeEnabled();
    expect(workspace.addAsset).toHaveBeenCalledTimes(1);
  });
});
