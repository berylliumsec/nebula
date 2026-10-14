import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkspace } from "../state/WorkspaceContext";
import type { ChatStreamEvent } from "../api/types";
import { ThemeProvider } from "../state/ThemeContext";
import { DialogProvider } from "./DialogSystem";
import { NebulaControlAssistant } from "./NebulaControlAssistant";
import { NebulaAssistantLauncherButton } from "./NebulaAssistantLauncher";

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: vi.fn(),
}));

describe("NebulaControlAssistant", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(useWorkspace).mockReturnValue({ api: undefined, engagement: undefined, providers: [], harnesses: [] } as unknown as ReturnType<typeof useWorkspace>);
  });

  it("offers project work prompts and sends a status request through the control conversation", async () => {
    const user = userEvent.setup();
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: { id: "answer", role: "assistant", content: "One mission is running." },
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.click(await screen.findByRole("button", { name: "Start mission" }));
    expect(screen.getByRole("textbox", { name: "Ask Nebula a question" })).toHaveValue("Start a mission to ");
    await user.click(screen.getByRole("button", { name: "Check work" }));
    await user.click(screen.getByRole("button", { name: "Send to Nebula assistant" }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ controlAssistant: true, engagementId: "project", messages: [{ role: "user", content: "What are my assistants and missions doing in this project?" }] }),
      expect.any(Function), expect.any(AbortSignal),
    ));
    expect(await screen.findByText("One mission is running.")).toBeInTheDocument();
    expect(screen.getByText("Work I can help with")).toBeInTheDocument();
    await user.click(screen.getByText("Work I can help with"));
    await user.click(screen.getByRole("button", { name: "Start assistant" }));
    expect(screen.getByRole("textbox", { name: "Ask Nebula a question" })).toHaveValue("Start an assistant to ");
  });

  it("keeps one conversation behind the shell launcher without a floating overlay", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()}>
      <header><NebulaAssistantLauncherButton /></header>
    </NebulaControlAssistant></ThemeProvider></DialogProvider>);
    const launcher = screen.getByRole("button", { name: "Open Nebula assistant" });
    expect(launcher.closest("header")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Open Nebula assistant" })).toHaveLength(1);
    await user.click(launcher);
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "Help me find a setting");
    await user.click(screen.getByRole("button", { name: "Hide Nebula assistant" }));
    await user.click(screen.getByRole("button", { name: "Show Nebula assistant" }));
    expect(screen.getByRole("textbox", { name: "Ask Nebula a question" })).toHaveValue("Help me find a setting");
  });

  it("opens from the global icon, applies a device theme, and keeps the setting catalog reachable", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.click(screen.getByText("Settings I can help with"));
    await user.click(screen.getByRole("button", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("nebula.theme")).toBe("light");
    await user.type(screen.getByRole("textbox", { name: "Find a setting" }), "provider");
    await user.click(screen.getByRole("button", { name: /Model providers.*Direct actions available/ }));
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.providers" }));
    expect(screen.getByRole("button", { name: "Show Nebula assistant" })).toBeInTheDocument();
  });

  it("changes device themes from natural text requests without a project or model", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    const onCloseSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "change to zero theme");
    expect(screen.getByRole("button", { name: "Send to Nebula assistant" })).toBeEnabled();
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "{enter}");
    await waitFor(() => expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.appearance" })));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("zero-dark"));
    expect(localStorage.getItem("nebula.theme")).toBe("zero-dark");
    await waitFor(() => expect(onCloseSetting).toHaveBeenCalled(), { timeout: 4000 });
    expect(screen.getByText("Changed this device to Zero Dark. The choice is saved in this browser.")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "change it to the light theme{enter}");
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(localStorage.getItem("nebula.theme")).toBe("light");
    await waitFor(() => expect(screen.getByText("Changed this device to Light. The choice is saved in this browser.")).toBeInTheDocument(), { timeout: 4000 });
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "change it to the rainbow theme{enter}");
    expect(screen.getByText("I couldn't identify that theme. Choose one in Settings → Appearance.")).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("applies a model-selected theme action on this browser and confirms it after readback", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    const onCloseSetting = vi.fn();
    const getControlAssistantSession = vi.fn().mockResolvedValue(null);
    const listChatMessages = vi.fn().mockResolvedValue([
      { id: "question", role: "user", content: "could you make the screen look like Nebula Zero?" },
      { id: "answer", role: "assistant", content: "The theme request is pending in the browser." },
    ]);
    const streamChat = vi.fn().mockImplementation(async (_body: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({
        type: "tool_completed", turnId: "turn", toolCallId: "theme-call", capability: "nebula.settings.set_device_theme",
        status: "complete", summary: "Action requested", evidenceIds: [], artifacts: [], step: 0,
        receipt: { state: "pending_browser_apply", client_action: { kind: "device_theme", theme: "zero-dark" } },
      });
      return { sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
        message: { id: "answer", role: "assistant", content: "Applying Zero Dark on this device." } };
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession, listChatMessages, streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const mounted = render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "could you make the screen look like Nebula Zero?{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.appearance" })));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("zero-dark"));
    await waitFor(() => expect(onCloseSetting).toHaveBeenCalled(), { timeout: 4000 });
    expect(screen.getByText("Changed this device to Zero Dark. The choice is saved in this browser.")).toBeInTheDocument();
    getControlAssistantSession.mockResolvedValue({ id: "control", engagementId: "project", backend: "harness", harnessProfileId: "local", model: "model" });
    mounted.unmount();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await waitFor(() => expect(listChatMessages).toHaveBeenCalledWith("control"));
    expect(await screen.findByText("Changed this device to Zero Dark. The choice is saved in this browser.")).toBeInTheDocument();
  });

  it("requires operator confirmation for a model-selected Host action before Core readback", async () => {
    const user = userEvent.setup();
    const original = { executionMode: "docker", approvalPolicy: "always", networkEnabled: false, revision: 2 };
    const saved = { ...original, executionMode: "host", revision: 3 };
    const getAutomationPolicy = vi.fn().mockResolvedValueOnce(original).mockResolvedValueOnce(saved);
    const updateAutomationPolicy = vi.fn().mockResolvedValue(saved);
    const streamChat = vi.fn().mockImplementation(async (_body: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({ type: "tool_completed", turnId: "turn", toolCallId: "mode-call", capability: "nebula.settings.set_project_mode",
        status: "complete", summary: "Action requested", evidenceIds: [], artifacts: [], step: 0,
        receipt: { state: "pending_browser_apply", client_action: { kind: "project_execution_mode", mode: "host" } } });
      return { sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
        message: { role: "assistant", content: "I requested the Host mode change." } };
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat, getAutomationPolicy, updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "could you get this project's commands running on the host for me?{enter}");
    await waitFor(() => expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.network-scope" })));
    expect(await screen.findByRole("heading", { name: "Switch Project to Host mode?" })).toBeInTheDocument();
    expect(screen.getByText(/Agents in new sessions can access and modify host folders/)).toBeInTheDocument();
    expect(updateAutomationPolicy).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Switch to Host mode" }));
    await waitFor(() => expect(updateAutomationPolicy).toHaveBeenCalledWith("project", expect.objectContaining({
      executionMode: "host", hostAccessAcknowledged: true, expectedRevision: 2,
    })));
    await waitFor(() => expect(getAutomationPolicy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText(/Project is now in Host mode for new sessions/)).toBeInTheDocument(), { timeout: 4000 });
  });

  it("saves an all-destinations policy from a model receipt and refreshes Project Policy", async () => {
    const user = userEvent.setup();
    const current = { revision: 2, allow_all_targets: false, bypass_permissions: false, local_only: true,
      allowed_cidrs: [], allowed_domains: ["example.test"], allowed_urls: [], allowed_ports: [443],
      prohibited_actions: [], grants: [], max_concurrency: 1 };
    const saved = { ...current, revision: 3, allow_all_targets: true };
    const request = vi.fn().mockResolvedValueOnce(current).mockResolvedValueOnce(saved).mockResolvedValueOnce(saved);
    const streamChat = vi.fn().mockImplementation(async (_body: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({ type: "tool_completed", turnId: "turn", toolCallId: "scope-call", capability: "nebula.settings.set_project_scope",
        status: "complete", summary: "Action requested", evidenceIds: [], artifacts: [], step: 0,
        receipt: { state: "pending_browser_apply", client_action: { kind: "project_scope", changes: { mode: "all_destinations" } } } });
      return { sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
        message: { role: "assistant", content: "Applying the scope." } };
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat, request },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onSavedSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} onSavedSetting={onSavedSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "allow all URLs and network targets{enter}");
    await waitFor(() => expect(onSavedSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.network-scope" }), expect.stringContaining("All destinations")));
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({ allow_all_targets: true, bypass_permissions: false, local_only: true, expected_revision: 2 });
    expect(screen.queryByRole("button", { name: /Confirm|Allow all/ })).not.toBeInTheDocument();
  });

  it("starts the Core headless browser and reports its observed session", async () => {
    const user = userEvent.setup();
    const currentScope = { revision: 2, allow_all_targets: false, bypass_permissions: false, local_only: true };
    const savedScope = { ...currentScope, revision: 3, allow_all_targets: true };
    const request = vi.fn().mockResolvedValueOnce(currentScope).mockResolvedValueOnce(savedScope).mockResolvedValueOnce(savedScope);
    const startHeadlessSecuritySession = vi.fn().mockResolvedValue({ session_id: "headless", state: "active" });
    const getHeadlessSecuritySession = vi.fn().mockResolvedValue({ session_id: "headless", state: "active" });
    const streamChat = vi.fn().mockImplementation(async (_body: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({ type: "tool_completed", turnId: "turn", toolCallId: "headless-call", capability: "nebula.settings.headless_session",
        status: "complete", summary: "Action requested", evidenceIds: [], artifacts: [], step: 0,
        receipt: { state: "pending_browser_apply", client_action: { kind: "headless_session", action: "start" } } });
      return { sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
        message: { role: "assistant", content: "Starting the browser." } };
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat, request, startHeadlessSecuritySession, getHeadlessSecuritySession },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "set project policy to allow all URLs and network targets, then start the headless browser session{enter}");
    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(startHeadlessSecuritySession).toHaveBeenCalledWith("project"));
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({ allow_all_targets: true, bypass_permissions: false });
    await waitFor(() => expect(screen.getByText(/headless security browser: active/)).toBeInTheDocument());
    expect(getHeadlessSecuritySession).toHaveBeenCalledWith("project");
  });

  it("hides, restores, and closes without discarding the draft", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "Help me choose a theme");
    await user.click(screen.getByRole("button", { name: "Hide Nebula assistant" }));
    await user.click(screen.getByRole("button", { name: "Show Nebula assistant" }));
    expect(screen.getByRole("textbox", { name: "Ask Nebula a question" })).toHaveValue("Help me choose a theme");
    await user.click(screen.getByRole("button", { name: "Close Nebula assistant" }));
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    expect(screen.getByRole("textbox", { name: "Ask Nebula a question" })).toHaveValue("Help me choose a theme");
  });

  it("opens a named setting from Enter through the settings lens callback", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "open MCP servers{enter}");
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.mcp" }));
    expect(screen.getByRole("button", { name: "Show Nebula assistant" })).toBeInTheDocument();
  });

  it("opens All projects immediately from 'show me my projects', without a model or selected project", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "show me my projects{enter}");
    expect(onNavigate).toHaveBeenCalledExactlyOnceWith("/projects");
    expect(screen.getByRole("button", { name: "Show Nebula assistant" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show Nebula assistant" }));
    expect(screen.getByText("Opened All projects.")).toBeInTheDocument();
  });

  it("applies a model-selected page action only from its scoped tool receipt", async () => {
    const user = userEvent.setup();
    const streamChat = vi.fn().mockImplementation(async (_body: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({ type: "tool_completed", turnId: "turn", toolCallId: "nav-call", capability: "nebula.navigate.open",
        status: "complete", summary: "Navigation requested", evidenceIds: [], artifacts: [], step: 0,
        receipt: { state: "pending_browser_apply", client_action: { kind: "navigate", destination_id: "projects" } } });
      return { sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
        message: { role: "assistant", content: "Opening the Projects list." } };
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onNavigate = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    const composer = screen.getByRole("textbox", { name: "Ask Nebula a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "locate the place with everything I've built{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onNavigate).toHaveBeenCalledExactlyOnceWith("/projects"));
  });

  it("keeps the catalog available from a navigation prompt without a model", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "open pineapple moon");
    const send = screen.getByRole("button", { name: "Send to Nebula assistant" });
    expect(send).toBeEnabled();
    await user.click(send);
    expect(screen.getByText("I couldn't identify a Nebula destination. Browse the places and settings below.")).toBeInTheDocument();
  });

  it("sends once on Enter, keeps Shift+Enter as a newline, and ignores composition", async () => {
    const user = userEvent.setup();
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: { role: "assistant", content: "Done" },
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    const composer = screen.getByRole("textbox", { name: "Ask Nebula a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "First{shift>}{enter}{/shift}second");
    expect(composer).toHaveValue("First\nsecond");
    fireEvent.keyDown(composer, { key: "Enter", isComposing: true });
    expect(streamChat).not.toHaveBeenCalled();
    await user.type(composer, "{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    expect(streamChat.mock.calls[0][0]).toMatchObject({ messages: [{ content: "First\nsecond" }] });
  });

  it("answers a Docker host-file question and opens a cited setting as a lens", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: { role: "assistant", content: "Docker can edit the linked project workspace. [Open Project Policy](/settings#engagement-policy-settings) to review Host mode." },
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    const composer = screen.getByRole("textbox", { name: "Ask Nebula a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "Why can't my assistant edit a host file in Docker mode?{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    expect(streamChat.mock.calls[0][0]).toMatchObject({ controlAssistant: true });
    expect(onOpenSetting).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("link", { name: "Open Project Policy" }));
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.network-scope" }));
    expect(screen.getByRole("button", { name: "Show Nebula assistant" })).toBeInTheDocument();
  });

  it("opens Project Policy, saves Host acknowledgment, preserves fields, and reads Core back", async () => {
    const user = userEvent.setup();
    const original = {
      id: "policy", engagementId: "project", executionMode: "docker", hostAccessAcknowledged: false,
      approvalPolicy: "always", networkEnabled: true, runnerProfileId: "runner", vpnProfileId: "vpn",
      maxTimeoutMs: 120000, revision: 3,
    } as const;
    const saved = { ...original, executionMode: "host", hostAccessAcknowledged: true, revision: 4 } as const;
    const getAutomationPolicy = vi.fn().mockResolvedValueOnce(original).mockResolvedValueOnce(saved);
    const updateAutomationPolicy = vi.fn().mockResolvedValue(saved);
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), getAutomationPolicy, updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [], harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onOpenSetting = vi.fn();
    const onCloseSetting = vi.fn();
    const onSavedSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} onSavedSetting={onSavedSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    const composer = screen.getByRole("textbox", { name: "Ask Nebula a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "open project policy and change from dockermode to host mode{enter}");
    await waitFor(() => expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.network-scope" })));
    expect(await screen.findByRole("heading", { name: "Switch Project to Host mode?" })).toBeInTheDocument();
    expect(updateAutomationPolicy).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Switch to Host mode" }));
    await waitFor(() => expect(updateAutomationPolicy).toHaveBeenCalledWith("project", {
      executionMode: "host", hostAccessAcknowledged: true, approvalPolicy: "always",
      networkEnabled: true, runnerProfileId: "runner", vpnProfileId: "vpn",
      maxTimeoutMs: 120000, expectedRevision: 3,
    }));
    await waitFor(() => expect(onSavedSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.network-scope" }), "Project saved in Host mode for new sessions.", "host"));
    await waitFor(() => expect(onCloseSetting).toHaveBeenCalled(), { timeout: 4000 });
    expect(getAutomationPolicy).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/Project is now in Host mode for new sessions/)).toBeInTheDocument();
  });

  it("leaves Core policy untouched when the operator cancels Host access", async () => {
    const user = userEvent.setup();
    const updateAutomationPolicy = vi.fn();
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null),
        getAutomationPolicy: vi.fn().mockResolvedValue({ executionMode: "docker", revision: 3 }), updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [], harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} onCloseSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula a question" }), "open project policy and change from dockermode to host mode{enter}");
    expect(await screen.findByRole("heading", { name: "Switch Project to Host mode?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(updateAutomationPolicy).not.toHaveBeenCalled();
    expect(await screen.findByText("Project remains in Docker mode. No change was saved.")).toBeInTheDocument();
  });

  it("does not change mode when the requested source mode differs from Core", async () => {
    const user = userEvent.setup();
    const updateAutomationPolicy = vi.fn();
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null),
        getAutomationPolicy: vi.fn().mockResolvedValue({ executionMode: "host" }), updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [], harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} onCloseSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    const composer = screen.getByRole("textbox", { name: "Ask Nebula a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "open project policy and change from dockermode to host mode{enter}");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("already in Host mode"));
    expect(updateAutomationPolicy).not.toHaveBeenCalled();
    expect(composer).toHaveValue("open project policy and change from dockermode to host mode");
  });
});
