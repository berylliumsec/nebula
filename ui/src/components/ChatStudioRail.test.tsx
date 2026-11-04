import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ChatGoal } from "../api/types";
import { ChatStudioRail } from "./ChatStudioRail";

it("keeps Nebula and native harness goals independently labeled", () => {
  const goal: ChatGoal = {
    id: "nebula-goal", engagementId: "project", sessionId: "chat",
    objective: "Retain evidence for the audio path", completionCriteria: ["Trace retained"],
    plan: [], currentStep: 2, status: "running", elapsedSeconds: 20,
    usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
    childrenStarted: 0, linkedTurnIds: [], completionEvidence: [], skillSnapshots: [], revision: 3,
  };
  render(<ChatStudioRail
    goal={goal}
    harnessGoal={{ objective: "Finish the native review", status: "complete", childAgents: 0 }}
    subagents={[]}
    pendingRequests={0}
    sessionId="chat"
    goalEditorOpen={false}
    canEditGoal
    onToggleGoal={vi.fn()}
    onOpenSubagents={vi.fn()}
    onOpenSessionDetails={vi.fn()}
  />);
  const region = screen.getByRole("region", { name: "Goal" });
  expect(region).toHaveTextContent("Retain evidence for the audio path");
  expect(region).toHaveTextContent("running");
  expect(region).toHaveTextContent("Native harness goal: complete");
  expect(screen.getByRole("button", { name: "View full goal and evidence gates" })).toBeVisible();
  const native = screen.getByText("View native harness goal");
  fireEvent.click(native);
  expect(screen.getByText("Finish the native review", { selector: "p" })).toBeVisible();
});

it("discloses the native goal while Core can create a separate goal", () => {
  render(<ChatStudioRail
    harnessGoal={{ objective: "Read the full native objective", currentStep: "Check the saved evidence", status: "running", childAgents: 0 }}
    subagents={[]} pendingRequests={0} sessionId="chat" goalEditorOpen={false} canEditGoal
    onToggleGoal={vi.fn()} onOpenSubagents={vi.fn()} onOpenSessionDetails={vi.fn()}
  />);
  expect(screen.getByRole("button", { name: "Create goal" })).toBeVisible();
  fireEvent.click(screen.getByText("View full goal"));
  expect(screen.getByText("Read the full native objective", { selector: "p" })).toBeVisible();
  expect(screen.getByText("Current step: Check the saved evidence")).toBeVisible();
});
