import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { ButlerModePage } from "./ButlerModePage";

/*
 * Sending in Butler mode shows the operator's message at once. Core still
 * reads Butler's settings and reserves the thread before the response starts,
 * but the composer must not sit on the typed text while it does.
 */

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const project = { id: "p1", name: "Research" };
const provider = { id: "prov", name: "Local model", local: true, permitsSensitiveData: false, capabilities: [] };
const config = { backend: "provider", profileId: "prov", model: "m", toolsEnabled: false };

const requestIfChanged = vi.fn();
const getControlAssistantSession = vi.fn();
const getChatAssistantConfig = vi.fn();
const getOrCreateControlAssistantSession = vi.fn();
const streamChat = vi.fn();
const listChatMessages = vi.fn();
const cancelChatTurn = vi.fn();
const getPendingChatTurn = vi.fn();
const request = vi.fn();
const api = { requestIfChanged, request, getControlAssistantSession, getChatAssistantConfig, getOrCreateControlAssistantSession, streamChat, listChatMessages, cancelChatTurn, getPendingChatTurn };

// Core's follow-up queue for the open thread, as the page reads and writes it.
let threadQueue: { revision: number; paused: boolean; items: Array<Record<string, unknown>> };
const queueWrites: Array<Record<string, unknown>> = [];
function queueAnswer(path: string) {
  return path.includes("/queue") ? { value: threadQueue, etag: `"q${threadQueue.revision}"` } : undefined;
}

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api, coreState: "online", engagement: project, engagements: [project], providers: [provider], harnesses: [], selectEngagement: vi.fn() }),
}));
vi.mock("../state/ButlerPanelBridge", () => ({ useButlerPanelBridge: () => null }));
vi.mock("../components/DialogSystem", () => ({ useConfirmation: () => vi.fn() }));
vi.mock("../hooks/useCompactLayout", () => ({ useCompactLayout: () => false, isNebulaShell: () => false }));
vi.mock("../components/NebulaVoice", () => ({ MicrophoneIcon: () => null }));
vi.mock("../components/ButlerOversight", () => ({ ButlerOversight: () => null }));
vi.mock("../components/ButlerMemory", () => ({ ButlerMemory: () => null }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  threadQueue = { revision: 0, paused: false, items: [] };
  queueWrites.length = 0;
  request.mockImplementation(async (path: string, init?: { method?: string; body?: string }) => {
    if (!path.includes("/queue")) throw new Error(`unexpected request ${path}`);
    if (init?.method === "POST") {
      const body = JSON.parse(init.body ?? "{}") as Record<string, unknown>;
      queueWrites.push(body);
      const request = body.request as { messages: unknown[] };
      const item = { id: `item-${queueWrites.length}`, key: body.idempotency_key, status: "queued", request };
      threadQueue = { revision: threadQueue.revision + 1, paused: false, items: body.interrupt ? [item, ...threadQueue.items] : [...threadQueue.items, item] };
    }
    return threadQueue;
  });
  requestIfChanged.mockImplementation(async (path: string) => {
    const queued = queueAnswer(path);
    if (queued) return queued;
    if (path.startsWith("butler/threads")) return { value: { items: [] }, etag: '"t"' };
    return { value: { counts: { active: 0, idle: 0, completed: 0 }, states: { waiting: 0, working: 0, failed: 0 }, projects: [], items: [], next_offset: null, truncated: false }, etag: '"b"' };
  });
  getChatAssistantConfig.mockResolvedValue({ settings: config });
  listChatMessages.mockResolvedValue([]);
  streamChat.mockResolvedValue(undefined);
});

const mount = () => render(<MemoryRouter initialEntries={["/butler?project=p1"]}><ButlerModePage /></MemoryRouter>);
const log = () => screen.getByRole("log", { name: "Butler thread messages" });

it("shows the message and clears the composer before Core reserves the thread", async () => {
  const current = deferred<{ id: string }>();
  getControlAssistantSession.mockReturnValue(current.promise);
  const reserved = deferred<{ id: string }>();
  getOrCreateControlAssistantSession.mockReturnValue(reserved.promise);
  // The response keeps streaming while the hand-off is checked.
  streamChat.mockReturnValue(new Promise(() => undefined));
  mount();
  const composer = screen.getByRole("textbox", { name: "Message Butler" });
  await userEvent.type(composer, "What needs me today?{Enter}");

  // Nothing from Core has answered yet, and the message is already shown.
  expect(composer).toHaveValue("");
  expect(within(log()).getByText("What needs me today?")).toBeVisible();
  expect(within(log()).getByText(/Starting Butler/)).toBeVisible();
  expect(streamChat).not.toHaveBeenCalled();

  current.resolve({ id: "butler" });
  reserved.resolve({ id: "thread-1" });
  await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
  expect(streamChat.mock.calls[0][0]).toMatchObject({ sessionId: "thread-1", messages: [{ role: "user", content: "What needs me today?" }] });
  // The reserved thread's log re-renders after the send starts; the message shows once there.
  await waitFor(() => expect(within(log()).getAllByText("What needs me today?")).toHaveLength(1));
  expect(within(log()).queryByText(/Starting Butler/)).not.toBeInTheDocument();
});

it("puts the message back in the composer when Core cannot start the thread", async () => {
  const current = deferred<{ id: string }>();
  getControlAssistantSession.mockReturnValue(current.promise);
  mount();
  const composer = screen.getByRole("textbox", { name: "Message Butler" });
  await userEvent.type(composer, "Pause the fuzzers{Enter}");
  expect(composer).toHaveValue("");

  current.reject(new Error("Core is restarting."));
  await waitFor(() => expect(composer).toHaveValue("Pause the fuzzers"));
  expect(screen.getByRole("alert")).toHaveTextContent("Core is restarting.");
  expect(within(log()).queryByText("Pause the fuzzers")).not.toBeInTheDocument();
  expect(streamChat).not.toHaveBeenCalled();
});

it("keeps what the operator typed meanwhile when the send fails", async () => {
  const current = deferred<{ id: string }>();
  getControlAssistantSession.mockReturnValue(current.promise);
  mount();
  const composer = screen.getByRole("textbox", { name: "Message Butler" });
  await userEvent.type(composer, "First question{Enter}");
  await userEvent.type(composer, "Second thought");

  current.reject(new Error("Core is restarting."));
  await waitFor(() => expect(composer).toHaveValue("First question\n\nSecond thought"));
});

it("replies in the thread a question just started once Butler has answered it", async () => {
  let threadState = "working";
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) {
      return { value: { items: [{ session_id: "thread-1", title: "What needs me today?", project, state: threadState, updated_at: new Date().toISOString() }] }, etag: `"t-${threadState}"` };
    }
    return { value: { counts: { active: 0, idle: 0, completed: 0 }, states: { waiting: 0, working: 0, failed: 0 }, projects: [], items: [], next_offset: null, truncated: false }, etag: '"b"' };
  });
  getControlAssistantSession.mockResolvedValue({ id: "butler" });
  getOrCreateControlAssistantSession.mockResolvedValue({ id: "thread-1" });
  const answer = deferred<void>();
  streamChat.mockReturnValueOnce(answer.promise);
  mount();
  const composer = screen.getByRole("textbox", { name: "Message Butler" });
  await userEvent.type(composer, "What needs me today?{Enter}");
  await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));

  // While Butler answers, the next question can still start its own thread.
  expect(screen.getByRole("button", { name: "New thread" })).toHaveAttribute("aria-pressed", "true");

  threadState = "idle";
  answer.resolve();
  // Once the answer settles, the composer continues the open thread.
  await waitFor(() => expect(screen.getByRole("button", { name: "Reply in this thread" })).toHaveAttribute("aria-pressed", "true"), { timeout: 3000 });
  await userEvent.type(composer, "And tomorrow?{Enter}");
  await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2));
  expect(getOrCreateControlAssistantSession).toHaveBeenCalledTimes(1);
  expect(streamChat.mock.calls[1][0]).toMatchObject({ sessionId: "thread-1", messages: [{ role: "user", content: "And tomorrow?" }] });
});

it("replies in a thread opened from the address, such as after a reload", async () => {
  requestIfChanged.mockImplementation(async (path: string) => {
    if (path.startsWith("butler/threads")) return { value: { items: [{ session_id: "thread-1", title: "Earlier", project, state: "idle", updated_at: new Date().toISOString() }] }, etag: '"t"' };
    return { value: { counts: { active: 0, idle: 0, completed: 0 }, states: { waiting: 0, working: 0, failed: 0 }, projects: [], items: [], next_offset: null, truncated: false }, etag: '"b"' };
  });
  getControlAssistantSession.mockResolvedValue({ id: "butler" });
  render(<MemoryRouter initialEntries={["/butler?project=p1&thread=thread-1"]}><ButlerModePage /></MemoryRouter>);
  await waitFor(() => expect(screen.getByRole("button", { name: "Reply in this thread" })).toHaveAttribute("aria-pressed", "true"));
  await userEvent.type(screen.getByRole("textbox", { name: "Message Butler" }), "Still there?{Enter}");
  await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
  expect(getOrCreateControlAssistantSession).not.toHaveBeenCalled();
  expect(streamChat.mock.calls[0][0]).toMatchObject({ sessionId: "thread-1" });
});

it("stops a harness-backed thread by its chat turn after a tool step ran", async () => {
  getControlAssistantSession.mockResolvedValue({ id: "butler" });
  getOrCreateControlAssistantSession.mockResolvedValue({ id: "thread-1" });
  cancelChatTurn.mockResolvedValue({ id: "chat-turn", status: "cancelled" });
  const thread = { session_id: "thread-1", title: "Check the fuzzers", project, state: "working", turn_id: null, updated_at: "2026-11-26T00:00:00Z" };
  requestIfChanged.mockImplementation(async (path: string) => {
    const queued = queueAnswer(path);
    if (queued) return queued;
    if (path.startsWith("butler/threads")) return { value: { items: [thread] }, etag: '"t1"' };
    return { value: { counts: { active: 0, idle: 0, completed: 0 }, states: { waiting: 0, working: 0, failed: 0 }, projects: [], items: [], next_offset: null, truncated: false }, etag: '"b"' };
  });
  // A harness names its chat turn only on "started"; its tool frames carry the harness turn.
  streamChat.mockImplementation((_request: unknown, onEvent: (event: unknown) => void) => {
    onEvent({ type: "started", turnId: "chat-turn", harnessTurnId: "harness-turn", model: "claude" });
    onEvent({ type: "tool_started", turnId: "harness-turn", toolCallId: "call-1", capability: "Read", arguments: {}, step: 0 });
    return new Promise(() => undefined);
  });
  mount();
  await userEvent.type(screen.getByRole("textbox", { name: "Message Butler" }), "Check the fuzzers{Enter}");
  await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
  await userEvent.click(await screen.findByRole("button", { name: "Reply in this thread" }));
  await userEvent.click(await screen.findByRole("button", { name: "Stop response" }));

  await waitFor(() => expect(cancelChatTurn).toHaveBeenCalledWith("chat-turn"));
  expect(cancelChatTurn).not.toHaveBeenCalledWith("harness-turn");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

let finishStream: () => void = () => undefined;
/** A thread Butler is still answering, opened and set to reply. */
async function answeringThread() {
  getControlAssistantSession.mockResolvedValue({ id: "butler" });
  getOrCreateControlAssistantSession.mockResolvedValue({ id: "thread-1" });
  cancelChatTurn.mockResolvedValue({ id: "chat-turn", status: "cancelled" });
  const thread = { session_id: "thread-1", title: "Check the fuzzers", project, state: "working", turn_id: "chat-turn", updated_at: "2026-11-26T00:00:00Z" };
  requestIfChanged.mockImplementation(async (path: string) => {
    const queued = queueAnswer(path);
    if (queued) return queued;
    if (path.startsWith("butler/threads")) return { value: { items: [thread] }, etag: '"t1"' };
    return { value: { counts: { active: 0, idle: 0, completed: 0 }, states: { waiting: 0, working: 0, failed: 0 }, projects: [], items: [], next_offset: null, truncated: false }, etag: '"b"' };
  });
  streamChat.mockImplementation((_request: unknown, onEvent: (event: unknown) => void) => {
    onEvent({ type: "started", turnId: "chat-turn", model: "m" });
    return new Promise((resolve) => { finishStream = () => resolve(undefined); });
  });
  mount();
  await userEvent.type(screen.getByRole("textbox", { name: "Message Butler" }), "Check the fuzzers{Enter}");
  await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
  await userEvent.click(await screen.findByRole("button", { name: "Reply in this thread" }));
  return screen.getByRole("textbox", { name: "Message Butler" });
}

it("queues a reply in Core while Butler is still answering the thread", async () => {
  const composer = await answeringThread();
  // Nothing typed: the only action is Stop.
  expect(screen.getByRole("button", { name: "Stop response" })).toBeVisible();
  await userEvent.type(composer, "Also check the parser goal{Enter}");

  await waitFor(() => expect(queueWrites).toHaveLength(1));
  expect(queueWrites[0]).toMatchObject({ action: "enqueue", interrupt: false, request: { session_id: "thread-1", control_assistant: true, messages: [{ role: "user", content: "Also check the parser goal" }] } });
  expect(composer).toHaveValue("");
  const queue = await screen.findByRole("region", { name: "Thread queue" });
  expect(within(queue).getByText("Also check the parser goal")).toBeVisible();
  expect(within(queue).getByRole("status")).toHaveTextContent("1 queued · sent in order when Butler finishes this answer");
  // Queueing does not stop the running answer, and nothing else starts.
  expect(cancelChatTurn).not.toHaveBeenCalled();
  expect(streamChat).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("sends a reply now by putting it first in Core's queue and stopping the answer", async () => {
  const composer = await answeringThread();
  await userEvent.type(composer, "Stop, look at auth instead");
  expect(screen.getByRole("button", { name: "Queue reply" })).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Send now" }));

  await waitFor(() => expect(cancelChatTurn).toHaveBeenCalledWith("chat-turn"));
  expect(queueWrites[0]).toMatchObject({ action: "enqueue", interrupt: true });
  // The stop follows the enqueue, so Core already knows to send this next.
  expect(request.mock.invocationCallOrder.at(-1)!).toBeLessThan(cancelChatTurn.mock.invocationCallOrder[0]);
  expect(composer).toHaveValue("");
});

it("sends a reply now from the keyboard with the send-now shortcut", async () => {
  const composer = await answeringThread();
  await userEvent.type(composer, "Urgent change{Control>}{Enter}{/Control}");
  await waitFor(() => expect(cancelChatTurn).toHaveBeenCalledWith("chat-turn"));
  expect(queueWrites[0]).toMatchObject({ action: "enqueue", interrupt: true, request: { messages: [{ content: "Urgent change" }] } });
});

it("sends a queued reply now from the thread queue", async () => {
  const composer = await answeringThread();
  await userEvent.type(composer, "First follow-up{Enter}");
  const queue = await screen.findByRole("region", { name: "Thread queue" });
  await userEvent.click(within(queue).getByRole("button", { name: "Send queued message 1 now" }));
  await waitFor(() => expect(queueWrites.at(-1)).toMatchObject({ action: "interrupt", item_id: "item-1" }));
  await waitFor(() => expect(cancelChatTurn).toHaveBeenCalledWith("chat-turn"));
});

it("reads the thread again when Core sends a queued reply, even if the thread list never saw it working", async () => {
  const composer = await answeringThread();
  await userEvent.type(composer, "Queued follow-up{Enter}");
  await screen.findByRole("region", { name: "Thread queue" });
  // The answer this device streamed ends (it was stopped, say).
  await act(async () => { finishStream(); });
  const reads = listChatMessages.mock.calls.length;
  // Core sends and finishes the reply between two reads of the thread list.
  threadQueue = { revision: threadQueue.revision + 1, paused: false, items: [{ ...threadQueue.items[0], status: "complete", turn_id: "queued-turn" }] };
  listChatMessages.mockResolvedValue([
    { id: "u1", role: "user", content: "Check the fuzzers" },
    { id: "u2", role: "user", content: "Queued follow-up" },
    { id: "a2", role: "assistant", content: "Queued answer arrived." },
  ]);
  await waitFor(() => expect(listChatMessages.mock.calls.length).toBeGreaterThan(reads), { timeout: 15_000 });
  expect(await within(log()).findByText("Queued answer arrived.")).toBeVisible();
}, 30_000);
