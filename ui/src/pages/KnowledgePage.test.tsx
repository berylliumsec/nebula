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

const MiB = 1024 * 1024;
const retrievalReady = { backend: "chromadb", state: "ready", model: "all-MiniLM-L6-v2", downloadedBytes: 80 * MiB, totalBytes: 80 * MiB };
const relevanceModel = { model: "mixedbread-ai/mxbai-rerank-xsmall-v1", totalBytes: 91.5 * MiB };

describe("KnowledgePage model banner", () => {
  beforeEach(() => {
    workspace.api.getKnowledgeIndexStatus.mockReset();
    workspace.knowledgeSources = [];
  });

  it("follows the relevance model's download in the banner until it is ready", async () => {
    workspace.api.getKnowledgeIndexStatus
      .mockResolvedValueOnce({ ...retrievalReady, reranker: { ...relevanceModel, state: "downloading", downloadedBytes: 22.875 * MiB } })
      .mockResolvedValueOnce({ ...retrievalReady, reranker: { ...relevanceModel, state: "preparing", downloadedBytes: 91.5 * MiB } })
      .mockResolvedValue({ ...retrievalReady, reranker: { ...relevanceModel, state: "ready", downloadedBytes: 91.5 * MiB } });
    renderPage();

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Downloading the local relevance model");
    expect(banner).toHaveTextContent("22.9 MiB of 91.5 MiB downloaded · 25%");
    const progress = within(banner).getByRole("progressbar", { name: "Relevance model download" });
    expect(progress).toHaveAttribute("aria-valuenow", String(22.875 * MiB));
    expect(progress).toHaveAttribute("aria-valuemax", String(91.5 * MiB));

    // No operation is running: the page keeps following the download itself.
    expect(await screen.findByText("Preparing the local relevance model", {}, { timeout: 3000 })).toBeVisible();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/local relevance model/)).not.toBeInTheDocument(), { timeout: 3000 });
    expect(workspace.api.getKnowledgeIndexStatus).toHaveBeenCalledTimes(3);
  });

  it("explains that knowledge still works when the relevance model fails, and when it retries", async () => {
    workspace.api.getKnowledgeIndexStatus.mockResolvedValue({
      ...retrievalReady,
      reranker: { ...relevanceModel, state: "error", downloadedBytes: 2 * MiB, detail: "the relevance model download failed" },
    });
    renderPage();

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Relevance check unavailable");
    expect(banner).toHaveTextContent("The relevance model download failed. Knowledge still works: chats attach the nearest sources without the relevance check. Nebula tries again when knowledge is next used, 10 minutes after this failure.");
    expect(within(banner).queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("shows the retrieval model first and nothing once both models are ready", async () => {
    workspace.api.getKnowledgeIndexStatus.mockResolvedValueOnce({
      ...retrievalReady,
      state: "downloading",
      downloadedBytes: 40 * MiB,
      reranker: { ...relevanceModel, state: "required", downloadedBytes: 0 },
    });
    const { unmount } = renderPage();
    expect(await screen.findByRole("progressbar", { name: "Embedding model download" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Downloading the local retrieval model");
    expect(screen.queryByText(/relevance model/)).not.toBeInTheDocument();
    unmount();

    workspace.api.getKnowledgeIndexStatus.mockReset();
    workspace.api.getKnowledgeIndexStatus.mockResolvedValue({ ...retrievalReady, reranker: { ...relevanceModel, state: "ready", downloadedBytes: 91.5 * MiB } });
    renderPage();
    await waitFor(() => expect(workspace.api.getKnowledgeIndexStatus).toHaveBeenCalled());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
