import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DialogProvider } from "../components/DialogSystem";
import { ChromeProvider, type ChromeContextValue } from "../state/ChromeContext";
import { KnowledgePage } from "./KnowledgePage";

const workspace = vi.hoisted(() => ({
  api: {
    getKnowledgeIndexStatus: vi.fn(),
  },
  coreState: "online" as const,
  engagement: { id: "project-1", name: "Research" },
  ingestKnowledgeSource: vi.fn(),
  ingestKnowledgeUrlSource: vi.fn(),
  knowledgeSources: [],
  reindexKnowledgeSource: vi.fn(),
  removeKnowledgeSource: vi.fn(),
}));

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => workspace,
}));

const chrome: ChromeContextValue = {
  activityOpen: false,
  paletteOpen: false,
  sidebarCollapsed: true,
  toolbarHost: null,
  openPalette: () => undefined,
  setActivityOpen: () => undefined,
  setPaletteOpen: () => undefined,
  setToolbarHost: () => undefined,
  toggleActivity: () => undefined,
  toggleSidebar: () => undefined,
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ChromeProvider value={chrome}>
        <DialogProvider>
          <KnowledgePage />
        </DialogProvider>
      </ChromeProvider>
    </MemoryRouter>,
  );
}

describe("KnowledgePage URL ingestion", () => {
  beforeEach(() => {
    workspace.api.getKnowledgeIndexStatus.mockReset();
    workspace.api.getKnowledgeIndexStatus.mockResolvedValue({
      state: "ready",
      downloadedBytes: 0,
      totalBytes: 0,
    });
    workspace.ingestKnowledgeUrlSource.mockReset();
  });

  it("shows URL rendering failures inside the open Add URL dialog", async () => {
    const user = userEvent.setup();
    workspace.ingestKnowledgeUrlSource.mockRejectedValue(
      new Error("URL page rendering requires Chromium; install Playwright Chromium or a system Chrome/Chromium browser"),
    );
    renderPage();

    await user.click(screen.getByRole("button", { name: "Add URL" }));
    const dialog = screen.getByRole("dialog", { name: "Add source from URL" });
    await user.type(within(dialog).getByLabelText("URL"), "https://docs.example.com/guide");
    await user.click(within(dialog).getByRole("button", { name: "Add URL source" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("URL page rendering requires Chromium");
    expect(screen.getByRole("dialog", { name: "Add source from URL" })).toBeVisible();

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add source from URL" })).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
