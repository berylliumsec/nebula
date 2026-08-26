import type { AgentRunSummary, RunEvent, ToolArtifactReference } from "../api/types";
import type { HarnessActivityItem } from "../pages/harnessActivity";

export type ActivityLedgerStatus =
  | "queued"
  | "active"
  | "complete"
  | "attention"
  | "failed"
  | "cancelled";

export type ActivityLedgerPhaseKey =
  | "planning"
  | "research"
  | "execution"
  | "changes"
  | "delegation"
  | "outputs"
  | "verification"
  | "activity";

export interface ActivityLedgerOutput {
  label: string;
  content: string;
}

export interface ActivityLedgerEntry {
  id: string;
  source: "harness" | "native" | "mission";
  phase: ActivityLedgerPhaseKey;
  status: ActivityLedgerStatus;
  statusLabel?: string;
  label: string;
  summary?: string;
  brief?: string;
  occurredAt?: string;
  sequence: number;
  kind?: string;
  countsAsAction: boolean;
  artifactIds: string[];
  evidenceIds: string[];
  outputs: ActivityLedgerOutput[];
  payload: Record<string, unknown>;
  usageLabel?: string;
  sourceItem?: HarnessActivityItem;
  sourceTool?: NativeActivitySource;
  sourceEvent?: RunEvent;
}

export interface ActivityLedgerPhase {
  key: string;
  label: string;
  status: ActivityLedgerStatus;
  completed: number;
  total: number;
}

export interface ActivityLedgerViewModel {
  title: string;
  status: ActivityLedgerStatus;
  entries: ActivityLedgerEntry[];
  phases: ActivityLedgerPhase[];
  currentAction?: string;
  actionCount: number;
  attentionCount: number;
  artifactCount: number;
  durationMs?: number;
  completedTasks?: number;
  totalTasks?: number;
}

export interface NativeActivitySource {
  assistantId: string;
  toolCallId: string;
  capability: string;
  status: string;
  summary?: string;
  evidenceIds: string[];
  resultArtifactId?: string;
  artifacts: ToolArtifactReference[];
  receipt?: Record<string, unknown>;
}

const PHASE_LABELS: Record<ActivityLedgerPhaseKey, string> = {
  planning: "Planning and analysis",
  research: "Research",
  execution: "Tools and execution",
  changes: "Workspace changes",
  delegation: "Delegated work",
  outputs: "Recorded outputs",
  verification: "Verification and recovery",
  activity: "Activity",
};

const PHASE_ORDER: ActivityLedgerPhaseKey[] = [
  "planning",
  "research",
  "execution",
  "changes",
  "delegation",
  "outputs",
  "verification",
  "activity",
];

const INTERNAL_LABELS = new Set([
  "item upsert",
  "item_upsert",
  "tool",
  "notice",
  "reasoning",
  "commentary",
  "output delta",
  "output_delta",
  "tool started",
  "tool_started",
  "tool completed",
  "tool_completed",
]);

const ACTIVE_STATUSES = new Set(["running", "streaming", "in_progress", "started", "planning", "connecting", "cancelling"]);
const COMPLETE_STATUSES = new Set(["complete", "completed", "success", "succeeded", "verified", "resolved"]);
const ATTENTION_STATUSES = new Set(["waiting_approval", "waiting_input", "blocked", "paused", "warning"]);
const FAILED_STATUSES = new Set(["failed", "error", "interrupted", "verification_failed"]);
const CANCELLED_STATUSES = new Set(["cancelled", "canceled", "declined", "denied"]);

export function normalizeActivityStatus(status: string | undefined): ActivityLedgerStatus {
  const normalized = (status ?? "").toLowerCase().replaceAll("-", "_");
  if (COMPLETE_STATUSES.has(normalized)) return "complete";
  if (ATTENTION_STATUSES.has(normalized)) return "attention";
  if (FAILED_STATUSES.has(normalized)) return "failed";
  if (CANCELLED_STATUSES.has(normalized)) return "cancelled";
  if (normalized === "queued" || normalized === "pending") return "queued";
  return ACTIVE_STATUSES.has(normalized) || !normalized ? "active" : "active";
}

function sourceStatusLabel(status: string | undefined): string | undefined {
  if (!status) return undefined;
  switch (status.toLowerCase().replaceAll("-", "_")) {
    case "waiting_approval": return "Waiting for approval";
    case "waiting_input": return "Waiting for input";
    case "in_progress": return "In progress";
    case "streaming": return "Streaming";
    case "interrupted": return "Interrupted";
    case "verification_failed": return "Verification failed";
    case "cancelled":
    case "canceled": return "Cancelled";
    case "completed":
    case "complete":
    case "success": return "Completed";
    case "running": return "Running";
    case "blocked": return "Blocked";
    case "failed":
    case "error": return "Failed";
    default: return status.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
  }
}

function phaseForKind(kind: string | undefined, type = ""): ActivityLedgerPhaseKey {
  switch (kind) {
    case "reasoning":
    case "plan":
    case "goal":
    case "mode": return "planning";
    case "web_search":
    case "browser": return "research";
    case "command":
    case "tool":
    case "skill":
    case "image": return "execution";
    case "file_change": return "changes";
    case "subagent": return "delegation";
    case "review":
    case "hook":
    case "compaction": return "verification";
  }
  if (/^(finding|evidence)\./.test(type)) return "outputs";
  if (/^(approval)\./.test(type) || /(failed|blocked|cancelled|interrupted)/.test(type)) return "verification";
  if (/^(tool)\./.test(type)) return "execution";
  if (/^(task|stage|agent)\./.test(type)) return "execution";
  if (/^(run)\./.test(type)) return "planning";
  return "activity";
}

function firstSentence(value: string | undefined): string | undefined {
  const line = value?.split(/\r?\n/)
    .map((part) => part.trim())
    .filter((part) => part && !part.startsWith("```") && !/^(summary|result|activity):?$/i.test(part.replaceAll(/[*_#`]/g, "")))
    .map((part) => part.replace(/^[-*+]\s+/, "").replace(/^#+\s*/, "").replaceAll(/[*_`]/g, ""))
    .find(Boolean);
  const compact = line?.replaceAll(/\s+/g, " ").trim();
  if (!compact) return undefined;
  const match = compact.match(/^.*?(?:[.!?](?:\s|$)|$)/);
  return (match?.[0] ?? compact).trim().slice(0, 240);
}

function meaningfulLabel(title: string | undefined, summary: string | undefined, phase: ActivityLedgerPhaseKey): string {
  const cleanTitle = title?.replaceAll("_", " ").trim();
  if (cleanTitle && !INTERNAL_LABELS.has(cleanTitle.toLowerCase())) return cleanTitle;
  return firstSentence(summary) ?? PHASE_LABELS[phase];
}

function harnessUsageLabel(item: HarnessActivityItem): string | undefined {
  if (!item.usage?.totalTokens) return undefined;
  const parts = [`${item.usage.totalTokens.toLocaleString()} tokens`];
  if (item.usage.durationMs) parts.push(`${(item.usage.durationMs / 1000).toFixed(1)}s`);
  return parts.join(" · ");
}

export function harnessLedgerEntries(items: HarnessActivityItem[]): ActivityLedgerEntry[] {
  return items.map((item) => {
    const phase = phaseForKind(item.kind, item.type);
    const commentary = item.streams.commentary;
    const reasoningSummary = item.streams.reasoning_summary;
    const summary = commentary || reasoningSummary || item.summary;
    return {
      id: item.key,
      source: "harness" as const,
      phase,
      status: normalizeActivityStatus(item.status),
      statusLabel: sourceStatusLabel(item.status),
      label: meaningfulLabel(item.title, summary, phase),
      summary,
      brief: firstSentence(summary),
      occurredAt: item.occurredAt,
      sequence: item.sequence,
      kind: item.kind,
      countsAsAction: Boolean(item.kind && !["reasoning", "plan", "goal", "mode", "hook", "compaction"].includes(item.kind)),
      artifactIds: item.artifactIds,
      evidenceIds: [],
      outputs: Object.entries(item.streams)
        .filter(([stream]) => stream !== "reasoning_summary" && stream !== "commentary")
        .map(([label, content]) => ({ label, content })),
      payload: item.payload,
      usageLabel: harnessUsageLabel(item),
      sourceItem: item,
    };
  });
}

export function nativeLedgerEntries(items: NativeActivitySource[]): ActivityLedgerEntry[] {
  return items.map((item, index) => ({
    id: `native:${item.toolCallId}`,
    source: "native" as const,
    phase: "execution" as const,
    status: normalizeActivityStatus(item.status),
    statusLabel: sourceStatusLabel(item.status),
    label: meaningfulLabel(item.capability, item.summary, "execution"),
    summary: item.summary,
    brief: firstSentence(item.summary),
    sequence: index,
    kind: "tool",
    countsAsAction: true,
    artifactIds: [...new Set([
      ...item.artifacts.map((artifact) => artifact.artifactId),
      ...(item.resultArtifactId ? [item.resultArtifactId] : []),
    ])],
    evidenceIds: item.evidenceIds,
    outputs: [],
    payload: item.receipt ?? {},
    sourceTool: item,
  }));
}

function missionHarnessFields(event: RunEvent): Record<string, unknown> {
  const payload = event.payload;
  const nested = payload.payload;
  return {
    ...payload,
    payload: nested && typeof nested === "object" && !Array.isArray(nested) ? nested : {},
  };
}

function missionEventIdentity(event: RunEvent, fields: Record<string, unknown>, harnessTurnId: string, itemId: string | undefined): string {
  if (event.kind.startsWith("harness.") && itemId) return `mission:harness:${harnessTurnId}:${itemId}`;
  const prefix = event.kind.split(".")[0] ?? "activity";
  for (const key of ["task_id", "tool_call_id", "approval_id", "finding_id", "evidence_id", "stage_id"]) {
    const value = fields[key];
    if (typeof value === "string" && value) return `mission:${prefix}:${value}`;
  }
  return `mission:${event.id}`;
}

export function missionLedgerEntries(events: RunEvent[]): ActivityLedgerEntry[] {
  const byIdentity = new Map<string, ActivityLedgerEntry>();
  for (const event of [...events].sort((left, right) => left.sequence - right.sequence)) {
    const fields = missionHarnessFields(event);
    const harness = event.kind.startsWith("harness.");
    const kind = typeof fields.item_kind === "string" ? fields.item_kind : undefined;
    const itemId = typeof fields.item_id === "string" ? fields.item_id : undefined;
    const harnessTurnId = typeof fields.harness_turn_id === "string" ? fields.harness_turn_id : "turn";
    const phase = phaseForKind(kind, event.kind);
    const rawTitle = typeof fields.title === "string" ? fields.title : undefined;
    const rawSummary = typeof fields.summary === "string" ? fields.summary : event.summary;
    const delta = typeof fields.delta === "string" ? fields.delta : undefined;
    const stream = typeof fields.stream === "string" ? fields.stream : "output";
    const severity = typeof fields.severity === "string" ? fields.severity.toLowerCase() : undefined;
    const rawStatus = typeof fields.item_status === "string"
      ? fields.item_status
        : event.kind.endsWith(".completed") || event.kind.endsWith(".verified") || event.kind.endsWith(".resolved")
          ? "completed"
          : event.kind.endsWith(".failed")
            ? "failed"
            : event.kind.endsWith(".cancelled")
              ? "cancelled"
              : severity === "error" || severity === "critical"
                ? "failed"
                : severity === "warning"
                  ? "warning"
              : event.kind.includes("approval")
                ? "waiting_approval"
                : "running";
    const status = normalizeActivityStatus(rawStatus);
    const identity = missionEventIdentity(event, fields, harnessTurnId, itemId);
    const existing = byIdentity.get(identity);
    const nestedPayload = fields.payload && typeof fields.payload === "object" && !Array.isArray(fields.payload)
      ? fields.payload as Record<string, unknown>
      : {};
    const artifactIds = Array.isArray(fields.artifact_ids)
      ? fields.artifact_ids.filter((id): id is string => typeof id === "string")
      : [];
    const next: ActivityLedgerEntry = {
      id: identity,
      source: "mission",
      phase,
      status,
      statusLabel: sourceStatusLabel(rawStatus),
      label: meaningfulLabel(rawTitle, rawSummary, phase),
      summary: rawSummary,
      brief: firstSentence(rawSummary),
      occurredAt: event.occurredAt,
      sequence: event.sequence,
      kind,
      countsAsAction: harness
        ? Boolean(kind && !["reasoning", "plan", "goal", "mode", "hook", "compaction"].includes(kind))
        : !event.kind.startsWith("run.") && event.kind !== "system.notice",
      artifactIds: [...new Set([...(existing?.artifactIds ?? []), ...artifactIds])],
      evidenceIds: [],
      outputs: [
        ...(existing?.outputs ?? []),
        ...(delta ? [{ label: stream, content: delta }] : []),
      ],
      payload: { ...(existing?.payload ?? {}), ...nestedPayload },
      sourceEvent: event,
    };
    byIdentity.set(identity, next);
  }
  return [...byIdentity.values()].sort((left, right) => left.sequence - right.sequence);
}

function overallStatus(value: string, entries: ActivityLedgerEntry[]): ActivityLedgerStatus {
  const normalized = normalizeActivityStatus(value);
  if (["complete", "failed", "cancelled"].includes(normalized)) return normalized;
  if (entries.some((entry) => entry.status === "failed" || entry.status === "attention")) return "attention";
  return normalized;
}

function phaseStatus(entries: ActivityLedgerEntry[]): ActivityLedgerStatus {
  if (entries.some((entry) => entry.status === "failed")) return "failed";
  if (entries.some((entry) => entry.status === "attention")) return "attention";
  if (entries.some((entry) => entry.status === "active")) return "active";
  if (entries.some((entry) => entry.status === "queued")) return "queued";
  if (entries.some((entry) => entry.status === "cancelled")) return "cancelled";
  return "complete";
}

function explicitPlanPhases(items: HarnessActivityItem[]): ActivityLedgerPhase[] {
  const latest = [...items].reverse().find((item) => item.plan?.length)?.plan;
  if (!latest?.length) return [];
  return latest.map((entry) => ({
    key: `plan:${entry.id}`,
    label: entry.title,
    status: normalizeActivityStatus(entry.status),
    completed: entry.status === "completed" ? 1 : 0,
    total: 1,
  }));
}

function missionStagePhases(run: AgentRunSummary | undefined, entries: ActivityLedgerEntry[]): ActivityLedgerPhase[] {
  if (!run?.stages?.length) return [];
  const completedStages = entries.filter((entry) => entry.sourceEvent?.kind === "stage.completed").length;
  const allComplete = run.status === "complete";
  return run.stages.map((stage, index) => {
    const completed = allComplete || index < completedStages;
    const active = !completed && run.status === "running" && index === Math.min(completedStages, run.stages!.length - 1);
    return {
      key: `stage:${index}`,
      label: stage.title,
      status: completed ? "complete" : active ? "active" : run.status === "failed" && index === completedStages ? "failed" : "queued",
      completed: completed ? 1 : 0,
      total: 1,
    };
  });
}

export function buildActivityLedger(options: {
  title: string;
  status: string;
  entries: ActivityLedgerEntry[];
  harnessItems?: HarnessActivityItem[];
  run?: AgentRunSummary;
}): ActivityLedgerViewModel {
  const uniqueEntries = [...new Map(options.entries.map((entry) => [entry.id, entry])).values()]
    .sort((left, right) => left.sequence - right.sequence);
  const explicitPhases = options.harnessItems ? explicitPlanPhases(options.harnessItems) : missionStagePhases(options.run, uniqueEntries);
  const phases = explicitPhases.length ? explicitPhases : PHASE_ORDER.flatMap((key) => {
    const entries = uniqueEntries.filter((entry) => entry.phase === key && (entry.countsAsAction || key === "planning" || entry.status !== "complete"));
    if (!entries.length) return [];
    return [{
      key,
      label: PHASE_LABELS[key],
      status: phaseStatus(entries),
      completed: entries.filter((entry) => entry.status === "complete").length,
      total: entries.length,
    }];
  });
  const newestFirst = [...uniqueEntries].reverse();
  const current = newestFirst.find((entry) => entry.status === "attention" || entry.status === "failed")
    ?? newestFirst.find((entry) => entry.status === "active" || entry.countsAsAction || entry.summary);
  const goal = options.harnessItems
    ? [...options.harnessItems].reverse().find((item) => item.goal?.currentStep || item.goal?.objective)?.goal
    : undefined;
  const artifacts = new Set(uniqueEntries.flatMap((entry) => [...entry.artifactIds, ...entry.evidenceIds]));
  const occurred = uniqueEntries.map((entry) => entry.occurredAt ? Date.parse(entry.occurredAt) : Number.NaN).filter(Number.isFinite);
  const usageDuration = options.harnessItems?.reduce((maximum, item) => Math.max(maximum, item.usage?.durationMs ?? 0), 0) ?? 0;
  const timestampDuration = occurred.length > 1 ? Math.max(...occurred) - Math.min(...occurred) : 0;
  return {
    title: options.title,
    status: overallStatus(options.status, uniqueEntries),
    entries: [...uniqueEntries].reverse(),
    phases,
    currentAction: current?.label || goal?.currentStep || goal?.objective,
    actionCount: uniqueEntries.filter((entry) => entry.countsAsAction).length,
    attentionCount: uniqueEntries.filter((entry) => ["attention", "failed"].includes(entry.status)).length,
    artifactCount: artifacts.size,
    durationMs: usageDuration || timestampDuration || undefined,
    completedTasks: options.run?.completedTasks,
    totalTasks: options.run?.totalTasks,
  };
}

export function activityLedgerFromHarness(title: string, status: string, items: HarnessActivityItem[]): ActivityLedgerViewModel {
  return buildActivityLedger({ title, status, entries: harnessLedgerEntries(items), harnessItems: items });
}

export function activityLedgerFromNative(title: string, status: string, items: NativeActivitySource[]): ActivityLedgerViewModel {
  return buildActivityLedger({ title, status, entries: nativeLedgerEntries(items) });
}

export function activityLedgerFromMission(run: AgentRunSummary, events: RunEvent[]): ActivityLedgerViewModel {
  return buildActivityLedger({ title: "Mission activity", status: run.status, entries: missionLedgerEntries(events), run });
}

export function activityLedgerStatusLabel(status: ActivityLedgerStatus): string {
  switch (status) {
    case "active": return "Running";
    case "complete": return "Completed";
    case "attention": return "Attention needed";
    case "failed": return "Failed";
    case "cancelled": return "Cancelled";
    case "queued": return "Queued";
  }
}
