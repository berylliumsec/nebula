import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage, ChatSubagentView } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { ThemeProvider } from "../state/ThemeContext";
import { DialogProvider } from "./DialogSystem";
import { NebulaControlAssistant } from "./NebulaControlAssistant";
import { butlerActivityRows, hasUnreadReport } from "./ButlerSubagentActivity";

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: vi.fn() }));

function subagent(overrides: Partial<ChatSubagentView>): ChatSubagentView {
  return {
    id: "sub-1", name: "Pause goals", task: "Pause every running goal.", status: "running",
    parentSessionId: "control", parentTurnId: "turn", parentBackend: "provider", childSessionId: "child-1",
    stepCount: 2, recentSteps: [], usage: { inputTokens: 10, outputTokens: 5, totalTokens: 15 },
    startedAt: "2026-11-16T10:00:00Z", elapsedSeconds: 4, result: "",
    ...overrides,
  };
}

const butler = {
  id: "control", engagementId: "project", backend: "provider" as const, providerId: "local-provider",
  model: "model", isControlAssistant: true,
};
const providers = [{ id: "local-provider", name: "Local provider", enabled: true, state: "healthy", healthy: true,
  local: true, permitsSensitiveData: true, models: ["model"], modelAllowlist: ["model"], defaultModel: "model" }];
const config = { backend: "provider", profileId: "local-provider", model: "model", allowSubagents: true };

function renderButler(api: Record<string, unknown>, onNavigate = vi.fn()) {
  vi.mocked(useWorkspace).mockReturnValue({
    api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(butler), getControlAssistantSession: vi.fn().mockResolvedValue(butler), ...api },
    engagement: { id: "project", name: "Project" }, providers, harnesses: [],
  } as unknown as ReturnType<typeof useWorkspace>);
  render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={onNavigate} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
}

describe("Butler subagents", () => {
  beforeEach(() => localStorage.clear());

  it("lists running children first and keeps finished ones until the next request", () => {
    const running = subagent({ id: "a", status: "running" });
    const done = subagent({ id: "b", status: "completed" });
    const old = subagent({ id: "c", status: "completed" });
    expect(butlerActivityRows([done, old, running], new Set(["a", "b"])).map((item) => item.id)).toEqual(["a", "b"]);
    expect(hasUnreadReport([subagent({ resultMessageId: "report" })], new Set(["answer"]))).toBe(true);
    expect(hasUnreadReport([subagent({ resultMessageId: "report" })], new Set(["report"]))).toBe(false);
  });

  it("saves the delegation choice and running-at-once limit for a provider Butler", async () => {
    const user = userEvent.setup();
    let saved = { ...config };
    const getChatAssistantConfig = vi.fn().mockImplementation(async () => ({ sessionId: "control", revision: 2, settings: saved }));
    const updateChatAssistantConfig = vi.fn().mockImplementation(async (_id: string, _revision: number, patch: Record<string, unknown>) => {
      saved = { ...saved, ...patch } as typeof saved;
    });
    renderButler({ getChatAssistantConfig, updateChatAssistantConfig, listChatMessages: vi.fn().mockResolvedValue([]),
      listChatSubagentsIfChanged: vi.fn().mockResolvedValue({ items: [] }) });
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.click(screen.getByRole("button", { name: "Butler settings" }));
    const toggle = await screen.findByRole("checkbox", { name: /Delegate longer work to subagents/ });
    await waitFor(() => expect(toggle).toBeChecked());

    await user.type(screen.getByRole("spinbutton", { name: "Running at once" }), "3");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateChatAssistantConfig).toHaveBeenLastCalledWith("control", 2,
      { reasoningEffort: null, allowSubagents: true, maxActiveSubagents: 3 }));
    expect(await screen.findByText("Saved. Applies to Butler’s next message.")).toBeInTheDocument();

    await user.click(toggle);
    expect(screen.queryByRole("spinbutton", { name: "Running at once" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateChatAssistantConfig).toHaveBeenLastCalledWith("control", 2,
      { reasoningEffort: null, allowSubagents: false, maxActiveSubagents: null }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Delegate longer work to subagents/ })).not.toBeChecked());
  });

  it("shows Core's default delegation before Butler's first conversation exists", async () => {
    const user = userEvent.setup();
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(butler), getControlAssistantSession: vi.fn().mockResolvedValue(null),
        listChatMessages: vi.fn().mockResolvedValue([]), listChatSubagentsIfChanged: vi.fn().mockResolvedValue({ items: [] }) },
      engagement: { id: "project", name: "Project" }, providers, harnesses: [],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    await user.click(screen.getByRole("button", { name: "Butler settings" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Delegate longer work to subagents/ })).toBeChecked());
  });

  it("shows working subagents and adds a report Core posts after Butler replied", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    const history: ChatMessage[] = [
      { id: "ask", role: "user", content: "Pause every running goal." },
      { id: "answer", role: "assistant", content: "Started a subagent to pause the goals." },
    ];
    const report: ChatMessage = { id: "report", role: "assistant", content: "Pause goals completed\n\nPaused 3 goals.",
      metadata: { kind: "subagent_result", subagent_status: "completed" } };
    let finished = false;
    const listChatMessages = vi.fn().mockImplementation(async () => finished ? [...history, report] : history);
    const listChatSubagentsIfChanged = vi.fn().mockImplementation(async () => ({ items: finished
      ? [subagent({ status: "completed", result: "Paused 3 goals.", resultMessageId: "report", finishedAt: "2026-11-16T10:00:09Z" })]
      : [subagent({})] }));
    renderButler({ listChatMessages, listChatSubagentsIfChanged,
      getChatAssistantConfig: vi.fn().mockResolvedValue({ sessionId: "control", revision: 2, settings: config }) }, onNavigate);
    await user.click(screen.getByRole("button", { name: "Open Butler" }));

    const strip = await screen.findByRole("region", { name: "Butler subagents" });
    expect(within(strip).getByRole("status")).toHaveTextContent("1 subagent working");
    expect(within(strip).getByText("Pause goals")).toBeInTheDocument();
    expect(within(strip).getByText("Running · 2 steps")).toBeInTheDocument();
    // The composer stays usable while the child works.
    expect(screen.getByRole("textbox", { name: "Ask Butler a question" })).toBeEnabled();

    finished = true;
    await waitFor(() => expect(within(screen.getByRole("log", { name: "Butler conversation" })).getByText(/Paused 3 goals\./)).toBeInTheDocument(), { timeout: 6_000 });
    expect(within(strip).getByRole("status")).toHaveTextContent("Subagent finished");
    expect(within(strip).getByText("Finished")).toBeInTheDocument();
    // Only the new report was added; the shown conversation was not replaced.
    expect(screen.getByText("Started a subagent to pause the goals.")).toBeInTheDocument();

    await user.click(within(strip).getByRole("link", { name: "Subagents" }));
    expect(onNavigate).toHaveBeenCalledWith("/projects/project/workbench?view=chat&session=control&drawer=subagents");
  }, 10_000);
});
