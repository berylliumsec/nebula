import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchDraftProvider, useWorkbenchDrafts } from "./WorkbenchDraftContext";

const state = vi.hoisted(() => ({
  createHandoff: vi.fn(),
}));

vi.mock("../api/runtime", () => ({ desktopDeviceId: () => Promise.resolve("device-current") }));
vi.mock("./WorkspaceContext", () => ({
  useWorkspace: () => ({
    api: { createHandoff: state.createHandoff },
    engagement: { id: "project-1", name: "Project one" },
  }),
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function AskFromAssistant() {
  const { requestNebulaDraft } = useWorkbenchDrafts();
  return <button type="button" onClick={() => requestNebulaDraft({
    text: "Keep this conversation visible",
    sourceKind: "assistant_message",
    sourceId: "message-1",
    sourceLabel: "Assistant response",
  })}>Ask Nebula</button>;
}

describe("Ask Nebula conversation navigation", () => {
  beforeEach(() => {
    state.createHandoff.mockReset();
    state.createHandoff.mockResolvedValue({ id: "handoff-1" });
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
