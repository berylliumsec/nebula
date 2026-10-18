import {render, screen} from "@testing-library/react";
import {expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {ChatGoal} from "../api/types";
import {DialogProvider} from "./DialogSystem";
import {ProviderGoalPanel} from "./ProviderGoalPanel";

const source: ChatGoal = {
  id: "old-goal", engagementId: "project", sessionId: "source-chat",
  objective: "Inspect safely", completionCriteria: ["Evidence retained"],
  plan: ["Inspect"], currentStep: 0, status: "paused",
  usage: {inputTokens: 0, outputTokens: 0, totalTokens: 0},
  elapsedSeconds: 0, childrenStarted: 0, linkedTurnIds: [],
  completionEvidence: [], skillSnapshots: [], revision: 1,
  handoffTargetSessionId: "provider-chat",
  handoffReason: "The harness could not enforce planning",
};

it("links both sides of a saved handoff and keeps the old goal paused", () => {
  const view = render(<DialogProvider><ProviderGoalPanel
    api={{} as ApiClient} sessionId="source-chat" goal={source} onChange={vi.fn()}
  /></DialogProvider>);
  expect(screen.getByRole("link", {name: "Open continuation"})).toHaveAttribute(
    "href", "/projects/project/workbench?view=chat&session=provider-chat",
  );
  expect(screen.getByText(/The harness could not enforce planning/)).toBeVisible();
  expect(screen.queryByRole("button", {name: "Resume"})).not.toBeInTheDocument();

  view.rerender(<DialogProvider><ProviderGoalPanel
    api={{} as ApiClient} sessionId="provider-chat"
    goal={{...source, id: "new-goal", sessionId: "provider-chat", status: "running", handoffSourceSessionId: "source-chat", handoffTargetSessionId: undefined}}
    onChange={vi.fn()}
  /></DialogProvider>);
  expect(screen.getByRole("link", {name: "Open source chat"})).toHaveAttribute(
    "href", "/projects/project/workbench?view=chat&session=source-chat",
  );
});
