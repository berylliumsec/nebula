import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { useWorkspace } from "../state/WorkspaceContext";
import type { ChatStreamEvent } from "../api/types";
import { ThemeProvider } from "../state/ThemeContext";
import { WorkbenchDraftProvider } from "../state/WorkbenchDraftContext";
import { DialogProvider } from "./DialogSystem";
import { NebulaControlAssistant } from "./NebulaControlAssistant";
import { NebulaAssistantLauncherButton } from "./NebulaAssistantLauncher";

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: vi.fn(),
}));

const savedButler = {
  id: "control", engagementId: "project", backend: "harness" as const,
  harnessProfileId: "local", model: "model", isControlAssistant: true,
};

describe("NebulaControlAssistant", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(useWorkspace).mockReturnValue({ api: undefined, engagement: undefined, providers: [], harnesses: [] } as unknown as ReturnType<typeof useWorkspace>);
  });

  it("sends selected Butler text to Butler and keeps it available after a failed turn", async () => {
    const user = userEvent.setup();
    const response = (content: string) => ({
      sessionId: "control", backend: "harness" as const, harnessProfileId: "local", model: "model",
      message: { id: content, role: "assistant" as const, content },
    });
    const streamChat = vi.fn()
      .mockResolvedValueOnce(response("The goal needs another turn."))
      .mockRejectedValueOnce(new Error("Core connection failed"))
      .mockResolvedValueOnce(response("The context reached Butler."));
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<MemoryRouter><WorkbenchDraftProvider><DialogProvider><ThemeProvider>
      <NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} />
    </ThemeProvider></DialogProvider></WorkbenchDraftProvider></MemoryRouter>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "What happened?{enter}");
    const answer = await screen.findByText("The goal needs another turn.");
    const range = document.createRange();
    range.selectNodeContents(answer);
    document.getSelection()?.removeAllRanges();
    document.getSelection()?.addRange(range);
    fireEvent.pointerUp(answer);
    await user.click(screen.getByRole("button", { name: "Add context to chat" }));
    expect(screen.getByRole("region", { name: "Butler context pack" })).toHaveTextContent("1 selected context item for Butler");
    await user.type(composer, "Use that detail.{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2));
    expect(streamChat.mock.calls[1][0].contextAttachments).toEqual([expect.objectContaining({
      sourceKind: "conversation", sourceId: "control", sourceLabel: "Butler conversation",
      text: "The goal needs another turn.", sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
    })]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Core connection failed");
    expect(screen.getByRole("region", { name: "Butler context pack" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Send to Butler" }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(3));
    expect(streamChat.mock.calls[2][0].contextAttachments).toEqual(streamChat.mock.calls[1][0].contextAttachments);
    expect(await screen.findByText("The context reached Butler.")).toBeVisible();
    await waitFor(() => expect(screen.queryByRole("region", { name: "Butler context pack" })).not.toBeInTheDocument());
  });

  it("opens the full-screen voice mode from its assistant button and keeps an HTTPS recovery action visible", async () => {
    const user = userEvent.setup();
    const secure = window.isSecureContext;
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), getVoiceWorkRoster: vi.fn().mockResolvedValue({ project: { id: "project", name: "Project" }, chats: [], missions: [], more_chats: false, more_missions: false }) },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.click(screen.getByRole("button", { name: "Start Butler Voice" }));
    expect(await screen.findByRole("dialog", { name: "Butler Voice" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("trusted HTTPS address");
    await user.click(screen.getByRole("button", { name: "Exit voice mode" }));
    expect(screen.queryByRole("dialog", { name: "Butler Voice" })).not.toBeInTheDocument();
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: secure });
  });

  it("saves the Core-wide Voice timeout independently and restores it after a failed change", async () => {
    const user = userEvent.setup();
    let saved = { autoPauseSeconds: 20 as 10 | 20 | 30 | 60, revision: 0 };
    const getButlerVoiceSettings = vi.fn().mockImplementation(async () => saved);
    const updateButlerVoiceSettings = vi.fn().mockImplementation(async (revision: number, seconds: 10 | 20 | 30 | 60) => {
      if (seconds === 60) throw new Error("Voice settings changed on another device.");
      expect(revision).toBe(saved.revision);
      saved = { autoPauseSeconds: seconds, revision: revision + 1 };
      return saved;
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), getButlerVoiceSettings, updateButlerVoiceSettings },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.click(screen.getByRole("button", { name: "Butler settings" }));
    const timeout = await screen.findByRole("combobox", { name: "Pause Voice after" });
    await waitFor(() => expect(timeout).toBeEnabled());
    expect(timeout).toHaveValue("20");
    await user.selectOptions(timeout, "30");
    expect(await screen.findByText("Saved for the next Voice session on this Core.")).toBeVisible();
    expect(updateButlerVoiceSettings).toHaveBeenCalledWith(0, 30);
    expect(timeout).toHaveValue("30");
    await user.selectOptions(timeout, "60");
    expect(await screen.findByRole("alert")).toHaveTextContent("Voice settings changed on another device.");
    expect(timeout).toHaveValue("30");
    saved = { autoPauseSeconds: 10, revision: 2 };
    await user.click(screen.getByRole("button", { name: "Butler settings" }));
    await user.click(screen.getByRole("button", { name: "Butler settings" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Pause Voice after" })).toHaveValue("10"));
  });

  it("keeps Core-wide Voice settings open while the selected project first loads", async () => {
    const user = userEvent.setup();
    const api = {
      getControlAssistantSession: vi.fn().mockResolvedValue(null),
      getButlerVoiceSettings: vi.fn().mockResolvedValue({ autoPauseSeconds: 30, revision: 1 }),
    };
    let workspace = { api, engagement: undefined as { id: string; name: string } | undefined, providers: [], harnesses: [] };
    vi.mocked(useWorkspace).mockImplementation(() => workspace as unknown as ReturnType<typeof useWorkspace>);
    const assistant = () => <DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>;
    const view = render(assistant());
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.click(screen.getByRole("button", { name: "Butler settings" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Pause Voice after" })).toHaveValue("30"));
    workspace = { ...workspace, engagement: { id: "project", name: "Project" } };
    view.rerender(assistant());
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Butler" })).toHaveTextContent("Project"));
    expect(screen.getByRole("combobox", { name: "Pause Voice after" })).toHaveValue("30");
  });

  it("offers project work prompts and sends a status request through the control conversation", async () => {
    const user = userEvent.setup();
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: { id: "answer", role: "assistant", content: "One mission is running." },
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.click(await screen.findByRole("button", { name: "Start mission" }));
    expect(screen.getByRole("textbox", { name: "Ask Butler a question" })).toHaveValue("Start a mission to ");
    await user.click(screen.getByRole("button", { name: "Check work" }));
    await user.click(screen.getByRole("button", { name: "Send to Butler" }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ controlAssistant: true, engagementId: "project", messages: [{ role: "user", content: "What are my assistants and missions doing in this project?" }] }),
      expect.any(Function), expect.any(AbortSignal),
    ));
    expect(await screen.findByText("One mission is running.")).toBeInTheDocument();
    expect(screen.getByText("Work I can help with")).toBeInTheDocument();
    await user.click(screen.getByText("Work I can help with"));
    await user.click(screen.getByRole("button", { name: "Start assistant" }));
    expect(screen.getByRole("textbox", { name: "Ask Butler a question" })).toHaveValue("Start an assistant to ");
  });

  it("saves Butler's advertised effort and Daybreak Blue, then uses Core readback on its next turn", async () => {
    const user = userEvent.setup();
    const saved = {
      id: "control", engagementId: "project", backend: "harness", harnessProfileId: "local", model: "model",
      assistantConfig: {
        backend: "harness", profileId: "local", model: "model", harnessReasoningEffort: "high",
        harnessMode: "plan", mcpServerIds: ["notebook"], hookIds: [],
        includeKnowledge: false, allowCloudKnowledge: false, toolsEnabled: true,
        allowSubagents: false, allowAgentMessaging: true,
      },
    };
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: {id: "answer", role: "assistant", content: "Ready."},
    });
    let revision = 3;
    let currentConfig = { ...saved.assistantConfig, harnessCyberAccessProgram: "automatic" };
    const getChatAssistantConfig = vi.fn().mockImplementation(async () => ({sessionId: "control", revision, settings: currentConfig}));
    const updateChatAssistantConfig = vi.fn().mockImplementation(async (_id: string, expected: number, patch: Record<string, unknown>) => {
      expect(expected).toBe(revision);
      currentConfig = { ...currentConfig, ...patch } as typeof currentConfig;
      revision += 1;
      return {sessionId: "control", revision, settings: currentConfig};
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler),getControlAssistantSession: vi.fn().mockResolvedValue(saved), getChatAssistantConfig, updateChatAssistantConfig,
        listChatMessages: vi.fn().mockResolvedValue([]), streamChat},
      engagement: {id: "project", name: "Project"}, providers: [],
      harnesses: [{id: "local", name: "Local", kind: "codex_app_server", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [{model: "model", reasoningEfforts: [
          {id: "low", label: "Low", description: ""}, {id: "high", label: "High", description: ""}],
          serviceTiers: [], cyberAccessPrograms: ["standard", "daybreakBlue"]}], defaultModel: "model"}],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onNavigate = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", {name: "Open Butler"}));
    await waitFor(() => expect(screen.getByRole("button", {name: "Butler settings"})).toBeEnabled());
    await user.type(screen.getByRole("textbox", {name: "Ask Butler a question"}), "Summarize the work{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith(expect.objectContaining({
      controlAssistant: true, sessionId: "control", harnessReasoningEffort: "high", harnessMode: "plan",
      mcpServerIds: ["notebook"], includeKnowledge: false, allowAgentMessaging: true,
    }), expect.any(Function), expect.any(AbortSignal)));
    await user.click(screen.getByRole("button", {name: "Butler settings"}));
    expect(await screen.findByRole("combobox", {name: "Butler effort"})).toHaveValue("high");
    await user.selectOptions(screen.getByRole("combobox", {name: "Butler effort"}), "low");
    await user.selectOptions(screen.getByRole("combobox", {name: "Butler cyber access"}), "daybreakBlue");
    await user.click(screen.getByRole("button", {name: "Save"}));
    expect(await screen.findByText("Saved. Applies to Butler’s next message.")).toBeInTheDocument();
    expect(updateChatAssistantConfig).toHaveBeenCalledWith("control", 3,
      {harnessReasoningEffort: "low", harnessCyberAccessProgram: "daybreakBlue"});
    await user.type(screen.getByRole("textbox", {name: "Ask Butler a question"}), "Continue{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenLastCalledWith(expect.objectContaining({
      harnessReasoningEffort: "low", harnessCyberAccessProgram: "daybreakBlue",
    }), expect.any(Function), expect.any(AbortSignal)));
    await user.click(screen.getByRole("button", {name: "Full Assistant settings"}));
    expect(onNavigate).toHaveBeenCalledWith("/projects/project/workbench?view=chat&session=control");
  });

  it("lets Butler choose first-turn settings before a saved conversation exists", async () => {
    const user = userEvent.setup();
    let config = {
      backend: "harness" as const, profileId: "local", model: "model",
      harnessReasoningEffort: undefined as string | undefined,
      harnessCyberAccessProgram: "automatic" as string,
    };
    const getChatAssistantConfig = vi.fn().mockImplementation(async () => ({ sessionId: "control", revision: 1, settings: config }));
    const updateChatAssistantConfig = vi.fn().mockImplementation(async (_id: string, _revision: number, patch: Record<string, unknown>) => {
      config = { ...config, ...patch } as typeof config;
    });
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: {id: "answer", role: "assistant", content: "Ready."},
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null),
        getChatAssistantConfig, updateChatAssistantConfig, streamChat },
      engagement: {id: "project", name: "Project"}, providers: [],
      harnesses: [{id: "local", name: "Local", kind: "codex_app_server", enabled: true, healthy: true,
        localOnly: true, permitsSensitiveData: true, models: ["model"], defaultModel: "model",
        modelOptions: [{model: "model", reasoningEfforts: [{id: "high", label: "High", description: ""}],
          serviceTiers: [], cyberAccessPrograms: ["daybreakBlue"]}]}],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", {name: "Open Butler"}));
    await user.click(screen.getByRole("button", {name: "Butler settings"}));
    await user.selectOptions(screen.getByRole("combobox", {name: "Butler effort"}), "high");
    await user.selectOptions(screen.getByRole("combobox", {name: "Butler cyber access"}), "daybreakBlue");
    await user.click(screen.getByRole("button", {name: "Save"}));
    expect(screen.getByText("Saved for Butler’s first message.")).toBeInTheDocument();
    expect(updateChatAssistantConfig).toHaveBeenCalledWith("control", 1,
      { harnessReasoningEffort: "high", harnessCyberAccessProgram: "daybreakBlue" });
    await user.type(screen.getByRole("textbox", {name: "Ask Butler a question"}), "Start work{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith(expect.objectContaining({
      controlAssistant: true, harnessReasoningEffort: "high", harnessCyberAccessProgram: "daybreakBlue",
    }), expect.any(Function), expect.any(AbortSignal)));
  });

  it("loads saved settings when the panel opens before conversation restore finishes", async () => {
    const user = userEvent.setup();
    const saved = {
      id: "control", engagementId: "project", backend: "provider", providerId: "local-provider", model: "model",
      assistantConfig: {backend: "provider", profileId: "local-provider", model: "model", reasoningEffort: "high"},
    };
    let resolveSession!: (value: typeof saved) => void;
    const pendingSession = new Promise<typeof saved>((resolve) => { resolveSession = resolve; });
    const getControlAssistantSession = vi.fn().mockReturnValue(pendingSession);
    const getChatAssistantConfig = vi.fn().mockResolvedValue({sessionId: "control", revision: 2, settings: saved.assistantConfig});
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler),getControlAssistantSession, getChatAssistantConfig, listChatMessages: vi.fn().mockResolvedValue([])},
      engagement: {id: "project", name: "Project"},
      providers: [{id: "local-provider", name: "Local provider", enabled: true, state: "healthy", healthy: true,
        local: true, permitsSensitiveData: true, models: ["model"], defaultModel: "model"}],
      harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", {name: "Open Butler"}));
    await user.click(screen.getByRole("button", {name: "Butler settings"}));
    expect(screen.getByText("Loading Butler settings…")).toBeInTheDocument();
    resolveSession(saved);
    await waitFor(() => expect(screen.getByRole("combobox", {name: "Butler effort"})).toHaveValue("high"));
    expect(getControlAssistantSession).toHaveBeenCalledWith("project");
    expect(getChatAssistantConfig).toHaveBeenCalledWith("control");
  });

  it("sends provider effort without Codex-only settings after Core readback", async () => {
    const user = userEvent.setup();
    const saved = {
      id: "control", engagementId: "project", backend: "provider", providerId: "local-provider", model: "model",
      assistantConfig: {backend: "provider", profileId: "local-provider", model: "model", reasoningEffort: "high"},
    };
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "provider", providerId: "local-provider", model: "model",
      message: {id: "answer", role: "assistant", content: "Ready."},
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler),getControlAssistantSession: vi.fn().mockResolvedValue(saved),
        getChatAssistantConfig: vi.fn().mockResolvedValue({sessionId: "control", revision: 2,
          settings: {...saved.assistantConfig, harnessCyberAccessProgram: "automatic"}}),
        listChatMessages: vi.fn().mockResolvedValue([]), streamChat},
      engagement: {id: "project", name: "Project"},
      providers: [{id: "local-provider", name: "Local provider", enabled: true, healthy: true,
        local: true, permitsSensitiveData: true, models: ["model"], defaultModel: "model"}],
      harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", {name: "Open Butler"}));
    await user.type(screen.getByRole("textbox", {name: "Ask Butler a question"}), "Check status{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith(expect.objectContaining({
      reasoningEffort: "high", harnessCyberAccessProgram: undefined,
      harnessReasoningEffort: undefined, harnessServiceTier: undefined, harnessMode: undefined,
    }), expect.any(Function), expect.any(AbortSignal)));
  });

  it("uses the same saved Butler chat for successive voice questions", async () => {
    class VoiceChannel extends EventTarget {
      readyState = "open";
      send() {}
      close() { this.readyState = "closed"; }
      emit(event: Record<string, unknown>) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) })); }
    }
    class VoicePeer extends EventTarget {
      static latest: VoicePeer;
      channel = new VoiceChannel();
      iceGatheringState = "complete";
      localDescription = { sdp: "v=0\r\nvoice-offer\r\n" };
      constructor() { super(); VoicePeer.latest = this; }
      createDataChannel() { return this.channel; }
      addTrack() { return {}; }
      async createOffer() { return { type: "offer", sdp: this.localDescription.sdp }; }
      async setLocalDescription() {}
      async setRemoteDescription() {}
      close() {}
    }
    const originalSecure = window.isSecureContext;
    const originalDevices = navigator.mediaDevices;
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn().mockResolvedValue({
      getTracks: () => [{ stop: vi.fn() }], getAudioTracks: () => [{ enabled: true }],
    }) } });
    vi.stubGlobal("RTCPeerConnection", VoicePeer);
    const saved = { id: "control", engagementId: "project", backend: "harness", harnessProfileId: "local", model: "model" };
    // Initial panel restore can still be pending when Voice starts. The first
    // spoken turn must adopt Core's already-saved conversation on its own.
    const getControlAssistantSession = vi.fn().mockResolvedValueOnce(null).mockResolvedValue(saved);
    const getOrCreateControlAssistantSession = vi.fn().mockResolvedValue({ ...saved, isControlAssistant: true });
    const streamChat = vi.fn().mockResolvedValue({ sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: { id: "answer", role: "assistant", content: "I remember." } });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession, getOrCreateControlAssistantSession, streamChat, startVoiceSession: vi.fn().mockResolvedValue({ session_id: "live", sdp: "answer" }),
        getVoiceWorkRoster: vi.fn().mockResolvedValue({ project: { id: "project", name: "Project" }, chats: [], missions: [], more_chats: false, more_missions: false }) },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    try {
      const user = userEvent.setup();
      render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
      await user.click(screen.getByRole("button", { name: "Open Butler" }));
      await user.click(await screen.findByRole("button", { name: "Start Butler Voice" }));
      await waitFor(() => expect(VoicePeer.latest).toBeDefined());
      VoicePeer.latest.channel.emit({ type: "session.input_transcript.delta", delta: "Remember blue.", start_ms: 100, end_ms: 600 });
      VoicePeer.latest.channel.emit({ type: "session.delegation.created", offset_ms: 800, delegation: { id: "first", target: "client" } });
      await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1), { timeout: 3_000 });
      VoicePeer.latest.channel.emit({ type: "session.output_transcript.delta", delta: "I remember.", start_ms: 900, end_ms: 1_200 });
      VoicePeer.latest.channel.emit({ type: "session.input_transcript.delta", delta: "What color?", start_ms: 2_000, end_ms: 2_500 });
      VoicePeer.latest.channel.emit({ type: "session.delegation.created", offset_ms: 2_700, delegation: { id: "second", target: "client" } });
      await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2), { timeout: 3_000 });
      expect(streamChat.mock.calls[0][0]).toMatchObject({ sessionId: "control", messages: [{ role: "user", content: "Remember blue." }] });
      expect(streamChat.mock.calls[1][0]).toMatchObject({ sessionId: "control", messages: [{ role: "user", content: "What color?" }] });
      expect(streamChat.mock.calls[0][0].contextAttachments).toBeUndefined();
      expect(streamChat.mock.calls[1][0].contextAttachments).toBeUndefined();
      expect(getControlAssistantSession).toHaveBeenCalledWith("project");
      expect(getOrCreateControlAssistantSession).toHaveBeenCalledTimes(2);
    } finally {
      Object.defineProperty(window, "isSecureContext", { configurable: true, value: originalSecure });
      Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: originalDevices });
      vi.unstubAllGlobals();
    }
  });

  it("shows a durable chat link from a successful work receipt when the model omits it", async () => {
    const user = userEvent.setup();
    const sessionId = "70d5a305-6f30-4270-b27d-2322d7385fbf";
    const url = `/projects/project/workbench?view=chat&session=${sessionId}`;
    const getControlAssistantSession = vi.fn().mockResolvedValue(null);
    const listChatMessages = vi.fn().mockResolvedValue([
      { id: "question", role: "user", content: "Start an assistant" },
      { id: "answer", role: "assistant", content: "I started it." },
    ]);
    const streamChat = vi.fn().mockImplementation(async (_body: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({ type: "tool_completed", turnId: "turn", toolCallId: "work-call", capability: "nebula.capabilities.change",
        status: "complete", summary: "Started", evidenceIds: [], artifacts: [], step: 0,
        receipt: { session_id: sessionId, url, butler_operation: "nebula.work.start_chat", butler_project_id: "project" } });
      return { sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
        message: { id: "answer", role: "assistant", content: "I started it." } };
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession, listChatMessages, streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onNavigate = vi.fn();
    const mounted = render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "Start an assistant{enter}");
    const link = await screen.findByRole("link", { name: "Open chat" });
    expect(link).toHaveAttribute("href", url);
    await user.click(link);
    expect(onNavigate).toHaveBeenCalledExactlyOnceWith(url);
    getControlAssistantSession.mockResolvedValue({ id: "control", engagementId: "project", backend: "harness", harnessProfileId: "local", model: "model" });
    mounted.unmount();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    expect(await screen.findByRole("link", { name: "Open chat" })).toHaveAttribute("href", url);
  });

  it("keeps one conversation behind the shell launcher without a floating overlay", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()}>
      <header><NebulaAssistantLauncherButton /></header>
    </NebulaControlAssistant></ThemeProvider></DialogProvider>);
    const launcher = screen.getByRole("button", { name: "Open Butler" });
    expect(launcher.closest("header")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Open Butler" })).toHaveLength(1);
    await user.click(launcher);
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "Help me find a setting");
    await user.click(screen.getByRole("button", { name: "Hide Butler" }));
    await user.click(screen.getByRole("button", { name: "Show Butler" }));
    expect(screen.getByRole("textbox", { name: "Ask Butler a question" })).toHaveValue("Help me find a setting");
  });

  it("checks the selected project's incidents before Butler opens and clears its cue on project switch", async () => {
    let openCount = 2;
    const request = vi.fn().mockImplementation(async (path: string) => ({
      project_id: path.includes("project-b") ? "project-b" : "project-a",
      authority: "watch", watched_goals: [], incidents: [], decisions: [], resources: [], coverage_gaps: [],
      summary: { watched_goals: 0, open_incidents: path.includes("project-b") ? 0 : openCount,
        acknowledged_incidents: 0, resolved_incidents: 0, account_resources: 0, harness_quota_resources: 0 },
      capabilities: { authority_levels: ["watch"], incidents: true, decisions: false, agent_reports: true,
        machine_alerts: true, resource_alerts: true, live_events: false },
    }));
    const api = { request, getControlAssistantSession: vi.fn().mockResolvedValue(null) };
    let projectId = "project-a";
    let coreState: "online" | "offline" = "online";
    vi.mocked(useWorkspace).mockImplementation(() => ({ api, coreState,
      engagement: { id: projectId, name: projectId }, providers: [], harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>));
    const view = render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()}>
      <header><NebulaAssistantLauncherButton mobile /></header>
    </NebulaControlAssistant></ThemeProvider></DialogProvider>);
    expect(await screen.findByRole("button", { name: "Open Butler; 2 open incidents need attention in the selected project" })).toBeVisible();
    expect(request).toHaveBeenCalledWith("engagements/project-a/butler/overview", { signal: expect.any(AbortSignal) });

    openCount = 3;
    fireEvent(document, new Event("visibilitychange"));
    expect(await screen.findByRole("button", { name: "Open Butler; 3 open incidents need attention in the selected project" })).toBeVisible();

    coreState = "offline";
    view.rerender(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()}>
      <header><NebulaAssistantLauncherButton mobile /></header>
    </NebulaControlAssistant></ThemeProvider></DialogProvider>);
    expect(screen.getByRole("button", { name: "Open Butler; 3 open incidents need attention in the selected project" })).toBeVisible();
    openCount = 1;
    coreState = "online";
    view.rerender(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()}>
      <header><NebulaAssistantLauncherButton mobile /></header>
    </NebulaControlAssistant></ThemeProvider></DialogProvider>);
    expect(await screen.findByRole("button", { name: "Open Butler; 1 open incident needs attention in the selected project" })).toBeVisible();

    projectId = "project-b";
    view.rerender(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()}>
      <header><NebulaAssistantLauncherButton mobile /></header>
    </NebulaControlAssistant></ThemeProvider></DialogProvider>);
    expect(screen.getByRole("button", { name: "Open Butler" })).toHaveAttribute("title", "Butler");
    await waitFor(() => expect(request).toHaveBeenCalledWith("engagements/project-b/butler/overview", { signal: expect.any(AbortSignal) }));
  });

  it("scrolls the mobile body to a new Butler reply", async () => {
    const user = userEvent.setup();
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn().mockReturnValue({ matches: true, addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() });
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: { id: "answer", role: "assistant", content: "I checked the mission." },
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    try {
      render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
      await user.click(screen.getByRole("button", { name: "Open Butler" }));
      const body = screen.getByRole("log", { name: "Butler conversation" }).parentElement as HTMLElement;
      Object.defineProperty(body, "scrollHeight", { configurable: true, value: 700 });
      await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "Check the mission{enter}");
      expect(await screen.findByText("I checked the mission.")).toBeVisible();
      await waitFor(() => expect(body.scrollTop).toBe(700));
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it("opens from the global icon, applies a device theme, and keeps the setting catalog reachable", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.click(screen.getByText("Settings I can help with"));
    await user.click(screen.getByRole("button", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("nebula.theme")).toBe("light");
    await user.type(screen.getByRole("textbox", { name: "Find a setting" }), "provider");
    await user.click(screen.getByRole("button", { name: /Model providers.*Direct actions available/ }));
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.providers" }));
    expect(screen.getByRole("button", { name: "Show Butler" })).toBeInTheDocument();
  });

  it("changes device themes from natural text requests without a project or model", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    const onCloseSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "change to zero theme");
    expect(screen.getByRole("button", { name: "Send to Butler" })).toBeEnabled();
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "{enter}");
    await waitFor(() => expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.appearance" })));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("zero-dark"));
    expect(localStorage.getItem("nebula.theme")).toBe("zero-dark");
    await waitFor(() => expect(onCloseSetting).toHaveBeenCalled(), { timeout: 4000 });
    expect(screen.getByText("Changed this device to Zero Dark. The choice is saved in this browser.")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "change it to the light theme{enter}");
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(localStorage.getItem("nebula.theme")).toBe("light");
    await waitFor(() => expect(screen.getByText("Changed this device to Light. The choice is saved in this browser.")).toBeInTheDocument(), { timeout: 4000 });
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "change it to the rainbow theme{enter}");
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
        type: "tool_completed", turnId: "turn", toolCallId: "theme-call", capability: "nebula.capabilities.change",
        status: "complete", summary: "Action requested", evidenceIds: [], artifacts: [], step: 0,
        receipt: { state: "pending_browser_apply", butler_operation: "nebula.settings.set_device_theme", butler_project_id: "project", client_action: { kind: "device_theme", theme: "zero-dark" } },
      });
      return { sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
        message: { id: "answer", role: "assistant", content: "Applying Zero Dark on this device." } };
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession, listChatMessages, streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const mounted = render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "could you make the screen look like Nebula Zero?{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.appearance" })));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("zero-dark"));
    await waitFor(() => expect(onCloseSetting).toHaveBeenCalled(), { timeout: 4000 });
    expect(screen.getByText("Changed this device to Zero Dark. The choice is saved in this browser.")).toBeInTheDocument();
    getControlAssistantSession.mockResolvedValue({ id: "control", engagementId: "project", backend: "harness", harnessProfileId: "local", model: "model" });
    mounted.unmount();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat, getAutomationPolicy, updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "could you get this project's commands running on the host for me?{enter}");
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat, request },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onSavedSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} onSavedSetting={onSavedSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "allow all URLs and network targets{enter}");
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat, request, startHeadlessSecuritySession, getHeadlessSecuritySession },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "set project policy to allow all URLs and network targets, then start the headless browser session{enter}");
    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(startHeadlessSecuritySession).toHaveBeenCalledWith("project"));
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({ allow_all_targets: true, bypass_permissions: false });
    await waitFor(() => expect(screen.getByText(/headless security browser: active/)).toBeInTheDocument());
    expect(getHeadlessSecuritySession).toHaveBeenCalledWith("project");
  });

  it("hides, restores, and closes without discarding the draft", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "Help me choose a theme");
    await user.click(screen.getByRole("button", { name: "Hide Butler" }));
    await user.click(screen.getByRole("button", { name: "Show Butler" }));
    expect(screen.getByRole("textbox", { name: "Ask Butler a question" })).toHaveValue("Help me choose a theme");
    await user.click(screen.getByRole("button", { name: "Close Butler" }));
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    expect(screen.getByRole("textbox", { name: "Ask Butler a question" })).toHaveValue("Help me choose a theme");
  });

  it("opens a named setting from Enter through the settings lens callback", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "open MCP servers{enter}");
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.mcp" }));
    expect(screen.getByRole("button", { name: "Show Butler" })).toBeInTheDocument();
  });

  it("opens All projects immediately from 'show me my projects', without a model or selected project", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "show me my projects{enter}");
    expect(onNavigate).toHaveBeenCalledExactlyOnceWith("/projects");
    expect(screen.getByRole("button", { name: "Show Butler" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show Butler" }));
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onNavigate = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "locate the place with everything I've built{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onNavigate).toHaveBeenCalledExactlyOnceWith("/projects"));
  });

  it("keeps the catalog available from a navigation prompt without a model", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "open pineapple moon");
    const send = screen.getByRole("button", { name: "Send to Butler" });
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "Why can't my assistant edit a host file in Docker mode?{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    expect(streamChat.mock.calls[0][0]).toMatchObject({ controlAssistant: true });
    expect(onOpenSetting).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("link", { name: "Open Project Policy" }));
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.network-scope" }));
    expect(screen.getByRole("button", { name: "Show Butler" })).toBeInTheDocument();
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null), getAutomationPolicy, updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [], harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    const onOpenSetting = vi.fn();
    const onCloseSetting = vi.fn();
    const onSavedSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} onCloseSetting={onCloseSetting} onSavedSetting={onSavedSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
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
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null),
        getAutomationPolicy: vi.fn().mockResolvedValue({ executionMode: "docker", revision: 3 }), updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [], harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} onCloseSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Butler a question" }), "open project policy and change from dockermode to host mode{enter}");
    expect(await screen.findByRole("heading", { name: "Switch Project to Host mode?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(updateAutomationPolicy).not.toHaveBeenCalled();
    expect(await screen.findByText("Project remains in Docker mode. No change was saved.")).toBeInTheDocument();
  });

  it("does not change mode when the requested source mode differs from Core", async () => {
    const user = userEvent.setup();
    const updateAutomationPolicy = vi.fn();
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(savedButler), getControlAssistantSession: vi.fn().mockResolvedValue(null),
        getAutomationPolicy: vi.fn().mockResolvedValue({ executionMode: "host" }), updateAutomationPolicy },
      engagement: { id: "project", name: "Project" }, providers: [], harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} onCloseSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "open project policy and change from dockermode to host mode{enter}");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("already in Host mode"));
    expect(updateAutomationPolicy).not.toHaveBeenCalled();
    expect(composer).toHaveValue("open project policy and change from dockermode to host mode");
  });
});
