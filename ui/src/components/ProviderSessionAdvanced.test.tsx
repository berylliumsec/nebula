import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DialogProvider } from "./DialogSystem";
import { ProviderSessionAdvanced } from "./ProviderSessionAdvanced";
import { ApiError } from "../api/client";

const goal = {
  id: "goal-1", engagementId: "project", sessionId: "session",
  objective: "Parent", completionCriteria: ["Done"], plan: [],
  currentStep: 0, status: "running" as const, childBudget: 1, elapsedSeconds: 0,
  childrenStarted: 0, usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
  linkedTurnIds: [], completionEvidence: [], skillSnapshots: [], childSessionIds: [],
  revision: 2,
};

describe("ProviderSessionAdvanced", () => {
  it("captures a checkpoint and starts a child without nested forms", async () => {
    const api = {
      listChatCheckpoints: vi.fn().mockResolvedValue([]),
      listGoalChildren: vi.fn().mockResolvedValue([]),
      getChatSchedule: vi.fn().mockRejectedValue(new ApiError("missing", 404)),
      captureChatCheckpoint: vi.fn().mockResolvedValue({ id: "cp-1" }),
      startGoalChild: vi.fn().mockResolvedValue({ ...goal, id: "child-goal", sessionId: "child-session", objective: "Child" }),
      createChatSchedule: vi.fn(),
    };
    const onOpenChild = vi.fn();
    render(<DialogProvider><ProviderSessionAdvanced api={api as never} sessionId="session" goal={goal} onOpenChild={onOpenChild} /></DialogProvider>);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Checkpoint files" }), "notes.md");
    await user.click(screen.getByRole("button", { name: "Save checkpoint" }));
    expect(api.captureChatCheckpoint).toHaveBeenCalledWith("session", { label: "Operator checkpoint", paths: ["notes.md"] });
    await user.type(screen.getByRole("textbox", { name: "Child objective" }), "Isolated child");
    await user.click(screen.getByRole("button", { name: "Start child" }));
    expect(api.startGoalChild).toHaveBeenCalled();
    expect(onOpenChild).toHaveBeenCalledWith("child-session");
  });

  it("confirms before restoring a checkpoint over workspace files", async () => {
    const api = {
      listChatCheckpoints: vi.fn().mockResolvedValue([{ id: "cp-1", label: "Before refactor", files: [{ path: "notes.md" }] }]),
      listGoalChildren: vi.fn().mockResolvedValue([]),
      getChatSchedule: vi.fn().mockRejectedValue(new ApiError("missing", 404)),
      restoreChatCheckpoint: vi.fn().mockResolvedValue(undefined),
    };
    render(<DialogProvider><ProviderSessionAdvanced api={api as never} sessionId="session" /></DialogProvider>);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Restore Before refactor" }));
    const dialog = await screen.findByRole("dialog", { name: "Restore checkpoint?" });
    expect(api.restoreChatCheckpoint).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.restoreChatCheckpoint).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Restore Before refactor" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Restore checkpoint?" })).getByRole("button", { name: "Restore checkpoint" }));
    await waitFor(() => expect(api.restoreChatCheckpoint).toHaveBeenCalledWith("cp-1"));
  });
});
