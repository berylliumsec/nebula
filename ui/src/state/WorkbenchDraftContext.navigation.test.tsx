import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchDraftProvider, useWorkbenchDrafts } from "./WorkbenchDraftContext";

const state = vi.hoisted(() => {
  const api = {
    createHandoff: vi.fn(),
    cancelHandoff: vi.fn().mockResolvedValue({}),
    createTemporaryChat: vi.fn(),
    discardTemporaryChat: vi.fn().mockResolvedValue(undefined),
    keepTemporaryChatAlive: vi.fn().mockResolvedValue(undefined),
    streamChat: vi.fn(),
    cancelChatTurn: vi.fn().mockResolvedValue({}),
    stopHarnessTurn: vi.fn().mockResolvedValue(undefined),
    getPendingChatTurn: vi.fn().mockResolvedValue({ id: "pending-turn" }),
  };
  return { ...api, api };
});

vi.mock("../api/runtime", () => ({ desktopDeviceId: () => Promise.resolve("device-current") }));
vi.mock("./WorkspaceContext", () => ({
  useWorkspace: () => ({
    api: state.api,
    engagement: { id: "project-1", name: "Project one" },
  }),
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function LateNavigation() {
  const navigate = useNavigate();
  const { clearAssistantDrafts } = useWorkbenchDrafts();
  return <><button onClick={() => navigate("/projects/project-1/workbench?view=browser&session=conversation-1&handoff=handoff-1")}>Replay old navigation</button><button onClick={() => { clearAssistantDrafts(); navigate("/projects/project-1/workbench?view=browser&session=conversation-1&handoff=handoff-1"); }}>Clear and replay together</button><button onClick={() => navigate("/projects/project-1/workbench?view=browser&session=conversation-1&handoff=fresh-handoff")}>New navigation</button></>;
}

function AskFromAssistant({ view = "chat" }: { view?: "chat" | "browser" }) {
  const { requestChatContext, clearAssistantDrafts } = useWorkbenchDrafts();
  return <><button type="button" onClick={() => requestChatContext({
    text: "Keep this conversation visible",
    sourceKind: "assistant_message",
    sourceId: "message-1",
    sourceLabel: "Assistant response",
  }, view)}>Add context to chat</button><button onClick={clearAssistantDrafts}>Clear context</button></>;
}

function ConcurrentAskProbe() {
  const { registerAssistantSnapshot, requestNebulaDraft } = useWorkbenchDrafts();
  return <>
    <button onClick={() => registerAssistantSnapshot({ engagementId: "project-1", providerId: "provider-1", model: "model-1" })}>Register runtime</button>
    <button onClick={() => requestNebulaDraft({ text: "First context", sourceKind: "conversation", sourceId: "one", sourceLabel: "First source" })}>Ask first</button>
    <button onClick={() => requestNebulaDraft({ text: "Second context", sourceKind: "conversation", sourceId: "two", sourceLabel: "Second source" })}>Ask second</button>
  </>;
}

it("opens independent Ask Nebula windows without replacing a running request", async () => {
  state.createTemporaryChat.mockReset();
  state.createTemporaryChat
    .mockResolvedValueOnce({ id: "popup-one", backend: "provider", providerId: "provider-1", model: "model-1" })
    .mockResolvedValue({ id: "popup-two", backend: "provider", providerId: "provider-1", model: "model-1" });
  state.discardTemporaryChat.mockClear();
  state.streamChat.mockImplementationOnce((_body, onEvent, signal) => new Promise((_resolve, reject) => {
    onEvent({ type: "started", turnId: "turn-one", model: "model-1", sessionId: "popup-one" });
    signal.addEventListener("abort", () => reject(new DOMException("Stopped", "AbortError")));
  }));
  const user = userEvent.setup();
  render(<MemoryRouter><WorkbenchDraftProvider><ConcurrentAskProbe /></WorkbenchDraftProvider></MemoryRouter>);

  await user.click(screen.getByRole("button", { name: "Register runtime" }));
  await user.click(screen.getByRole("button", { name: "Ask first" }));
  await waitFor(() => expect(state.createTemporaryChat).toHaveBeenCalledTimes(1));
  const firstDialog = screen.getByRole("dialog", { name: "Ask Nebula" });
  await user.type(within(firstDialog).getByRole("textbox", { name: "Question for Nebula" }), "Keep working");
  await user.click(within(firstDialog).getByRole("button", { name: "Ask question" }));
  await expect(within(firstDialog).getByRole("button", { name: "Stop response" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Ask second" }));
  await waitFor(() => expect(state.createTemporaryChat).toHaveBeenCalledTimes(2));

  const dialogs = screen.getAllByRole("dialog", { name: "Ask Nebula" });
  expect(dialogs).toHaveLength(2);
  expect(within(dialogs[0]).getByText("First source")).toBeVisible();
  expect(within(dialogs[1]).getByText("Second source")).toBeVisible();
  expect(within(dialogs[0]).getByRole("button", { name: "Stop response" })).toBeVisible();

  await user.click(within(dialogs[1]).getByRole("button", { name: "Close Ask Nebula" }));
  expect(screen.getAllByRole("dialog", { name: "Ask Nebula" })).toHaveLength(1);
  expect(screen.getByText("First source")).toBeVisible();
  await waitFor(() => expect(state.discardTemporaryChat).toHaveBeenCalledExactlyOnceWith("popup-two"));
});

describe("Add context to chat conversation navigation", () => {
  beforeEach(() => {
    state.createHandoff.mockReset();
    state.cancelHandoff.mockClear();
    state.createHandoff.mockResolvedValue({ id: "handoff-1", revision: 1 });
  });

  it("clears a sent or discarded context handoff without losing browser conversation identity", async () => {
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench?view=browser&session=conversation-1"]}>
      <WorkbenchDraftProvider><LocationProbe /><AskFromAssistant view="browser" /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Add context to chat" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("handoff=handoff-1"));
    await userEvent.click(screen.getByRole("button", { name: "Clear context" }));
    expect(screen.getByTestId("location")).toHaveTextContent("?view=browser&session=conversation-1");
    expect(screen.getByTestId("location")).not.toHaveTextContent("handoff=");
    expect(state.cancelHandoff).toHaveBeenCalledWith("handoff-1", 1);
  });

  it("does not resurrect a handoff that finishes saving after its context was cleared", async () => {
    let resolve: (value: { id: string; revision: number }) => void = () => {};
    state.createHandoff.mockReturnValue(new Promise(value => { resolve = value; }));
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench?view=browser&session=conversation-1"]}>
      <WorkbenchDraftProvider><LocationProbe /><AskFromAssistant view="browser" /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Add context to chat" }));
    await userEvent.click(screen.getByRole("button", { name: "Clear context" }));
    await act(async () => resolve({ id: "late-handoff", revision: 2 }));
    expect(screen.getByTestId("location")).not.toHaveTextContent("handoff=");
    expect(state.cancelHandoff).toHaveBeenCalledWith("late-handoff", 2);
  });

  it("removes a cleared handoff restored by a late stream callback while retaining fresh handoffs", async () => {
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench?view=browser&session=conversation-1"]}>
      <WorkbenchDraftProvider><LocationProbe /><AskFromAssistant view="browser" /><LateNavigation /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Add context to chat" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("handoff=handoff-1"));
    await userEvent.click(screen.getByRole("button", { name: "Clear context" }));
    await userEvent.click(screen.getByRole("button", { name: "Replay old navigation" }));
    expect(screen.getByTestId("location")).toHaveTextContent("?view=browser&session=conversation-1");
    expect(screen.getByTestId("location")).not.toHaveTextContent("handoff=");
    await userEvent.click(screen.getByRole("button", { name: "New navigation" }));
    expect(screen.getByTestId("location")).toHaveTextContent("handoff=fresh-handoff");
  });

  it("reconciles batched clearing and navigation even when the final URL matches the previous render", async () => {
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench?view=browser&session=conversation-1"]}>
      <WorkbenchDraftProvider><LocationProbe /><AskFromAssistant view="browser" /><LateNavigation /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Add context to chat" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("handoff=handoff-1"));
    await userEvent.click(screen.getByRole("button", { name: "Clear and replay together" }));
    expect(screen.getByTestId("location")).not.toHaveTextContent("handoff=");
  });

  it("keeps the active conversation selected before and after the durable handoff is created", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/projects/project-1/workbench?view=chat&session=conversation-1"]}>
        <WorkbenchDraftProvider>
          <LocationProbe />
          <AskFromAssistant />
        </WorkbenchDraftProvider>
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "Add context to chat" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("handoff=handoff-1"));
    const destination = new URL(screen.getByTestId("location").textContent ?? "", "http://nebula.test");
    expect(destination.pathname).toBe("/projects/project-1/workbench");
    expect(destination.searchParams.get("view")).toBe("chat");
    expect(destination.searchParams.get("session")).toBe("conversation-1");
    expect(destination.searchParams.get("handoff")).toBe("handoff-1");
    expect(state.createHandoff).toHaveBeenCalledTimes(1);
  });
});

 it("keeps native browser and conversation selected during context attachment", async () => {
    state.createHandoff.mockResolvedValue({ id: "handoff-browser" });
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench?view=browser&browserEngine=native&session=conversation-1"]}>
      <WorkbenchDraftProvider><LocationProbe /><AskFromAssistant view="browser" /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Add context to chat" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("handoff=handoff-browser"));
    const destination = new URL(screen.getByTestId("location").textContent ?? "", "http://nebula.test");
    expect(destination.searchParams.get("view")).toBe("browser");
    expect(destination.searchParams.get("browserEngine")).toBe("native");
    expect(destination.searchParams.get("session")).toBe("conversation-1");
 });

function SelectionActions() {
  const navigate = useNavigate();
  const { requestNoteDraft, requestChatContext } = useWorkbenchDrafts();
  const request = { text: "Selected text", sourceKind: "conversation", sourceId: "message-1", sourceLabel: "Conversation" };
  return <>
    <button onClick={() => requestNoteDraft(request)}>Take note</button>
    <button onClick={() => requestChatContext(request)}>Add to context</button>
    <button onClick={() => navigate("/settings")}>Open settings</button>
  </>;
}

describe("selection handoffs after the operator moves on", () => {
  beforeEach(() => {
    state.createHandoff.mockReset();
    state.cancelHandoff.mockClear();
  });

  it("attaches the note handoff while the operator stays on the Workbench", async () => {
    state.createHandoff.mockResolvedValue({ id: "handoff-note", revision: 1 });
    render(<MemoryRouter initialEntries={["/projects/project-1/findings"]}>
      <WorkbenchDraftProvider><LocationProbe /><SelectionActions /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Take note" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/projects/project-1/workbench?view=notes&handoff=handoff-note"));
  });

  it.each([["Take note", "notes"], ["Add to context", "chat"]])(
    "does not pull the operator back to the Workbench after %s once they left",
    async (action, view) => {
      let resolve: (value: { id: string; revision: number }) => void = () => {};
      state.createHandoff.mockReturnValue(new Promise((value) => { resolve = value; }));
      render(<MemoryRouter initialEntries={["/projects/project-1/findings"]}>
        <WorkbenchDraftProvider><LocationProbe /><SelectionActions /></WorkbenchDraftProvider>
      </MemoryRouter>);
      await userEvent.click(screen.getByRole("button", { name: action }));
      expect(screen.getByTestId("location")).toHaveTextContent(`/projects/project-1/workbench?view=${view}`);
      await userEvent.click(screen.getByRole("button", { name: "Open settings" }));
      expect(screen.getByTestId("location")).toHaveTextContent("/settings");

      await act(async () => resolve({ id: "late-handoff", revision: 3 }));

      expect(screen.getByTestId("location")).toHaveTextContent("/settings");
      expect(screen.getByTestId("location")).not.toHaveTextContent("handoff=");
    },
  );
});
