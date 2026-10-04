import { ArrowUpRight, ChevronRight } from "lucide-react";
import type { ChatGoal, ChatSubagentView, HarnessGoalSnapshot } from "../api/types";
import { subagentSummary } from "./chat-subagents/useChatSubagents";

interface ChatStudioRailProps {
  goal?: ChatGoal;
  harnessGoal?: HarnessGoalSnapshot;
  subagents: ChatSubagentView[];
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
  goal, harnessGoal, subagents, pendingRequests, sessionId, goalEditorOpen,
  canEditGoal, onToggleGoal, onOpenSubagents, onOpenSessionDetails,
}: ChatStudioRailProps) {
  const objective = goal?.objective ?? harnessGoal?.objective;
  const goalStatus = goal?.status ?? harnessGoal?.status;
  const counts = subagentSummary(subagents);
  const agents = [...subagents].sort((left, right) =>
    Number(activeStatuses.has(right.status)) - Number(activeStatuses.has(left.status))).slice(0, 3);
  return <aside className="chat-studio-rail" aria-label="Conversation context">
    <h2>Session</h2>
    <section className="chat-studio-goal" aria-label="Goal">
      <div className="chat-studio-goal-state">
        <span className="chat-studio-eyebrow">{goalStatus === "running" ? "Active goal" : "Goal"}</span>
        {goalStatus && <small data-status={goalStatus}>{goalStatus.replaceAll("_", " ")}</small>}
      </div>
      <strong className="chat-studio-goal-summary" title={objective}>{objective || "No active goal"}</strong>
      {canEditGoal && <button type="button" className="chat-studio-link" aria-expanded={goalEditorOpen}
        aria-controls="chat-studio-goal-editor" onClick={onToggleGoal}>
        {goalEditorOpen ? "Hide full goal" : goal ? "View full goal and evidence gates" : "Create goal"} <ArrowUpRight size={14} aria-hidden="true" />
      </button>}
      {!canEditGoal && objective && <details className="chat-studio-goal-details">
        <summary>View full goal</summary>
        <p>{objective}</p>
        {harnessGoal?.currentStep && <p>Current step: {harnessGoal.currentStep}</p>}
      </details>}
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

    <button type="button" className="chat-studio-link chat-studio-session-link" onClick={onOpenSessionDetails}>Session details <ArrowUpRight size={14} aria-hidden="true" /></button>
    {pendingRequests > 0 && <div className="chat-studio-session-status" role="status" data-attention>
      <span className="chat-studio-agent-dot" data-status="waiting_approval" aria-hidden="true" />
      <span><strong>Action required</strong><small>Session {sessionId.slice(0, 8)}</small></span>
    </div>}
  </aside>;
}
