import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { useState } from "react";
import type { EngagementScopePolicy } from "../api/types";
import type { ApiClient } from "../api/client";
import { ChromeProvider, type ChromeContextValue } from "../state/ChromeContext";
import { DialogProvider, useDialogPresence } from "./DialogSystem";
import { WorkbenchBrowser } from "./WorkbenchBrowser";

const runtimeMocks = vi.hoisted(() => ({
  isTauriRuntime: vi.fn(),
  desktopDeviceId: vi.fn(),
}));

const browserMocks = vi.hoisted(() => ({
  applyProxyScope: vi.fn(),
  bounds: vi.fn(),
  capabilities: vi.fn(),
  captureContext: vi.fn(),
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
  desktopDeviceId: runtimeMocks.desktopDeviceId,
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

const scope: EngagementScopePolicy = {
  engagementId: "project-1",
  allowedCidrs: [],
  allowedDomains: ["docs.example.com"],
  allowedUrls: [],
  allowedPorts: [443],
  allowAllTargets: false,
  prohibitedActions: [],
  localOnly: true,
  maxConcurrency: 1,
  grants: [],
  revision: 4,
};

function browserApi(): ApiClient {
  const identity = {
    id: "identity-1",
    name: "Default identity",
    description: "Project-isolated browser profile",
    color: "#7c6cff",
    storagePartition: "browser-00000000-0000-0000-0000-000000000000",
    ephemeral: false,
    isDefault: true,
    revision: 1,
  };
  const session = {
    id: "browser-session-1",
    name: "Research session",
    identityId: identity.id,
    status: "active" as const,
    captureMode: "headers" as const,
    proxyEnabled: false,
    proxyTrustAcknowledged: false,
    tabs: [{ id: "tab-durable", title: "New tab", position: 0, lastScopeState: "unknown" as const }],
    activeTabId: "tab-durable",
    upstreamProxyEnabled: false,
    interceptionEnabled: false,
    lastSeenAt: "2026-08-24T00:00:00Z",
    revision: 1,
  };
  return {
    getSecurityBrowserWorkspace: vi.fn(async () => ({ identities: [identity], sessions: [session], traffic: [], frames: [], actions: [], handoffs: [] })),
    syncSecurityBrowserSession: vi.fn(async (_session, tabs, activeTabId) => ({ ...session, tabs, activeTabId, revision: 2 })),
    getEngagementScope: vi.fn(async () => scope),
    updateEngagementScope: vi.fn(async (_projectId, request) => ({
      ...scope,
      ...request,
      engagementId: scope.engagementId,
      revision: request.expectedRevision + 1,
    })),
  } as unknown as ApiClient;
}

function BlockingSurfaceControl() {
  const [open, setOpen] = useState(false);
  useDialogPresence(open);
  return <button type="button" onClick={() => setOpen((value) => !value)}>{open ? "Close blocking surface" : "Open blocking surface"}</button>;
}

function renderBrowser(
  onAddKnowledgeUrl = vi.fn(async () => ({ id: "source-1", name: "Guide" })),
  onAskNebula = vi.fn(),
  scopeValue: EngagementScopePolicy | undefined = scope,
  onScopeUpdated = vi.fn(),
  api = browserApi(),
  onAskSelection?: (
    request: { question: string; context: { text: string; sourceKind: string; sourceId?: string; sourceLabel: string; truncated?: boolean } },
    signal: AbortSignal,
    onDelta: (answer: string) => void,
  ) => Promise<{ sessionId: string; answer: string }>,
  onContinueConversation = vi.fn(),
) {
  return {
    onAddKnowledgeUrl,
    onAskNebula,
    onScopeUpdated,
    api,
    onContinueConversation,
    ...render(
      <MemoryRouter>
        <DialogProvider>
          <ChromeProvider value={chrome}>
            <BlockingSurfaceControl />
            <WorkbenchBrowser
              active
              api={api}
              projectId="project-1"
              scope={scopeValue}
              onAddKnowledgeUrl={onAddKnowledgeUrl}
              onAskNebula={onAskNebula}
              assistantRuntimeLabel={onAskSelection ? "Local harness · gpt-test" : undefined}
              onAskSelection={onAskSelection}
              onContinueConversation={onContinueConversation}
              onOpenFiles={() => undefined}
              onScopeUpdated={onScopeUpdated}
            />
          </ChromeProvider>
        </DialogProvider>
      </MemoryRouter>,
    ),
  };
}

async function openPage(finalUrl = "https://docs.example.com/guide") {
  await waitFor(() => expect(screen.getByRole("button", { name: "Go" })).not.toBeDisabled());
  fireEvent.input(screen.getByLabelText("Start browsing"), { target: { value: "https://docs.example.com/start" } });
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
    chrome.settingLensOpen = false;
    runtimeMocks.isTauriRuntime.mockReset();
    runtimeMocks.isTauriRuntime.mockReturnValue(false);
    runtimeMocks.desktopDeviceId.mockReset();
    runtimeMocks.desktopDeviceId.mockResolvedValue("desktop-test");
    for (const mock of Object.values(browserMocks)) {
      mock.mockReset();
      mock.mockResolvedValue(undefined);
    }
    browserMocks.capabilities.mockResolvedValue({ engine: "test-webview", projectStorage: "persistent" });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("passes fresh scope into tab creation before the first page request", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) { return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600); });
    const api = browserApi();
    const fresh = {...scope, revision: 9};
    vi.mocked(api.getEngagementScope).mockResolvedValue(fresh);
    renderBrowser(undefined, undefined, scope, undefined, api);
    await openPage();
    expect(browserMocks.create.mock.calls[0][10]).toEqual(fresh);
  });

  it("refreshes native scope before reload and blocks the reload when installation fails", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) { return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600); });
    const api = browserApi();
    const workspace = await api.getSecurityBrowserWorkspace("project-1");
    workspace.sessions[0].proxyEnabled = true;
    vi.mocked(api.getSecurityBrowserWorkspace).mockResolvedValue(workspace);
    vi.mocked(api.syncSecurityBrowserSession).mockImplementation(async (session, tabs, activeTabId) => ({...session, tabs, activeTabId}));
    renderBrowser(undefined, undefined, scope, undefined, api);
    await openPage();
    browserMocks.applyProxyScope.mockRejectedValueOnce(new Error("Scope installation failed"));
    fireEvent.click(screen.getByRole("button", {name: "Reload"}));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Scope installation failed"));
    expect(browserMocks.control).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", {name: "Reload"}));
    await waitFor(() => expect(browserMocks.control).toHaveBeenCalled());
    expect(browserMocks.applyProxyScope).toHaveBeenLastCalledWith("project-1", "browser-session-1", scope);
    expect(browserMocks.applyProxyScope.mock.invocationCallOrder.at(-1)).toBeLessThan(browserMocks.control.mock.invocationCallOrder[0]);
  });

  it("does not open a page after Core revokes its previously allowed scope", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) { return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600); });
    const api = browserApi();
    vi.mocked(api.getEngagementScope).mockResolvedValue({...scope, allowedDomains: [], revision: 10});
    renderBrowser(undefined, undefined, scope, undefined, api);
    await waitFor(() => expect(screen.getByRole("button", {name: "Go"})).not.toBeDisabled());
    fireEvent.input(screen.getByLabelText("Start browsing"), {target: {value: "https://docs.example.com/"}});
    fireEvent.click(screen.getByRole("button", {name: "Go"}));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Navigation blocked"));
    expect(browserMocks.create).not.toHaveBeenCalled();
  });

  it("shows missing native scope without claiming research state is unavailable", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) { return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600); });
    const api = browserApi();
    api.recordSecurityBrowserTraffic = vi.fn(async (sessionId, event) => ({...event, sessionId, id: "blocked-scope-event"} as never));
    renderBrowser(undefined, undefined, {...scope, allowAllTargets: true}, undefined, api);
    await openPage();
    expect(screen.getByText("Project: all targets")).toBeVisible();
    const tabId = browserMocks.create.mock.calls[0][0] as string;
    await act(async () => eventMocks.handlers.get("nebula-browser-traffic")?.({payload: {
      sessionId: "browser-session-1", tabId, url: "https://docs.example.com/guide", method: "GET", protocol: "http/1.1",
      requestHeaders: {}, responseHeaders: {}, blocked: true, error: "no compiled Project scope is active for this browser session",
    }}));
    expect(screen.getByText("Browser scope unavailable")).toBeVisible();
    expect(screen.getByText(/Navigation blocked: browser scope unavailable/)).toBeVisible();
    expect(screen.queryByText("Research state is unavailable")).not.toBeInTheDocument();
    expect(screen.getByRole("button", {name: "Reload"})).toBeEnabled();
    await act(async () => eventMocks.handlers.get("nebula-browser-traffic")?.({payload: {
      sessionId: "browser-session-1", tabId, url: "https://docs.example.com/guide", method: "GET", protocol: "http/1.1",
      requestHeaders: {}, responseHeaders: {}, blocked: false, statusCode: 200,
    }}));
    expect(screen.queryByText(/Navigation blocked: browser scope unavailable/)).not.toBeInTheDocument();
    expect(screen.getByText("Project: all targets")).toBeVisible();
  });

  it("hides the native browser while a settings lens is open and restores the active tab", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    renderBrowser();
    await openPage();
    await waitFor(() => expect(browserMocks.visible).toHaveBeenLastCalledWith(expect.any(String), "project-1", true));

    chrome.settingLensOpen = true;
    fireEvent.change(screen.getByLabelText("Address or search"), {
      target: { value: "https://docs.example.com/hidden" },
    });
    await waitFor(() => expect(browserMocks.visible).toHaveBeenLastCalledWith(expect.any(String), "project-1", false));

    chrome.settingLensOpen = false;
    fireEvent.change(screen.getByLabelText("Address or search"), {
      target: { value: "https://docs.example.com/restored" },
    });
    await waitFor(() => expect(browserMocks.visible).toHaveBeenLastCalledWith(expect.any(String), "project-1", true));
  });

  it("opens web addresses externally and can add them directly to Project Sources", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue({} as Window);
    const { onAddKnowledgeUrl, api } = renderBrowser();
    await waitFor(() => expect(api.getSecurityBrowserWorkspace).toHaveBeenCalled());
    await act(async () => { await Promise.resolve(); });
    const address = screen.getByRole("textbox", { name: "Web address" });
    fireEvent.change(address, { target: { value: "docs.example.com/guide" } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(open).toHaveBeenCalledWith("https://docs.example.com/guide", "_blank", "noopener,noreferrer");
    expect(await screen.findByRole("status")).toHaveTextContent("New tab requested.");
    expect(screen.getByText(/In scope · Matches Project scope revision 4/)).toBeVisible();

    const addToSources = screen.getByRole("button", { name: "Add to Sources" });
    await waitFor(() => expect(addToSources).toBeEnabled());
    fireEvent.click(addToSources);
    await waitFor(() => expect(onAddKnowledgeUrl).toHaveBeenCalledWith("https://docs.example.com/guide"));
    expect(screen.getByRole("status")).toHaveTextContent("Guide is ready for cited retrieval.");
  });

  it("retains the device-browser address while durable desktop tabs finish loading", async () => {
    const api = browserApi();
    const workspace = await api.getSecurityBrowserWorkspace("project-1");
    let resolveWorkspace!: (value: typeof workspace) => void;
    vi.mocked(api.getSecurityBrowserWorkspace).mockImplementation(() => new Promise(resolve => { resolveWorkspace = resolve; }));
    const open = vi.spyOn(window, "open").mockReturnValue({} as Window);
    renderBrowser(undefined, undefined, scope, undefined, api);
    const address = screen.getByRole("textbox", { name: "Web address" });
    fireEvent.change(address, { target: { value: "docs.example.com/unsent" } });
    await act(async () => { resolveWorkspace(workspace); });
    expect(address).toHaveValue("docs.example.com/unsent");
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(open).toHaveBeenCalledWith("https://docs.example.com/unsent", "_blank", "noopener,noreferrer");
    expect(address).toHaveValue("https://docs.example.com/unsent");
  });

  it("does not confuse an isolated popup's null handle with a blocked popup", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    renderBrowser();
    await act(async () => { await Promise.resolve(); });
    fireEvent.change(screen.getByRole("textbox", { name: "Web address" }), { target: { value: "docs.example.com/guide" } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(open).toHaveBeenCalledWith("https://docs.example.com/guide", "_blank", "noopener,noreferrer");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("If no tab appeared, allow pop-ups for Nebula and choose Open again.");
  });

  it("retains a requested device URL and scope when desktop history arrives later", async () => {
    const api = browserApi();
    const workspace = await api.getSecurityBrowserWorkspace("project-1");
    let resolveWorkspace!: (value: typeof workspace) => void;
    vi.mocked(api.getSecurityBrowserWorkspace).mockImplementation(() => new Promise(resolve => { resolveWorkspace = resolve; }));
    vi.spyOn(window, "open").mockReturnValue(null);
    renderBrowser(undefined, undefined, scope, undefined, api);
    fireEvent.change(screen.getByRole("textbox", { name: "Web address" }), { target: { value: "docs.example.com/guide" } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    await act(async () => { resolveWorkspace(workspace); });
    expect(screen.getByRole("textbox", { name: "Web address" })).toHaveValue("https://docs.example.com/guide");
    expect(screen.getByText(/In scope · Matches Project scope revision 4/)).toBeVisible();
    expect(api.syncSecurityBrowserSession).not.toHaveBeenCalled();
  });

  it("retains the address and offers an explicit retry when requesting a tab throws", async () => {
    const open = vi.spyOn(window, "open").mockImplementationOnce(() => { throw new Error("Local popup policy"); }).mockReturnValue(null);
    renderBrowser();
    await act(async () => { await Promise.resolve(); });
    const address = screen.getByRole("textbox", { name: "Web address" });
    fireEvent.change(address, { target: { value: "docs.example.com/guide" } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Could not request a new tab.");
    expect(address).toHaveValue("https://docs.example.com/guide");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(open).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("New tab requested.");
  });

  it("hides and restores the native browser while a blocking application surface is open", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    renderBrowser();
    await openPage();
    const tabId = browserMocks.create.mock.calls[0][0] as string;
    await waitFor(() => expect(browserMocks.visible).toHaveBeenCalledWith(tabId, "project-1", true));

    fireEvent.click(screen.getByRole("button", { name: "Open blocking surface" }));
    await waitFor(() => expect(browserMocks.visible).toHaveBeenCalledWith(tabId, "project-1", false));

    fireEvent.click(screen.getByRole("button", { name: "Close blocking surface" }));
    await waitFor(() => expect(browserMocks.visible).toHaveBeenLastCalledWith(tabId, "project-1", true));
  });

  it("adds the final URL of the current page to Project Sources and links to it", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const { onAddKnowledgeUrl } = renderBrowser();
    await openPage("https://docs.example.com/final-guide");

    expect(screen.getByText("In scope")).toBeVisible();

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

  it("captures the live authenticated page only on request and opens a reviewed chat attachment", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const { onAskNebula } = renderBrowser();
    await openPage("https://docs.example.com/account");

    expect(browserMocks.captureContext).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Ask Nebula about the live page" }));
    await waitFor(() => expect(browserMocks.captureContext).toHaveBeenCalledTimes(1));
    const [tabId, projectId, requestId] = browserMocks.captureContext.mock.calls[0];
    expect(projectId).toBe("project-1");

    act(() => {
      eventMocks.handlers.get("nebula-browser-context")?.({
        payload: {
          requestId,
          tabId,
          state: "ready",
          context: {
            url: "https://docs.example.com/account",
            title: "Account portal",
            selectedText: "role=analyst",
            text: "Authenticated account page",
            truncated: false,
            forms: [{ method: "POST", action: "https://docs.example.com/profile", fields: [{ name: "display_name", id: "name", type: "text", autocomplete: "name", required: true }] }],
            links: [{ text: "Billing", href: "https://docs.example.com/billing" }],
          },
        },
      });
    });

    expect(onAskNebula).toHaveBeenCalledTimes(1);
    expect(onAskNebula.mock.calls[0][0]).toMatchObject({
      sourceKind: "browser_page",
      sourceLabel: "Browser · Account portal",
      truncated: false,
    });
    expect(onAskNebula.mock.calls[0][0].text).toContain("LIVE BROWSER CAPTURE");
    expect(onAskNebula.mock.calls[0][0].text).toContain("Project scope: In scope (revision 4)");
    expect(onAskNebula.mock.calls[0][0].text).toContain("role=analyst");
    expect(onAskNebula.mock.calls[0][0].text).not.toContain('"value"');
  });

  it("answers a native page selection inline and continues the same durable conversation", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const onAskSelection = vi.fn(async (_request, _signal, onDelta: (answer: string) => void) => {
      onDelta("This header rotates ");
      onDelta("This header rotates for each request.");
      return { sessionId: "chat-selection-1", answer: "This header rotates for each request." };
    });
    const onContinueConversation = vi.fn();
    renderBrowser(undefined, undefined, scope, undefined, undefined, onAskSelection, onContinueConversation);
    await openPage("https://docs.example.com/account");
    const tabId = browserMocks.create.mock.calls[0][0] as string;
    await waitFor(() => expect(eventMocks.handlers.has("nebula-browser-selection-request")).toBe(true));

    act(() => {
      eventMocks.handlers.get("nebula-browser-selection-request")?.({
        payload: { tabId, projectId: "project-1", url: "https://docs.example.com/account" },
      });
    });
    await waitFor(() => expect(browserMocks.captureContext).toHaveBeenCalledTimes(1));
    const requestId = browserMocks.captureContext.mock.calls[0][2];
    await waitFor(() => expect(eventMocks.handlers.has("nebula-browser-context")).toBe(true));
    act(() => {
      eventMocks.handlers.get("nebula-browser-context")?.({
        payload: {
          requestId,
          tabId,
          state: "ready",
          context: {
            url: "https://docs.example.com/account",
            title: "Account portal",
            selectedText: "X-CSRF-Token: rotating-value",
            text: "Other page content is not attached.",
            truncated: false,
            forms: [],
            links: [],
          },
        },
      });
    });

    expect(await screen.findByRole("heading", { name: "Ask Nebula" })).toBeVisible();
    expect(screen.getByText("X-CSRF-Token: rotating-value")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Question about selected text"), { target: { value: "What does this imply?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask question" }));

    await waitFor(() => expect(onAskSelection).toHaveBeenCalled());
    expect(onAskSelection.mock.calls[0][0]).toMatchObject({
      question: "What does this imply?",
      context: { sourceKind: "browser_selection", sourceLabel: "Browser selection · Account portal" },
    });
    expect(onAskSelection.mock.calls[0][0].context.text).toContain("LIVE BROWSER SELECTION");
    expect(onAskSelection.mock.calls[0][0].context.text).not.toContain("Other page content is not attached.");
    expect(await screen.findByText("This header rotates for each request.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Continue in Assistant" }));
    expect(onContinueConversation).toHaveBeenCalledWith("chat-selection-1");
  });

  it("fails closed when the live page or its final capture is outside durable Project scope", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const first = renderBrowser();
    await openPage("https://outside.example.net/account");

    const ask = screen.getByRole("button", { name: "Ask Nebula about the live page" });
    expect(ask).toBeDisabled();
    expect(ask).toHaveAttribute("title", expect.stringContaining("confirmed in scope"));
    expect(browserMocks.captureContext).not.toHaveBeenCalled();

    first.unmount();
    browserMocks.create.mockClear();
    browserMocks.captureContext.mockClear();
    const { onAskNebula } = renderBrowser();
    await openPage("https://docs.example.com/account");
    fireEvent.click(screen.getByRole("button", { name: "Ask Nebula about the live page" }));
    await waitFor(() => expect(browserMocks.captureContext).toHaveBeenCalledTimes(1));
    const [tabId, , requestId] = browserMocks.captureContext.mock.calls[0];
    act(() => {
      eventMocks.handlers.get("nebula-browser-context")?.({
        payload: {
          requestId,
          tabId,
          state: "ready",
          context: {
            url: "https://outside.example.net/redirected",
            title: "Redirected page",
            selectedText: "",
            text: "This content must not enter Chat.",
            truncated: false,
            forms: [],
            links: [],
          },
        },
      });
    });

    expect(onAskNebula).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("final page is not confirmed in scope");
  });

  it("keeps live-page capture failures actionable in the Browser", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    renderBrowser();
    await openPage();
    fireEvent.click(screen.getByRole("button", { name: "Ask Nebula about the live page" }));
    await waitFor(() => expect(browserMocks.captureContext).toHaveBeenCalledTimes(1));
    const [tabId, , requestId] = browserMocks.captureContext.mock.calls[0];

    act(() => {
      eventMocks.handlers.get("nebula-browser-context")?.({
        payload: { requestId, tabId, state: "failed", detail: "The page changed during capture." },
      });
    });

    expect(screen.getByRole("alert")).toHaveTextContent("The page changed during capture.");
    expect(screen.getByRole("button", { name: "Ask Nebula about the live page" })).toBeEnabled();
  });

  it("adds the right-clicked origin to durable Project scope after confirmation", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const api = browserApi();
    const onScopeUpdated = vi.fn();
    renderBrowser(vi.fn(async () => ({ id: "source-1", name: "Guide" })), vi.fn(), scope, onScopeUpdated, api);
    await openPage("https://outside.example.net/account?view=security");
    const tabId = browserMocks.create.mock.calls[0][0] as string;

    act(() => {
      eventMocks.handlers.get("nebula-browser-scope-request")?.({
        payload: {
          tabId,
          projectId: "project-1",
          url: "https://outside.example.net/account?view=security",
          state: "ready",
        },
      });
    });

    expect(await screen.findByText("Add https://outside.example.net/ to scope?")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Add to scope" }));

    await waitFor(() => expect(api.updateEngagementScope).toHaveBeenCalledWith("project-1", expect.objectContaining({
      allowedUrls: ["https://outside.example.net/"],
      allowedDomains: ["docs.example.com"],
      expectedRevision: 4,
    })));
    expect(onScopeUpdated).toHaveBeenCalledWith(expect.objectContaining({ revision: 5 }));
    expect(await screen.findByRole("status")).toHaveTextContent("was added to Project scope revision 5");
  });

  it("loads a concurrent scope revision without overwriting it and explains the retry", async () => {
    runtimeMocks.isTauriRuntime.mockReturnValue(true);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return new DOMRect(0, 0, 900, this.classList.contains("browser-toolbar") ? 48 : 600);
    });
    const api = browserApi();
    vi.mocked(api.updateEngagementScope).mockRejectedValueOnce(new Error("revision conflict"));
    const onScopeUpdated = vi.fn();
    renderBrowser(vi.fn(async () => ({ id: "source-1", name: "Guide" })), vi.fn(), scope, onScopeUpdated, api);
    await openPage("https://outside.example.net/account");
    vi.mocked(api.getEngagementScope).mockResolvedValueOnce({ ...scope, allowedDomains: ["docs.example.com", "new.example"], revision: 5 });
    const tabId = browserMocks.create.mock.calls[0][0] as string;
    act(() => {
      eventMocks.handlers.get("nebula-browser-scope-request")?.({
        payload: { tabId, projectId: "project-1", url: "https://outside.example.net/account", state: "ready" },
      });
    });
    fireEvent.click(await screen.findByRole("button", { name: "Add to scope" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Project scope changed before this addition could be saved");
    expect(onScopeUpdated).toHaveBeenCalledWith(expect.objectContaining({ revision: 5 }));
    expect(api.updateEngagementScope).toHaveBeenCalledTimes(1);
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
