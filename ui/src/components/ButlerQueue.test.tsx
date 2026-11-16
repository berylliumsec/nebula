import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { WorkbenchDraftProvider } from "../state/WorkbenchDraftContext";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatStreamEvent } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { ThemeProvider } from "../state/ThemeContext";
import { DialogProvider } from "./DialogSystem";
import { NebulaControlAssistant } from "./NebulaControlAssistant";
import { waitingQueueItems } from "./ButlerQueue";
import type { ChatQueue, QueueItem } from "../pages/useChatQueue";

vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: vi.fn() }));

const butler = { id: "control", engagementId: "project", backend: "provider" as const, providerId: "local-provider", model: "model", isControlAssistant: true };
const providers = [{ id: "local-provider", name: "Local provider", enabled: true, state: "healthy", healthy: true,
  local: true, permitsSensitiveData: true, models: ["model"], modelAllowlist: ["model"], defaultModel: "model" }];
const config = { backend: "provider", profileId: "local-provider", model: "model", allowSubagents: false };

function item(overrides: Partial<QueueItem>): QueueItem {
  return { id: "q1", key: "k1", status: "queued", request: { messages: [{ role: "user", content: "Then pause the Fuzz parser goal." }] }, ...overrides };
}

/** An in-memory Core queue that answers the same routes the panel calls. */
function coreQueue(initial: ChatQueue = { revision: 1, paused: false, items: [] }) {
  let state = initial;
  const writes: Record<string, unknown>[] = [];
  const request = vi.fn(async (path: string, init?: { method?: string; body?: string }) => {
    expect(path).toBe("chat/sessions/control/queue");
    if (init?.method === "POST") {
      const body = JSON.parse(init.body ?? "{}") as Record<string, unknown>;
      writes.push(body);
      if (body.action === "enqueue") state = { ...state, revision: state.revision + 1, items: [...state.items, item({ id: `q${state.items.length + 1}`, request: body.request as QueueItem["request"] })] };
      if (body.action === "remove") state = { ...state, revision: state.revision + 1, items: state.items.filter((entry) => entry.id !== body.item_id) };
      if (body.action === "pause" || body.action === "resume") state = { ...state, revision: state.revision + 1, paused: body.action === "pause" };
      if (body.action === "retry") state = { ...state, revision: state.revision + 1, items: state.items.map((entry) => entry.id === body.item_id ? { ...entry, status: "queued", detail: undefined } : entry) };
    }
    return state;
  });
  const requestIfChanged = vi.fn(async () => ({ value: state, etag: String(state.revision) }));
  return { request, requestIfChanged, writes, set: (next: ChatQueue) => { state = next; } };
}

function renderButler(api: Record<string, unknown>, onOpenSetting = vi.fn()) {
  vi.mocked(useWorkspace).mockReturnValue({
    api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(butler), getControlAssistantSession: vi.fn().mockResolvedValue(butler),
      getChatAssistantConfig: vi.fn().mockResolvedValue({ sessionId: "control", revision: 2, settings: config }),
      listChatMessages: vi.fn().mockResolvedValue([]), ...api },
    engagement: { id: "project", name: "Project" }, providers, harnesses: [], coreState: "online",
  } as unknown as ReturnType<typeof useWorkspace>);
  render(<DialogProvider><ThemeProvider><NebulaControlAssistant onNavigate={vi.fn()} onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
}

describe("Butler queue", () => {
  beforeEach(() => localStorage.clear());

  it("lists only messages Core has not sent", () => {
    expect(waitingQueueItems([item({ id: "a" }), item({ id: "b", status: "sending", turn_id: "t" }), item({ id: "c", status: "complete" }),
      item({ id: "d", status: "needs_review" }), item({ id: "e", status: "claiming" })]).map((entry) => entry.id)).toEqual(["a", "d", "e"]);
  });

  it("queues a message in Core while Butler answers, then lets the operator remove it", async () => {
    const user = userEvent.setup();
    const queue = coreQueue();
    let finish!: () => void;
    const streamChat = vi.fn().mockImplementation((_request: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({ type: "started", model: "model", turnId: "turn", sessionId: "control" } as ChatStreamEvent);
      return new Promise((resolve) => { finish = () => resolve({ sessionId: "control", backend: "provider", providerId: "local-provider", model: "model", message: { id: "answer", role: "assistant", content: "Summary ready." } }); });
    });
    renderButler({ ...queue, streamChat });
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
    await user.type(composer, "Summarize yesterday’s findings.{enter}");
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "Queue a message for Butler…"));

    await user.type(composer, "Then pause the Fuzz parser goal.{enter}");
    await waitFor(() => expect(queue.writes).toHaveLength(1));
    expect(String(queue.writes[0].idempotency_key)).toMatch(/^butler-/);
    expect(JSON.parse(localStorage.getItem("nebula.butler.queue.control") ?? "{}").keys).toEqual([queue.writes[0].idempotency_key]);
    expect(queue.writes[0]).toMatchObject({ action: "enqueue", expected_revision: 1,
      request: { control_assistant: true, session_id: "control", engagement_id: "project", messages: [{ role: "user", content: "Then pause the Fuzz parser goal." }] } });
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(composer).toHaveValue("");
    const list = await screen.findByRole("region", { name: "Butler queue" });
    expect(within(list).getByRole("status")).toHaveTextContent("1 queued · sent in order when Butler is free");
    expect(within(list).getByText("Then pause the Fuzz parser goal.")).toBeInTheDocument();

    await user.click(within(list).getByRole("button", { name: "Remove queued message 1" }));
    await waitFor(() => expect(queue.writes.at(-1)).toMatchObject({ action: "remove", item_id: "q1" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Butler queue" })).not.toBeInTheDocument());
    act(() => finish());
    expect(await screen.findByText("Summary ready.")).toBeInTheDocument();
  });

  it("queues two quick messages in the order they were sent", async () => {
    const user = userEvent.setup();
    const queue = coreQueue();
    const streamChat = vi.fn().mockImplementation((_request: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      onEvent({ type: "started", model: "model", turnId: "turn", sessionId: "control" } as ChatStreamEvent);
      return new Promise(() => undefined);
    });
    renderButler({ ...queue, streamChat });
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const composer = screen.getByRole("textbox", { name: "Ask Butler a question" });
    await user.type(composer, "First request.{enter}");
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "Queue a message for Butler…"));
    await user.type(composer, "Second request.{enter}");
    await user.type(composer, "Third request.{enter}");
    await waitFor(() => expect(queue.writes).toHaveLength(2));
    expect(queue.writes.map((write) => (write.request as { messages: { content: string }[] }).messages[0].content)).toEqual(["Second request.", "Third request."]);
    expect(composer).toHaveValue("");
    const list = await screen.findByRole("region", { name: "Butler queue" });
    await waitFor(() => expect(within(list).getByRole("status")).toHaveTextContent("2 queued"));
  });

  it("does not attach the running message's selected context to a queued one", async () => {
    const user = userEvent.setup();
    const queue = coreQueue();
    const streamChat = vi.fn()
      .mockResolvedValueOnce({ sessionId: "control", backend: "provider", providerId: "local-provider", model: "model", message: { id: "a1", role: "assistant", content: "The goal needs another turn." } })
      .mockImplementationOnce((_request: unknown, onEvent: (event: ChatStreamEvent) => void) => {
        onEvent({ type: "started", model: "model", turnId: "turn-2", sessionId: "control" } as ChatStreamEvent);
        return new Promise(() => undefined);
      });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getOrCreateControlAssistantSession: vi.fn().mockResolvedValue(butler), getControlAssistantSession: vi.fn().mockResolvedValue(butler),
        getChatAssistantConfig: vi.fn().mockResolvedValue({ sessionId: "control", revision: 2, settings: config }),
        listChatMessages: vi.fn().mockResolvedValue([]), streamChat, ...queue },
      engagement: { id: "project", name: "Project" }, providers, harnesses: [],
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
    await user.type(composer, "Use that detail.{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2));
    expect(streamChat.mock.calls[1][0].contextAttachments).toHaveLength(1);
    await waitFor(() => expect(composer).toHaveAttribute("placeholder", "Queue a message for Butler…"));
    await user.type(composer, "Then check the queue.{enter}");
    await waitFor(() => expect(queue.writes).toHaveLength(1));
    expect((queue.writes[0].request as Record<string, unknown>).context_attachments ?? []).toEqual([]);
  });

  it("follows a queued response Core sent and applies its browser action", async () => {
    const onOpenSetting = vi.fn();
    const queue = coreQueue({ revision: 3, paused: false, items: [item({ status: "sending", turn_id: "queued-turn", request: { messages: [{ role: "user", content: "Open appearance settings." }] } })] });
    const getPendingChatTurn = vi.fn().mockResolvedValueOnce({ id: "queued-turn", sessionId: "control", status: "running", revision: 1, queuedAt: "2026-11-16T10:00:00Z" })
      .mockResolvedValue(undefined);
    const followChatTurn = vi.fn().mockImplementation(async (turnId: string, _fallback: unknown, onEvent: (event: ChatStreamEvent) => void) => {
      expect(turnId).toBe("queued-turn");
      onEvent({ type: "tool_completed", turnId, toolCallId: "open-call", capability: "nebula.settings.open", status: "complete", summary: "Action requested",
        evidenceIds: [], artifacts: [], step: 0, receipt: { state: "pending_browser_apply", client_action: { kind: "open_setting", setting_id: "appearance" } } } as ChatStreamEvent);
      return { sessionId: "control", backend: "provider", providerId: "local-provider", model: "model", message: { id: "queued-answer", role: "assistant", content: "Opened Appearance." } };
    });
    // This device queued the message.
    localStorage.setItem("nebula.butler.queue.control", JSON.stringify({ keys: ["k1"], followed: [] }));
    // Core's saved transcript gains the queued message and its reply once the turn ends.
    const listChatMessages = vi.fn().mockImplementation(async () => followChatTurn.mock.calls.length
      ? [{ id: "queued-user", role: "user", content: "Open appearance settings." }, { id: "queued-answer", role: "assistant", content: "Opened Appearance." }]
      : []);
    renderButler({ ...queue, getPendingChatTurn, followChatTurn, listChatMessages }, onOpenSetting);
    await userEvent.setup().click(screen.getByRole("button", { name: "Open Butler" }));
    await waitFor(() => expect(followChatTurn).toHaveBeenCalledTimes(1));
    expect(JSON.parse(localStorage.getItem("nebula.butler.queue.control") ?? "{}").followed).toEqual(["queued-turn"]);
    await waitFor(() => expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.appearance" })));
    expect(await screen.findByText("Opened Appearance.")).toBeInTheDocument();
    expect(screen.getByText("Open appearance settings.")).toBeInTheDocument();
  });

  it("does not follow or apply a queued message another device added", async () => {
    const onOpenSetting = vi.fn();
    const queue = coreQueue({ revision: 3, paused: false, items: [item({ status: "complete", turn_id: "other-turn", request: undefined })] });
    const followChatTurn = vi.fn();
    renderButler({ ...queue, followChatTurn }, onOpenSetting);
    await userEvent.setup().click(screen.getByRole("button", { name: "Open Butler" }));
    await waitFor(() => expect(queue.requestIfChanged).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(followChatTurn).not.toHaveBeenCalled();
    expect(onOpenSetting).not.toHaveBeenCalled();
  });

  it("reads an older finished queued reply from the transcript without replaying its actions", async () => {
    const onOpenSetting = vi.fn();
    // This device queued it in an earlier page; Core finished it while no panel followed.
    localStorage.setItem("nebula.butler.queue.control", JSON.stringify({ keys: ["k1"], followed: [] }));
    const queue = coreQueue({ revision: 4, paused: false, items: [item({ status: "complete", turn_id: "old-turn", request: undefined })] });
    const followChatTurn = vi.fn();
    const listChatMessages = vi.fn().mockResolvedValue([{ id: "old-answer", role: "assistant", content: "Opened Appearance earlier." }]);
    renderButler({ ...queue, followChatTurn, listChatMessages, getPendingChatTurn: vi.fn().mockResolvedValue(undefined) }, onOpenSetting);
    await userEvent.setup().click(screen.getByRole("button", { name: "Open Butler" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("nebula.butler.queue.control") ?? "{}").followed).toEqual(["old-turn"]));
    expect(await screen.findByText("Opened Appearance earlier.")).toBeInTheDocument();
    expect(followChatTurn).not.toHaveBeenCalled();
    expect(onOpenSetting).not.toHaveBeenCalled();
  });

  it("shows a message Core could not send, and retries or resumes only on request", async () => {
    const user = userEvent.setup();
    const queue = coreQueue({ revision: 5, paused: true, items: [item({ status: "needs_review", detail: "Butler's settings changed before it was sent" })] });
    renderButler({ ...queue });
    await user.click(screen.getByRole("button", { name: "Open Butler" }));
    const list = await screen.findByRole("region", { name: "Butler queue" });
    expect(list).toHaveAttribute("data-state", "attention");
    expect(within(list).getByRole("status")).toHaveTextContent("Queue paused · 1 needs attention");
    expect(within(list).getByText(/Needs review · Butler's settings changed/)).toBeInTheDocument();
    expect(queue.writes).toEqual([]);
    await user.click(within(list).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(queue.writes.at(-1)).toMatchObject({ action: "retry", item_id: "q1" }));
    await user.click(within(list).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(queue.writes.at(-1)).toMatchObject({ action: "resume" }));
  });
});
