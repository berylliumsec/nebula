import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ProviderGoalChildren } from "./ProviderGoalChildren";

const goal = {
  id: "goal-1", engagementId: "project", sessionId: "session",
  objective: "Parent", completionCriteria: ["Done"], plan: [],
  currentStep: 0, status: "running" as const, childBudget: 1, elapsedSeconds: 0,
  childrenStarted: 0, usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
  linkedTurnIds: [], completionEvidence: [], skillSnapshots: [], childSessionIds: [],
  revision: 2,
};

describe("ProviderGoalChildren", () => {
  it("starts a child from a running goal with a child budget", async () => {
    const api = {
      listGoalChildren: vi.fn().mockResolvedValue([]),
      startGoalChild: vi.fn().mockResolvedValue({ ...goal, id: "child-goal", sessionId: "child-session", objective: "Child" }),
    };
    const onOpenChild = vi.fn();
    render(<ProviderGoalChildren api={api as never} sessionId="session" goal={goal} onOpenChild={onOpenChild} />);
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "Child objective" }), "Isolated child");
    await user.click(screen.getByRole("button", { name: "Start child" }));
    expect(api.startGoalChild).toHaveBeenCalledWith("session", { objective: "Isolated child", completionCriteria: ["Child work completes without sharing parent approvals"] });
    expect(onOpenChild).toHaveBeenCalledWith("child-session");
  });

  it("lists delegated children after the goal stops", async () => {
    const api = {
      listGoalChildren: vi.fn().mockResolvedValue([{ ...goal, id: "child-goal", sessionId: "child-session", objective: "Child", status: "completed" }]),
    };
    render(<ProviderGoalChildren api={api as never} sessionId="session" goal={{ ...goal, status: "completed" as const }} />);
    expect(await screen.findByRole("list", { name: "Delegated children" })).toHaveTextContent("Child");
    expect(screen.queryByRole("textbox", { name: "Child objective" })).toBeNull();
  });

  it("renders nothing without a goal that can delegate or delegated children", async () => {
    const api = { listGoalChildren: vi.fn().mockResolvedValue([]) };
    const { container } = render(<ProviderGoalChildren api={api as never} sessionId="session" />);
    await vi.waitFor(() => expect(api.listGoalChildren).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
