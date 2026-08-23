import type {
  AgentRunSummary,
  ApprovalSummary,
  AssetSummary,
  FindingSummary,
  HealthResponse,
  ReportSummary,
  SetupStatus,
} from "../api/types";
import type { WorkspaceState } from "../state/WorkspaceContext";

export type ZeroModuleTone = "attention" | "active" | "critical" | "warning" | "neutral";
export type ZeroModuleIcon = "approval" | "mission" | "finding" | "setup" | "report" | "project";
export type ZeroModuleAction =
  | { type: "route"; to: string }
  | { type: "activity"; view: "approvals" | "activity" };

export interface ZeroModule {
  id: string;
  priority: number;
  tone: ZeroModuleTone;
  icon: ZeroModuleIcon;
  eyebrow: string;
  title: string;
  detail: string;
  actionLabel: string;
  action: ZeroModuleAction;
}

export interface ZeroModuleState {
  workspaceState: WorkspaceState;
  health?: HealthResponse;
  setupStatus?: SetupStatus;
  engagementName?: string;
  approvals: ApprovalSummary[];
  run?: AgentRunSummary;
  findings: FindingSummary[];
  reports: ReportSummary[];
  assets: AssetSummary[];
}

const activeRunStates = new Set<AgentRunSummary["status"]>([
  "queued",
  "planning",
  "running",
  "waiting_approval",
  "paused",
  "cancelling",
]);
const failedRunStates = new Set<AgentRunSummary["status"]>(["failed", "interrupted"]);
const resolvedFindingStates = new Set<FindingSummary["status"]>([
  "accepted_risk",
  "false_positive",
  "remediated",
  "retest_passed",
]);
const findingSeverity = { critical: 0, high: 1, medium: 2, low: 3, info: 4 } as const;

function formatStatus(value: string): string {
  return value.replaceAll("_", " ");
}

function runtimeModule(state: ZeroModuleState): ZeroModule | undefined {
  if (state.workspaceState === "failed") {
    return {
      id: "runtime-core-failed",
      priority: 400,
      tone: "critical",
      icon: "setup",
      eyebrow: "System readiness",
      title: "Nebula Core is unavailable",
      detail: "Review local setup and reconnect the workspace.",
      actionLabel: "Open setup",
      action: { type: "route", to: "/settings" },
    };
  }
  if (state.workspaceState === "degraded" || state.health?.status === "degraded" || (state.setupStatus && state.setupStatus.core.status !== "ready")) {
    return {
      id: "runtime-core-degraded",
      priority: 400,
      tone: "warning",
      icon: "setup",
      eyebrow: "System readiness",
      title: "Workspace needs attention",
      detail: state.setupStatus?.core.detail ?? "Some local capabilities are currently limited.",
      actionLabel: "Review setup",
      action: { type: "route", to: "/settings" },
    };
  }
  if (state.setupStatus && state.setupStatus.terminal.status !== "ready" && state.setupStatus.terminal.status !== "disabled") {
    return {
      id: "runtime-terminal",
      priority: 410,
      tone: state.setupStatus.terminal.status === "error" ? "critical" : "warning",
      icon: "setup",
      eyebrow: "Terminal readiness",
      title: state.setupStatus.terminal.status === "needs_runner" ? "Choose a sandbox runner" : "Terminal setup is incomplete",
      detail: state.setupStatus.terminal.detail ?? `Terminal is ${formatStatus(state.setupStatus.terminal.status)}.`,
      actionLabel: "Configure",
      action: { type: "route", to: "/settings" },
    };
  }
  const assistant = state.setupStatus?.assistant;
  if (assistant && assistant.status !== "configured") {
    return {
      id: "runtime-assistant",
      priority: 420,
      tone: assistant.status === "error" ? "critical" : "neutral",
      icon: "setup",
      eyebrow: "Assistant readiness",
      title: assistant.status === "needs_model" ? "Connect an assistant model" : "Assistant setup needs attention",
      detail: assistant.detail ?? "Configure a model when you want analyst assistance.",
      actionLabel: "Open setup",
      action: { type: "route", to: "/settings" },
    };
  }
  return undefined;
}

export function deriveZeroModules(state: ZeroModuleState): ZeroModule[] {
  const modules: ZeroModule[] = [];
  const pendingApprovals = state.approvals.filter((approval) => approval.status === "pending");
  if (pendingApprovals.length) {
    const approval = pendingApprovals[0];
    modules.push({
      id: "pending-approvals",
      priority: 100,
      tone: "attention",
      icon: "approval",
      eyebrow: `${pendingApprovals.length} pending approval${pendingApprovals.length === 1 ? "" : "s"}`,
      title: approval.toolName,
      detail: `${approval.agentName} · ${approval.target}`,
      actionLabel: "Review request",
      action: { type: "activity", view: "approvals" },
    });
  }

  if (state.run && (activeRunStates.has(state.run.status) || failedRunStates.has(state.run.status))) {
    const failed = failedRunStates.has(state.run.status);
    const completed = `${state.run.completedTasks} of ${state.run.totalTasks} tasks`;
    modules.push({
      id: `mission-${state.run.id}`,
      priority: 200,
      tone: failed ? "critical" : "active",
      icon: "mission",
      eyebrow: failed ? "Mission needs review" : `Mission ${formatStatus(state.run.status)}`,
      title: state.run.title,
      detail: `${completed} · updated ${new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(new Date(state.run.updatedAt))}`,
      actionLabel: failed ? "Inspect activity" : "Open mission",
      action: { type: "route", to: failed ? "/?view=activity" : "/?view=missions" },
    });
  }

  const priorityFinding = state.findings
    .filter((finding) => (finding.severity === "critical" || finding.severity === "high") && !resolvedFindingStates.has(finding.status))
    .sort((left, right) => findingSeverity[left.severity] - findingSeverity[right.severity] || right.updatedAt.localeCompare(left.updatedAt))[0];
  if (priorityFinding) {
    modules.push({
      id: `finding-${priorityFinding.id}`,
      priority: 300,
      tone: priorityFinding.severity === "critical" ? "critical" : "attention",
      icon: "finding",
      eyebrow: `${priorityFinding.severity} finding · ${formatStatus(priorityFinding.status)}`,
      title: priorityFinding.title,
      detail: `${priorityFinding.affectedAssetCount} affected asset${priorityFinding.affectedAssetCount === 1 ? "" : "s"} · ${priorityFinding.evidenceCount} evidence record${priorityFinding.evidenceCount === 1 ? "" : "s"}`,
      actionLabel: "Open findings",
      action: { type: "route", to: "/findings" },
    });
  }

  const readiness = runtimeModule(state);
  if (readiness) modules.push(readiness);

  const draftReport = [...state.reports]
    .filter((report) => report.status !== "final")
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))[0];
  if (draftReport) {
    modules.push({
      id: `report-${draftReport.id}`,
      priority: 500,
      tone: "neutral",
      icon: "report",
      eyebrow: `${formatStatus(draftReport.status)} report`,
      title: draftReport.title,
      detail: `${draftReport.findingIds.length} linked finding${draftReport.findingIds.length === 1 ? "" : "s"} · updated ${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(draftReport.updatedAt))}`,
      actionLabel: "Continue report",
      action: { type: "route", to: "/reports" },
    });
  }

  if (modules.length < 3) {
    modules.push({
      id: "project-overview",
      priority: 900,
      tone: "neutral",
      icon: "project",
      eyebrow: state.engagementName ? "Active project" : "Project workspace",
      title: state.engagementName ?? "Create or select a project",
      detail: `${state.assets.length} asset${state.assets.length === 1 ? "" : "s"} · ${state.findings.length} finding${state.findings.length === 1 ? "" : "s"} · ${state.reports.length} report${state.reports.length === 1 ? "" : "s"}`,
      actionLabel: "Open overview",
      action: { type: "route", to: "/project" },
    });
  }

  return modules.sort((left, right) => left.priority - right.priority).slice(0, 3);
}
