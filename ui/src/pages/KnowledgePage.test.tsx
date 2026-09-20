import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeSource } from "../api/types";
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
  knowledgeSources: [] as KnowledgeSource[],
  reindexKnowledgeSource: vi.fn(),
  removeKnowledgeSource: vi.fn(),
}));

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => workspace,
}));
vi.mock("../components/ResourceRelationsPanel", () => ({ ResourceRelationsPanel: () => null }));

const chrome: ChromeContextValue = {
  activityOpen: false,
  paletteOpen: false,
  settingLensOpen: false,
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

const handbook: KnowledgeSource = {
  id: "source-1",
  engagementId: "project-1",
  name: "Handbook",
  sourceType: "document",
  status: "indexed",
  citation: "Handbook",
  documentCount: 3,
  createdAt: "2026-09-20T09:00:00Z",
  updatedAt: "2026-09-20T09:00:00Z",
  metadata: {},
};

function renderInspector() {
  return render(
    <MemoryRouter initialEntries={["/projects/project-1/sources/source-1"]}>
      <ChromeProvider value={chrome}>
        <DialogProvider>
          <Routes><Route path="/projects/:projectId/sources/:resourceId?" element={<KnowledgePage />} /></Routes>
        </DialogProvider>
      </ChromeProvider>
    </MemoryRouter>,
  );
}

describe("KnowledgePage inspector", () => {
  beforeEach(() => {
    workspace.api.getKnowledgeIndexStatus.mockReset();
    workspace.api.getKnowledgeIndexStatus.mockResolvedValue({ state: "ready", downloadedBytes: 0, totalBytes: 0 });
    workspace.reindexKnowledgeSource.mockReset();
    workspace.removeKnowledgeSource.mockReset();
    workspace.knowledgeSources = [handbook];
  });
  afterEach(() => {
    workspace.knowledgeSources = [];
  });

  it("presents the reindexed source and closes once the source is removed", async () => {
    const user = userEvent.setup();
    workspace.reindexKnowledgeSource.mockImplementation(async () => {
      workspace.knowledgeSources = [{ ...handbook, documentCount: 9, updatedAt: "2026-09-20T11:00:00Z" }];
    });
    workspace.removeKnowledgeSource.mockImplementation(async () => {
      workspace.knowledgeSources = [];
    });
    renderInspector();

    const inspector = screen.getByRole("complementary", { name: "Handbook" });
    expect(within(inspector).getByText("3")).toBeVisible();
    await user.click(within(inspector).getByRole("button", { name: "Reindex source" }));
    expect(await within(inspector).findByText("9")).toBeVisible();
    expect(await screen.findByText("Handbook was reindexed.")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Remove Handbook" })).toBeEnabled());

    await user.click(screen.getByRole("button", { name: "Remove Handbook" }));
    await user.click(screen.getByRole("button", { name: "Remove source" }));
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "Handbook" })).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
