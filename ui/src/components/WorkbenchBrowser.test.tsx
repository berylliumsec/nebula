import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { ChromeProvider, type ChromeContextValue } from "../state/ChromeContext";
import { DialogProvider } from "./DialogSystem";
import { WorkbenchBrowser } from "./WorkbenchBrowser";

const runtimeMocks = vi.hoisted(() => ({
  isTauriRuntime: vi.fn(),
}));

const browserMocks = vi.hoisted(() => ({
  bounds: vi.fn(),
  capabilities: vi.fn(),
  clear: vi.fn(),
  close: vi.fn(),
  control: vi.fn(),
  create: vi.fn(),
  discardDownload: vi.fn(),
  importDownload: vi.fn(),
  navigate: vi.fn(),
  visible: vi.fn(),
}));

type EventHandler = (event: { payload: unknown }) => void;
const eventMocks = vi.hoisted(() => ({
  handlers: new Map<string, EventHandler>(),
}));

vi.mock("../api/runtime", () => ({
  isTauriRuntime: runtimeMocks.isTauriRuntime,
}));

vi.mock("../api/workbenchBrowser", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/workbenchBrowser")>();
  return { ...actual, workbenchBrowser: browserMocks };
});

vi.mock("@tauri-apps/api/event", () => ({
  listen: (event: string, handler: EventHandler) => {
    eventMocks.handlers.set(event, handler);
    return Promise.resolve(() => eventMocks.handlers.delete(event));
  },
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

function renderBrowser(onAddKnowledgeUrl = vi.fn(async () => ({ id: "source-1", name: "Guide" }))) {
  return {
    onAddKnowledgeUrl,
    ...render(
      <MemoryRouter>
        <DialogProvider>
          <ChromeProvider value={chrome}>
            <WorkbenchBrowser
              active
              projectId="project-1"
              onAddKnowledgeUrl={onAddKnowledgeUrl}
              onOpenFiles={() => undefined}
            />
          </ChromeProvider>
        </DialogProvider>
      </MemoryRouter>,
    ),
  };
}

async function openPage(finalUrl = "https://docs.example.com/guide") {
  fireEvent.change(screen.getByLabelText("Start browsing"), { target: { value: "https://docs.example.com/start" } });
  fireEvent.click(screen.getByRole("button", { name: "Go" }));
  await waitFor(() => expect(browserMocks.create).toHaveBeenCalled());
  const tabId = browserMocks.create.mock.calls[0][0] as string;
  await waitFor(() => expect(eventMocks.handlers.has("nebula-browser-page")).toBe(true));
  act(() => {
    eventMocks.handlers.get("nebula-browser-page")?.({
      payload: { tabId, url: finalUrl, state: "loaded" },
    });
  });
}

describe("WorkbenchBrowser", () => {
  beforeEach(() => {
    eventMocks.handlers.clear();
    runtimeMocks.isTauriRuntime.mockReset();
    runtimeMocks.isTauriRuntime.mockReturnValue(false);
    for (const mock of Object.values(browserMocks)) {
      mock.mockReset();
      mock.mockResolvedValue(undefined);
    }
    browserMocks.capabilities.mockResolvedValue({ engine: "test-webview", projectStorage: "persistent" });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("explains that native browsing is desktop-only in the web workspace", () => {
    renderBrowser();
    expect(screen.getByRole("strong")).toHaveTextContent("Browser is available in the Nebula desktop app");
    expect(screen.getByText(/Native child webviews are intentionally unavailable/)).toBeVisible();
  });

  it("adds the final URL of the current page to Project Sources and links to it", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const { onAddKnowledgeUrl } = renderBrowser();
    await openPage("https://docs.example.com/final-guide");

    const addButton = screen.getByRole("button", { name: "Add current page to Project Sources" });
    expect(addButton).toBeEnabled();
    fireEvent.click(addButton);

    await waitFor(() => expect(onAddKnowledgeUrl).toHaveBeenCalledWith("https://docs.example.com/final-guide"));
    expect(await screen.findByText("Guide is ready for cited retrieval.")).toBeVisible();
    expect(screen.getByRole("link", { name: "View source" })).toHaveAttribute(
      "href",
      "/project?view=sources&source=source-1",
    );
  });

  it("keeps ingestion failures in the browser without a success notice", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const onAddKnowledgeUrl = vi.fn(async () => {
      throw new Error("Only public pages can be added.");
    });
    renderBrowser(onAddKnowledgeUrl);
    await openPage();

    fireEvent.click(screen.getByRole("button", { name: "Add current page to Project Sources" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Only public pages can be added.");
    expect(screen.queryByRole("link", { name: "View source" })).not.toBeInTheDocument();
  });
});
