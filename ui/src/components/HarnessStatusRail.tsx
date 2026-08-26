import { useId, useState } from "react";
import { Check, ChevronDown, Circle, LoaderCircle, OctagonAlert } from "lucide-react";
import type { HarnessPlanEntry, HarnessSessionActivity } from "../api/types";

interface Props {
  activity: HarnessSessionActivity;
  pendingRequests: number;
}

const statusLabels: Record<HarnessPlanEntry["status"], string> = {
  pending: "Pending",
  in_progress: "In progress",
  completed: "Completed",
  blocked: "Blocked",
};

function PlanStatusIcon({ status }: { status: HarnessPlanEntry["status"] }) {
  if (status === "completed") return <Check size={13} aria-hidden="true" />;
  if (status === "in_progress") return <LoaderCircle className="spin" size={13} aria-hidden="true" />;
  if (status === "blocked") return <OctagonAlert size={13} aria-hidden="true" />;
  return <Circle size={11} aria-hidden="true" />;
}

export function HarnessStatusRail({ activity, pendingRequests }: Props) {
  const [expanded, setExpanded] = useState(false);
  const planId = useId();
  const plan = activity.plan ?? [];
  const completed = plan.filter((entry) => entry.status === "completed").length;
  const status = !activity.live ? "Connection unavailable" : pendingRequests ? "Action required" : "Working";
  const summary = <>
    <span className={`status-dot ${activity.live ? activity.busy ? "pending" : "available" : "unavailable"}`} />
    <strong>{status}</strong>
    {activity.goal && <span title={activity.goal.objective}>Goal: {activity.goal.status}{typeof activity.goal.progress === "number" ? ` · ${Math.round(activity.goal.progress * 100)}%` : ""}</span>}
    {plan.length > 0 && <span>{completed}/{plan.length} plan steps</span>}
    {pendingRequests > 0 && <span>{pendingRequests} request{pendingRequests === 1 ? "" : "s"}</span>}
  </>;

  return <section className={`harness-status-rail${expanded ? " expanded" : ""}`} aria-label="Harness status">
    {plan.length > 0 ? <button
      className="harness-status-summary"
      type="button"
      aria-expanded={expanded}
      aria-controls={planId}
      aria-label={`${expanded ? "Collapse" : "Expand"} plan steps, ${completed} of ${plan.length} completed`}
      onClick={() => setExpanded((value) => !value)}
    >{summary}<ChevronDown className="harness-status-chevron" size={15} aria-hidden="true" /></button> : <div className="harness-status-summary">{summary}</div>}
    {plan.length > 0 && expanded && <ol id={planId} className="harness-plan-steps" aria-label="Plan steps">
      {plan.map((entry, index) => <li className={`status-${entry.status}`} key={entry.id || `${index}-${entry.title}`}>
        <span className="harness-plan-marker"><PlanStatusIcon status={entry.status} /></span>
        <span><strong>{entry.title}</strong><small>{statusLabels[entry.status]}</small></span>
      </li>)}
    </ol>}
  </section>;
}
