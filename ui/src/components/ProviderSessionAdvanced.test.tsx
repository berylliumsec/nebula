import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
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
    render(<ProviderSessionAdvanced api={api as never} sessionId="session" goal={goal} onOpenChild={onOpenChild} />);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Checkpoint files" }), "notes.md");
    await user.click(screen.getByRole("button", { name: "Save checkpoint" }));
    expect(api.captureChatCheckpoint).toHaveBeenCalledWith("session", { label: "Operator checkpoint", paths: ["notes.md"] });
    await user.type(screen.getByRole("textbox", { name: "Child objective" }), "Isolated child");
    await user.click(screen.getByRole("button", { name: "Start child" }));
    expect(api.startGoalChild).toHaveBeenCalled();
    expect(onOpenChild).toHaveBeenCalledWith("child-session");
  });
});
