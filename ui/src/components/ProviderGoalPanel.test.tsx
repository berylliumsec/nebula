import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import type { ChatGoal } from "../api/types";
import { DialogProvider } from "./DialogSystem";
import { activeSeconds, estimateLiveTokens, ProviderGoalPanel } from "./ProviderGoalPanel";

const draft: ChatGoal = {
  id: "goal", engagementId: "project", sessionId: "session",
  objective: "Inspect safely", completionCriteria: ["Evidence retained"],
  plan: ["Inspect"], currentStep: 0, status: "draft",
  usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
  elapsedSeconds: 0, childrenStarted: 0,
  linkedTurnIds: [], completionEvidence: [], skillSnapshots: [], revision: 1,
};

it("shows an explicitly estimated token total while a goal turn streams", () => {
  const running: ChatGoal = {
    ...draft,
    status: "running",
    usage: { inputTokens: 80, outputTokens: 20, totalTokens: 100 },
  };
  const view = render(<DialogProvider><ProviderGoalPanel
    api={{} as ApiClient}
    sessionId="session"
    goal={running}
    liveTokenEstimate={25}
    onChange={vi.fn()}
  /></DialogProvider>);

  expect(screen.getByText("~125 tokens")).toHaveAttribute("title", expect.stringContaining("Estimated while this turn streams"));
  view.rerender(<DialogProvider><ProviderGoalPanel api={{} as ApiClient} sessionId="session" goal={running} onChange={vi.fn()} /></DialogProvider>);
  expect(screen.getByText("100 tokens")).not.toHaveAttribute("title");
  expect(estimateLiveTokens("streaming response")).toBeGreaterThan(0);
});

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

it("creates the conversation with its goal before the first message", async () => {
  const onCreate = vi.fn().mockResolvedValue(draft);
  const onChange = vi.fn();
  render(<DialogProvider><ProviderGoalPanel api={{} as ApiClient} onCreate={onCreate} onChange={onChange} /></DialogProvider>);

  fireEvent.click(screen.getByRole("button", { name: "Add goal" }));
  fireEvent.change(screen.getByLabelText("Objective"), { target: { value: "Start with a goal" } });
  fireEvent.change(screen.getByLabelText("Completion criteria"), { target: { value: "First turn is linked" } });
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

  await waitFor(() => expect(onCreate).toHaveBeenCalledWith({
    objective: "Start with a goal",
    completionCriteria: ["First turn is linked"],
    plan: [],
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
  const getChatGoal = vi.fn()
    .mockResolvedValueOnce(running)
    .mockResolvedValueOnce({ ...running, revision: 3 });
  const api = { getChatGoal, writeChatGoal } as unknown as ApiClient;
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
  const onWorkDispatched = vi.fn().mockResolvedValue(undefined);
  const api = { getChatGoal: vi.fn().mockResolvedValue(draft), writeChatGoal } as unknown as ApiClient;
  render(<DialogProvider><ProviderGoalPanel api={api} sessionId="session" goal={draft} onChange={onChange} onWorkDispatched={onWorkDispatched} /></DialogProvider>);
  expect(screen.getByText(/draft · step 0/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Start" }));
  await waitFor(() => expect(writeChatGoal).toHaveBeenCalledWith("session", {
    expectedRevision: 1, action: "start",
  }));
  expect(onChange).toHaveBeenCalledWith(running);
  expect(onWorkDispatched).toHaveBeenCalledOnce();
});

it("waits for durable assistant settings before resuming a goal", () => {
  const paused: ChatGoal = { ...draft, status: "paused", revision: 2 };
  const writeChatGoal = vi.fn();
  render(<DialogProvider><ProviderGoalPanel
    api={{ writeChatGoal } as unknown as ApiClient}
    sessionId="session"
    goal={paused}
    settingsBusy
    onChange={vi.fn()}
  /></DialogProvider>);

  const resume = screen.getByRole("button", { name: "Resume" });
  expect(resume).toBeDisabled();
  fireEvent.click(resume);
  expect(writeChatGoal).not.toHaveBeenCalled();
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
  const api = { getChatGoal: vi.fn().mockResolvedValue(running), replaceChatGoalSkills } as unknown as ApiClient;
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
  render(<DialogProvider><ProviderGoalPanel api={{ getChatGoal: vi.fn().mockResolvedValue(running), writeChatGoal } as unknown as ApiClient} sessionId="session" goal={running} onChange={onChange} /></DialogProvider>);

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

it("cancels a goal whose revision moved while the model was working", async () => {
  // Core advances a goal on every turn, so the revision an operator is looking
  // at is stale the moment the model works. Their decision still stands.
  const running: ChatGoal = { ...draft, status: "running", revision: 2 };
  const advanced: ChatGoal = { ...running, currentStep: 3, revision: 7 };
  const cancelled: ChatGoal = { ...advanced, status: "cancelled", revision: 8 };
  const conflict = new ApiError("goal changed on another device; reload before retrying", 409);
  const writeChatGoal = vi.fn().mockRejectedValueOnce(conflict).mockResolvedValueOnce(cancelled);
  const getChatGoal = vi.fn().mockResolvedValue(advanced);
  const onChange = vi.fn();
  const api = { writeChatGoal, getChatGoal } as unknown as ApiClient;

  render(<DialogProvider><ProviderGoalPanel api={api} sessionId="session" goal={running} onChange={onChange} /></DialogProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Cancel goal" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel goal" }));

  await waitFor(() => expect(writeChatGoal).toHaveBeenCalledTimes(2));
  // The retry carries the revision Core actually holds, not the stale one.
  expect(writeChatGoal.mock.calls[0][1]).toMatchObject({ expectedRevision: 2, action: "cancel" });
  expect(writeChatGoal.mock.calls[1][1]).toMatchObject({ expectedRevision: 7, action: "cancel" });
  expect(onChange).toHaveBeenLastCalledWith(cancelled);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("says what a goal already is instead of silently failing a transition", async () => {
  const running: ChatGoal = { ...draft, status: "running", revision: 2 };
  const completed: ChatGoal = { ...running, status: "completed", revision: 9 };
  const conflict = new ApiError("goal changed on another device; reload before retrying", 409);
  const writeChatGoal = vi.fn().mockRejectedValue(conflict);
  const getChatGoal = vi.fn().mockResolvedValue(completed);
  const onChange = vi.fn();
  const api = { writeChatGoal, getChatGoal } as unknown as ApiClient;

  render(<DialogProvider><ProviderGoalPanel api={api} sessionId="session" goal={running} onChange={onChange} /></DialogProvider>);
  fireEvent.click(screen.getByRole("button", { name: "Pause" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("already completed");
  // The panel shows what Core holds rather than leaving a stale goal on screen.
  expect(onChange).toHaveBeenCalledWith(completed);
  expect(writeChatGoal).toHaveBeenCalledTimes(1);
});

it("counts the active stretch a running goal is still accruing", () => {
  const since = new Date("2026-09-20T12:00:00Z");
  const now = since.getTime() + 90_000;
  const running: ChatGoal = { ...draft, status: "running", elapsedSeconds: 30, activeSince: since.toISOString() };

  // Core banks elapsed time on a transition, so the stored value alone would
  // report the time this goal had when it was last paused.
  expect(activeSeconds(running, now)).toBeCloseTo(120);
  expect(activeSeconds({ ...running, status: "paused" }, now)).toBe(30);
  expect(activeSeconds({ ...running, activeSince: undefined }, now)).toBe(30);
  expect(activeSeconds({ ...running, activeSince: "not a date" }, now)).toBe(30);

  render(<DialogProvider><ProviderGoalPanel api={{} as unknown as ApiClient} sessionId="session" goal={running} onChange={vi.fn()} /></DialogProvider>);
  expect(screen.getByRole("region", { name: "Conversation goal" })).toHaveTextContent(/\ds active/);
});
