import { ArrowUpRight, ChevronRight } from "lucide-react";
import type { ChatGoal, ChatSubagentView, HarnessGoalSnapshot, HarnessSessionActivity } from "../api/types";
import { subagentSummary } from "./chat-subagents/useChatSubagents";

interface ChatStudioRailProps {
  goal?: ChatGoal;
  harnessGoal?: HarnessGoalSnapshot;
  subagents: ChatSubagentView[];
  activity?: HarnessSessionActivity;
  pendingRequests: number;
  sessionId: string;
  goalEditorOpen: boolean;
  canEditGoal: boolean;
  onToggleGoal: () => void;
  onOpenSubagents: () => void;
  onOpenSessionDetails: () => void;
}

const activeStatuses = new Set<ChatSubagentView["status"]>(["running", "waiting_approval", "recovering"]);

export function ChatStudioRail({
  goal, harnessGoal, subagents, activity, pendingRequests, sessionId, goalEditorOpen,
  canEditGoal, onToggleGoal, onOpenSubagents, onOpenSessionDetails,
}: ChatStudioRailProps) {
  const objective = goal?.objective ?? harnessGoal?.objective;
  const goalStatus = goal?.status ?? harnessGoal?.status;
  const counts = subagentSummary(subagents);
  const agents = [...subagents].sort((left, right) =>
    Number(activeStatuses.has(right.status)) - Number(activeStatuses.has(left.status))).slice(0, 3);
  const currentWork = activity?.goal?.currentStep
    ?? activity?.plan?.find((step) => step.status === "in_progress")?.title
    ?? activity?.detail;
  const status = pendingRequests > 0 ? "Action required"
    : activity?.busy ? "Harness working"
    : goal?.status === "running" ? "Goal running"
    : "Conversation ready";

  return <aside className="chat-studio-rail" aria-label="Conversation context">
    <h2>Session</h2>
    <section className="chat-studio-goal" aria-label="Goal">
      <span className="chat-studio-eyebrow">{goalStatus ? `${goalStatus.replaceAll("_", " ")} goal` : "Goal"}</span>
      <strong>{objective || "No active goal"}</strong>
      {goal?.plan.length ? <p>{goal.plan[goal.currentStep] ?? goal.plan.at(-1)}</p>
        : harnessGoal?.currentStep ? <p>{harnessGoal.currentStep}</p>
        : !objective ? <p>Set a goal to keep longer work organized.</p> : null}
      {canEditGoal && <button type="button" className="chat-studio-link" aria-expanded={goalEditorOpen}
        aria-controls="chat-studio-goal-editor" onClick={onToggleGoal}>
        {goalEditorOpen ? "Hide goal details" : goal ? "View goal" : "Create goal"} <ArrowUpRight size={14} aria-hidden="true" />
      </button>}
    </section>

    <section className="chat-studio-agents" aria-label="Subagents">
      <header><h3>Agents</h3>{counts.length > 0 && <span>{counts.map((item) => item.label).join(" · ")}</span>}</header>
      {agents.length ? <ul>{agents.map((agent) => <li key={agent.id}>
        <button type="button" onClick={onOpenSubagents} aria-label={`Open agent ${agent.name}, ${agent.status.replaceAll("_", " ")}`}>
          <span className="chat-studio-agent-dot" data-status={agent.status} aria-hidden="true" />
          <span><strong>{agent.name}</strong><small>{agent.task || agent.status.replaceAll("_", " ")}</small></span>
          <ChevronRight size={15} aria-hidden="true" />
        </button>
      </li>)}</ul> : <p>No agents in this conversation.</p>}
      {subagents.length > agents.length && <button type="button" className="chat-studio-link" onClick={onOpenSubagents}>View all agents <ArrowUpRight size={14} aria-hidden="true" /></button>}
    </section>

    <section className="chat-studio-activity" aria-label="Session activity">
      <h3>Activity</h3>
      <p>{currentWork || (goal?.status === "running" ? "Goal is running." : "No active work.")}</p>
      <button type="button" className="chat-studio-link" onClick={onOpenSessionDetails}>Session details <ArrowUpRight size={14} aria-hidden="true" /></button>
    </section>
    <div className="chat-studio-session-status" role="status" data-attention={pendingRequests > 0}>
      <span className="chat-studio-agent-dot" data-status={pendingRequests > 0 ? "waiting_approval" : activity?.busy ? "running" : "complete"} aria-hidden="true" />
      <span><strong>{status}</strong><small>Session {sessionId.slice(0, 8)}</small></span>
    </div>
  </aside>;
}
