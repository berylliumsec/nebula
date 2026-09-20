import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ChatGoal } from "../api/types";
import { DialogProvider } from "./DialogSystem";
import { ProviderGoalPanel } from "./ProviderGoalPanel";

const draft: ChatGoal = {
  id: "goal", engagementId: "project", sessionId: "session",
  objective: "Inspect safely", completionCriteria: ["Evidence retained"],
  plan: ["Inspect"], currentStep: 0, status: "draft",
  usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
  elapsedSeconds: 0, childrenStarted: 0,
  linkedTurnIds: [], completionEvidence: [], skillSnapshots: [], revision: 1,
};

it("creates a durable draft from explicit objective and criteria", async () => {
  const createChatGoal = vi.fn().mockResolvedValue(draft);
  const onChange = vi.fn();
  const api = { createChatGoal } as unknown as ApiClient;
  render(<DialogProvider><ProviderGoalPanel api={api} sessionId="session" onChange={onChange} /></DialogProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Add goal" }));
  fireEvent.change(screen.getByLabelText("Objective"), { target: { value: "Inspect safely" } });
  fireEvent.change(screen.getByLabelText("Completion criteria"), { target: { value: "Evidence retained\nChecks pass" } });
  fireEvent.change(screen.getByLabelText("Plan"), { target: { value: "Inspect\nValidate" } });
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  await waitFor(() => expect(createChatGoal).toHaveBeenCalledWith("session", {
    objective: "Inspect safely",
    completionCriteria: ["Evidence retained", "Checks pass"],
    plan: ["Inspect", "Validate"],
  }));
  expect(onChange).toHaveBeenCalledWith(draft);
});

it("records explicit blocked and completed outcomes", async () => {
  const running: ChatGoal = { ...draft, status: "running", revision: 2 };
  const blocked: ChatGoal = { ...running, status: "blocked", blockedReason: "No new evidence", revision: 3 };
  const writeChatGoal = vi.fn().mockResolvedValueOnce(blocked).mockResolvedValueOnce({
    ...running,
    status: "completed",
    completionSummary: "Checks passed.",
    completionEvidence: [{ kind: "operator_confirmation", detail: "Focused tests passed" }],
    revision: 4,
  });
  const onChange = vi.fn();
  const api = { writeChatGoal } as unknown as ApiClient;
  const view = render(<DialogProvider><ProviderGoalPanel api={api} sessionId="session" goal={running} onChange={onChange} /></DialogProvider>);

  fireEvent.click(screen.getByRole("button", { name: "Block" }));
  fireEvent.change(screen.getByLabelText("Blocked reason"), { target: { value: "No new evidence" } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm blocked" }));
  await waitFor(() => expect(writeChatGoal).toHaveBeenCalledWith("session", {
    expectedRevision: 2, action: "block", reason: "No new evidence",
  }));

  view.rerender(<DialogProvider><ProviderGoalPanel api={api} sessionId="session" goal={{ ...running, revision: 3 }} onChange={onChange} /></DialogProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Complete" }));
  fireEvent.change(screen.getByLabelText("Completion summary"), { target: { value: "Checks passed." } });
  fireEvent.change(screen.getByLabelText("Completion evidence"), { target: { value: "Focused tests passed" } });
  fireEvent.click(screen.getByRole("button", { name: "Confirm complete" }));
  await waitFor(() => expect(writeChatGoal).toHaveBeenLastCalledWith("session", {
    expectedRevision: 3,
    action: "complete",
    completionSummary: "Checks passed.",
    completionEvidence: [{ kind: "operator_confirmation", detail: "Focused tests passed" }],
  }));
});

it("requires an explicit Start and uses the latest revision", async () => {
  const running = { ...draft, status: "running" as const, revision: 2 };
  const writeChatGoal = vi.fn().mockResolvedValue(running);
  const onChange = vi.fn();
  const api = { writeChatGoal } as unknown as ApiClient;
  render(<DialogProvider><ProviderGoalPanel api={api} sessionId="session" goal={draft} onChange={onChange} /></DialogProvider>);
  expect(screen.getByText(/draft · step 0/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Start" }));
  await waitFor(() => expect(writeChatGoal).toHaveBeenCalledWith("session", {
    expectedRevision: 1, action: "start",
  }));
  expect(onChange).toHaveBeenCalledWith(running);
});

it("explicitly replaces immutable goal skills by exact source path", async () => {
  const running: ChatGoal = {
    ...draft,
    status: "running",
    revision: 3,
    skillSnapshots: [{
      name: "retained",
      path: "/workspace/.agents/skills/retained/SKILL.md",
      source: "project",
      sha256: "a".repeat(64),
    }],
  };
  const updated = { ...running, revision: 4, skillSnapshots: [] };
  const replaceChatGoalSkills = vi.fn().mockResolvedValue(updated);
  const onChange = vi.fn();
  const api = { replaceChatGoalSkills } as unknown as ApiClient;
  const view = render(<DialogProvider><form aria-label="Chat composer"><ProviderGoalPanel
      api={api}
      sessionId="session"
      goal={running}
      skills={[{
        name: "report",
        path: "/managed/.agents/skills/report/SKILL.md",
        source: "installed",
      }]}
      onChange={onChange}
    /></form></DialogProvider>);

  fireEvent.click(screen.getByRole("button", { name: "Edit skills" }));
  expect(view.container.querySelectorAll("form")).toHaveLength(1);
  expect(screen.getByText(/retained snapshot; source unavailable/)).toBeVisible();
  fireEvent.click(screen.getByRole("checkbox", { name: /retained/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: /report/ }));
  fireEvent.click(screen.getByRole("button", { name: "Save skills" }));

  await waitFor(() => expect(replaceChatGoalSkills).toHaveBeenCalledWith("session", {
    expectedRevision: 3,
    skills: [{ name: "report", path: "/managed/.agents/skills/report/SKILL.md" }],
  }));
  expect(onChange).toHaveBeenCalledWith(updated);
});

it("confirms before cancelling a goal because cancellation is final", async () => {
  const running: ChatGoal = { ...draft, status: "running", revision: 2 };
  const writeChatGoal = vi.fn().mockResolvedValue({ ...running, status: "cancelled", revision: 3 });
  const onChange = vi.fn();
  render(<DialogProvider><ProviderGoalPanel api={{ writeChatGoal } as unknown as ApiClient} sessionId="session" goal={running} onChange={onChange} /></DialogProvider>);

  fireEvent.click(screen.getByRole("button", { name: "Cancel goal" }));
  const dialog = await screen.findByRole("dialog", { name: "Cancel this goal?" });
  expect(writeChatGoal).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep goal" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(writeChatGoal).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Cancel goal" }));
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Cancel this goal?" })).getByRole("button", { name: "Cancel goal" }));
  await waitFor(() => expect(writeChatGoal).toHaveBeenCalledWith("session", { expectedRevision: 2, action: "cancel" }));
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: "cancelled" }));
});
