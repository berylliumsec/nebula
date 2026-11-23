import type { ChatSessionSummary } from "./api/types";

/** Core's ``ButlerRecoveryCase.status``. */
export type ButlerCaseStatus = "observed" | "investigating" | "verifying" | "resolved" | "needs_attention" | "uncertain" | "cancelled";
export const BUTLER_CASE_STATUSES: readonly ButlerCaseStatus[] = ["observed", "investigating", "verifying", "resolved", "needs_attention", "uncertain", "cancelled"];

/** The stage label a Butler conversation shows. */
export type ButlerStageKey = "investigating" | "needs-you" | "uncertain" | "resolved" | "closed" | "active" | "idle";

export interface ButlerStage {
  key: ButlerStageKey;
  label: string;
  /** Why the conversation is in this stage, for the tooltip and screen readers. */
  description: string;
}

const STAGES: Record<ButlerStageKey, { label: string }> = {
  investigating: { label: "Investigating" },
  "needs-you": { label: "Needs you" },
  uncertain: { label: "Uncertain" },
  resolved: { label: "Resolved" },
  closed: { label: "Closed" },
  active: { label: "Active" },
  idle: { label: "Idle" },
};

const stage = (key: ButlerStageKey, description: string): ButlerStage => ({ key, label: STAGES[key].label, description });

export function parseButlerCaseStatus(value: unknown): ButlerCaseStatus | undefined {
  return BUTLER_CASE_STATUSES.includes(value as ButlerCaseStatus) ? value as ButlerCaseStatus : undefined;
}

/** A Butler recovery case's stage, from Core's case status. */
export function butlerCaseStage(status: ButlerCaseStatus): ButlerStage {
  switch (status) {
    case "observed": return stage("investigating", "Butler has picked up this problem and will investigate it");
    case "investigating": return stage("investigating", "Butler is investigating this problem");
    case "verifying": return stage("investigating", "Butler made a repair; Core is checking that the work recovered");
    case "needs_attention": return stage("needs-you", "Butler could not repair this and needs your decision");
    case "uncertain": return stage("uncertain", "Butler acted, but Core could not confirm the repair");
    case "resolved": return stage("resolved", "Core verified that the work recovered");
    case "cancelled": return stage("closed", "This check was superseded or no longer applies");
  }
}

/** The project's own Butler conversation: not a case chat, thread or Butler subagent. */
export function isProjectButlerChat(session: ChatSessionSummary): boolean {
  return Boolean(session.isControlAssistant && !session.isSubagent && !session.butlerRecoveryCaseId && !session.isButlerThread);
}

/** A chat Butler opened to investigate one recovery case. */
export function isButlerCaseChat(session: ChatSessionSummary): boolean {
  return Boolean(session.butlerRecoveryCaseId && !session.isSubagent);
}

/**
 * The stage a Butler conversation shows in the list and its header, or
 * undefined for every other chat. Case chats follow their case; the project's
 * Butler chat and Butler threads follow their activity.
 */
export function butlerConversationStage(session: ChatSessionSummary, activity: "working" | "waiting" | "idle" | undefined): ButlerStage | undefined {
  if (isButlerCaseChat(session)) return session.butlerCaseStatus ? butlerCaseStage(session.butlerCaseStatus) : undefined;
  if (!session.isControlAssistant || session.isSubagent) return undefined;
  if (activity === "waiting") return stage("needs-you", "Butler is waiting for you in this conversation");
  if (activity === "working") return stage("active", "Butler is working in this conversation");
  return stage("idle", "Nothing is running or waiting in this conversation");
}

/** Whether a case chat waits on the operator, for the list's Needs you grouping. */
export function caseNeedsOperator(session: ChatSessionSummary): boolean {
  return isButlerCaseChat(session) && session.butlerCaseStatus === "needs_attention";
}
