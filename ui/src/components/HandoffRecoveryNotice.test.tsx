import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { HandoffResolution } from "../api/types";
import { HandoffRecoveryNotice } from "./HandoffRecoveryNotice";

const state = vi.hoisted(() => ({
  activeHandoffIds: [] as string[],
  api: {
    resolveHandoff: vi.fn(),
    consumeHandoff: vi.fn(),
  },
}));

vi.mock("../api/runtime", () => ({ desktopDeviceId: () => Promise.resolve("device-current") }));
vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => ({ api: state.api }) }));
vi.mock("../state/WorkbenchDraftContext", () => ({
  useWorkbenchDrafts: () => ({ activeHandoffIds: state.activeHandoffIds }),
}));

function resolution(overrides: Partial<HandoffResolution> = {}): HandoffResolution {
  return {
    envelope: {
      id: "handoff-1",
      projectId: "project-1",
      sourceRefs: [{ projectId: "project-1", kind: "evidence", id: "evidence-1", revision: 2 }],
      actionId: "ask_nebula",
      originDeviceId: "device-origin",
      sourceHashes: { "evidence:evidence-1": "a".repeat(64) },
      sourceLabels: { "evidence:evidence-1": "Request transcript" },
      transient: false,
      status: "pending",
      createdAt: "2026-08-27T00:00:00Z",
      updatedAt: "2026-08-27T00:00:00Z",
      expiresAt: "2026-08-28T00:00:00Z",
      revision: 1,
    },
    sources: [{
      ref: { projectId: "project-1", kind: "evidence", id: "evidence-1", revision: 2 },
      state: "available",
      label: "Request transcript",
    }],
    recovery: "ready",
    ...overrides,
  };
}

function renderNotice() {
  return render(
    <MemoryRouter initialEntries={["/projects/project-1/workbench?view=chat&handoff=handoff-1"]}>
      <HandoffRecoveryNotice />
    </MemoryRouter>,
  );
}

describe("HandoffRecoveryNotice", () => {
  beforeEach(() => {
    state.activeHandoffIds = [];
    state.api.resolveHandoff.mockReset();
    state.api.consumeHandoff.mockReset();
  });

  it("does not rehydrate a handoff whose unsent bytes are still in this window", () => {
    state.activeHandoffIds = ["handoff-1"];
    renderNotice();
    expect(state.api.resolveHandoff).not.toHaveBeenCalled();
    expect(screen.queryByText(/Handoff/)).not.toBeInTheDocument();
  });

  it("directs transient cross-device recovery back to the originating device", async () => {
    state.api.resolveHandoff.mockResolvedValue(resolution({
      envelope: { ...resolution().envelope, transient: true },
      sources: [{
        ref: { projectId: "project-1", kind: "evidence", id: "evidence-1" },
        state: "origin_required",
        label: "Request transcript",
      }],
      recovery: "resume_origin",
    }));
    renderNotice();
    expect(await screen.findByText("Resume on the originating device")).toBeVisible();
    expect(screen.getByText(/unsent selected bytes remained only in memory/i)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Continue/ })).not.toBeInTheDocument();
  });

  it("consumes a matching durable handoff idempotently", async () => {
    const current = resolution();
    state.api.resolveHandoff.mockResolvedValue(current);
    state.api.consumeHandoff.mockResolvedValue({ ...current.envelope, status: "consumed", revision: 2 });
    const user = userEvent.setup();
    renderNotice();
    await user.click(await screen.findByRole("button", { name: /Continue/ }));
    await waitFor(() => expect(state.api.consumeHandoff).toHaveBeenCalledWith(
      "handoff-1",
      1,
      "device-current",
      "consume:device-current:handoff-1",
    ));
  });
});
