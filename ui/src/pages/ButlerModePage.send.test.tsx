import { render, screen, waitFor, within } from "@testing-library/react";
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
const api = { requestIfChanged, getControlAssistantSession, getChatAssistantConfig, getOrCreateControlAssistantSession, streamChat, listChatMessages };

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api, coreState: "online", engagement: project, engagements: [project], providers: [provider], harnesses: [], selectEngagement: vi.fn() }),
}));
vi.mock("../state/ButlerPanelBridge", () => ({ useButlerPanelBridge: () => null }));
vi.mock("../components/DialogSystem", () => ({ useConfirmation: () => vi.fn() }));
vi.mock("../hooks/useCompactLayout", () => ({ useCompactLayout: () => false }));
vi.mock("../components/NebulaVoice", () => ({ MicrophoneIcon: () => null }));
vi.mock("../components/ButlerOversight", () => ({ ButlerOversight: () => null }));
vi.mock("../components/ButlerMemory", () => ({ ButlerMemory: () => null }));
vi.mock("../diagnostics", () => ({ logCaughtDiagnostic: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  requestIfChanged.mockImplementation(async (path: string) => {
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
  expect(within(log()).getAllByText("What needs me today?")).toHaveLength(1);
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
