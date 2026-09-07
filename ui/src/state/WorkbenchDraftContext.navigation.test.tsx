import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchDraftProvider, useWorkbenchDrafts } from "./WorkbenchDraftContext";

const state = vi.hoisted(() => ({
  createHandoff: vi.fn(),
  cancelHandoff: vi.fn().mockResolvedValue({}),
}));

vi.mock("../api/runtime", () => ({ desktopDeviceId: () => Promise.resolve("device-current") }));
vi.mock("./WorkspaceContext", () => ({
  useWorkspace: () => ({
    api: { createHandoff: state.createHandoff, cancelHandoff: state.cancelHandoff },
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
  const { requestNebulaDraft, clearAssistantDrafts } = useWorkbenchDrafts();
  return <><button type="button" onClick={() => requestNebulaDraft({
    text: "Keep this conversation visible",
    sourceKind: "assistant_message",
    sourceId: "message-1",
    sourceLabel: "Assistant response",
  }, view)}>Ask Nebula</button><button onClick={clearAssistantDrafts}>Clear context</button></>;
}

describe("Ask Nebula conversation navigation", () => {
  beforeEach(() => {
    state.createHandoff.mockReset();
    state.cancelHandoff.mockClear();
    state.createHandoff.mockResolvedValue({ id: "handoff-1", revision: 1 });
  });

  it("clears a sent or discarded context handoff without losing browser conversation identity", async () => {
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench?view=browser&session=conversation-1"]}>
      <WorkbenchDraftProvider><LocationProbe /><AskFromAssistant view="browser" /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Ask Nebula" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Ask Nebula" }));
    await userEvent.click(screen.getByRole("button", { name: "Clear context" }));
    await act(async () => resolve({ id: "late-handoff", revision: 2 }));
    expect(screen.getByTestId("location")).not.toHaveTextContent("handoff=");
    expect(state.cancelHandoff).toHaveBeenCalledWith("late-handoff", 2);
  });

  it("removes a cleared handoff restored by a late stream callback while retaining fresh handoffs", async () => {
    render(<MemoryRouter initialEntries={["/projects/project-1/workbench?view=browser&session=conversation-1"]}>
      <WorkbenchDraftProvider><LocationProbe /><AskFromAssistant view="browser" /><LateNavigation /></WorkbenchDraftProvider>
    </MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Ask Nebula" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Ask Nebula" }));
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

    await user.click(screen.getByRole("button", { name: "Ask Nebula" }));
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
    await userEvent.click(screen.getByRole("button", { name: "Ask Nebula" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("handoff=handoff-browser"));
    const destination = new URL(screen.getByTestId("location").textContent ?? "", "http://nebula.test");
    expect(destination.searchParams.get("view")).toBe("browser");
    expect(destination.searchParams.get("browserEngine")).toBe("native");
    expect(destination.searchParams.get("session")).toBe("conversation-1");
 });
