import { useState } from "react";
import { Check, ChevronDown, ChevronRight, CircleAlert, ExternalLink, LoaderCircle, Split, SquareStop, X } from "lucide-react";
import type { ApiClient } from "../../api/client";
import type { ChatSubagentView } from "../../api/types";
import { logCaughtDiagnostic } from "../../diagnostics";
import { IconAction } from "../IconAction";
import { StandardEmptyState } from "../SurfacePrimitives";
import {
  ACTIVE_STATUSES,
  compactTokens,
  elapsedLabel,
  statusLabel,
  SUBAGENT_SLOTS,
} from "./useChatSubagents";

interface ChatSubagentPaneProps {
  api: ApiClient;
  sessionId: string;
  subagents: ChatSubagentView[];
  error?: string;
  onClose: () => void;
  onOpenConversation: (childSessionId: string) => void;
  onChanged: () => void;
  /** The narrow sheet shows one row per child instead of open cards. */
  compact?: boolean;
  /**
   * A harness chat delegates to a provider model other than its own; name it.
   * Absent for provider chats, whose children share the chat's model.
   */
  harnessDelegation?: { harnessName: string; providerName?: string; model?: string };
}

function statusIcon(status: string) {
  if (status === "running") return <LoaderCircle className="spin" size={15} aria-hidden="true" />;
  if (status === "waiting_approval") return <span className="chat-subagent-dot" data-tone="waiting_approval" aria-hidden="true" />;
  if (status === "completed") return <Check size={15} aria-hidden="true" />;
  return <CircleAlert size={15} aria-hidden="true" />;
}

/**
 * Every child this conversation delegated, and the two decisions an operator
 * has over one: approve the command it is asking for, or stop it. Core owns
 * their lifecycle; nothing here starts work.
 */
export function ChatSubagentPane({
  api, sessionId, subagents, error, onClose, onOpenConversation, onChanged, compact = false, harnessDelegation,
}: ChatSubagentPaneProps) {
  const [expanded, setExpanded] = useState<string[]>([]);
  const [busy, setBusy] = useState<string>();
  const [actionError, setActionError] = useState<string>();

  const active = subagents.filter((item) => ACTIVE_STATUSES.has(item.status));
  const tokens = subagents.reduce((total, item) => total + item.usage.totalTokens, 0);
  const delegateModel = harnessDelegation
    ? harnessDelegation.model ?? [...subagents].reverse().find((item) => item.model)?.model
    : undefined;
  const delegateTarget = [harnessDelegation?.providerName, delegateModel].filter(Boolean).join(" · ");

  const decide = async (subagent: ChatSubagentView, decision: "approve" | "reject") => {
    if (!subagent.approval || busy) return;
    setBusy(subagent.id);
    setActionError(undefined);
    try {
      await api.decideApproval(subagent.approval.id, { decision });
      onChanged();
    } catch (caught) {
      void logCaughtDiagnostic("interface.chat_subagents.decision_failed", "A subagent approval decision failed.", caught, "chat_subagents");
      setActionError(caught instanceof Error ? caught.message : "The decision could not be recorded.");
    } finally {
      setBusy(undefined);
    }
  };

  const stop = async (subagent?: ChatSubagentView) => {
    if (busy) return;
    setBusy(subagent?.id ?? "all");
    setActionError(undefined);
    try {
      if (subagent) await api.stopChatSubagent(sessionId, subagent.id);
      else await api.stopAllChatSubagents(sessionId);
      onChanged();
    } catch (caught) {
      void logCaughtDiagnostic("interface.chat_subagents.stop_failed", "A subagent could not be stopped.", caught, "chat_subagents");
      setActionError(caught instanceof Error ? caught.message : "The subagent could not be stopped.");
    } finally {
      setBusy(undefined);
    }
  };

  const toggle = (id: string) =>
    setExpanded((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);

  return <section className={`chat-subagent-pane${compact ? " compact" : ""}`} aria-label="Subagents">
    <header>
      <div>
        <Split size={15} aria-hidden="true" />
        <strong>Subagents</strong>
        <span className="chat-subagent-total">{subagents.length}</span>
      </div>
      <IconAction icon={X} label="Close subagents" onClick={onClose} />
    </header>

    <p className="chat-subagent-slots">
      {active.length} of {SUBAGENT_SLOTS} slots active{delegateModel ? ` · ${delegateModel}` : ""} · {compactTokens(tokens)} tokens
      <span className="chat-subagent-slot-bar" aria-hidden="true">
        <span style={{ width: `${Math.min(100, (active.length / SUBAGENT_SLOTS) * 100)}%` }} />
      </span>
    </p>

    {error && <p className="chat-subagent-error" role="alert">{error}</p>}
    {actionError && <p className="chat-subagent-error" role="alert">{actionError}</p>}

    {subagents.length === 0 && <StandardEmptyState
      compact
      title="Nothing delegated yet"
      explanation={harnessDelegation
        ? `With provider subagents on, ${harnessDelegation.harnessName} can split independent work across parallel children on ${delegateModel ?? "the chosen provider model"}. They appear here as it does.`
        : "With subagents allowed, the assistant can split independent work across parallel children on this model. They appear here as it does."}
    />}

    <ol className="chat-subagent-list">
      {subagents.map((subagent) => {
        const open = expanded.includes(subagent.id) || subagent.status === "waiting_approval";
        const finished = !ACTIVE_STATUSES.has(subagent.status);
        return <li key={subagent.id} className="chat-subagent-card" data-status={subagent.status}>
          <div className="chat-subagent-card-head">
            <span className="chat-subagent-card-icon" data-status={subagent.status}>{statusIcon(subagent.status)}</span>
            <button
              type="button"
              className="chat-subagent-card-title"
              aria-expanded={open}
              onClick={() => toggle(subagent.id)}
            >
              <strong>{subagent.name}</strong>
              <small>
                {statusLabel(subagent.status)}
                {subagent.status === "running" && subagent.stepCount > 0 ? ` · step ${subagent.stepCount}` : ""}
                {` · ${elapsedLabel(subagent.elapsedSeconds)}`}
                {subagent.usage.totalTokens ? ` · ${compactTokens(subagent.usage.totalTokens)} tokens` : ""}
                {finished && subagent.resultMessageId ? " · result posted" : ""}
              </small>
            </button>
            {open ? <ChevronDown size={15} aria-hidden="true" /> : <ChevronRight size={15} aria-hidden="true" />}
          </div>

          {open && <div className="chat-subagent-card-body">
            {subagent.status === "waiting_approval" && subagent.approval && <div className="chat-subagent-approval">
              <p>{subagent.approval.rationale || `Wants to run ${subagent.approval.tool || "a command"} in the workspace`}</p>
              {subagent.approval.detail && <pre tabIndex={0}><code>{subagent.approval.detail}</code></pre>}
              <div className="chat-subagent-approval-actions">
                <button type="button" className="button primary" disabled={busy === subagent.id} onClick={() => void decide(subagent, "approve")}>Allow once</button>
                <button type="button" className="button secondary" disabled={busy === subagent.id} onClick={() => void decide(subagent, "reject")}>Deny</button>
              </div>
            </div>}

            {!compact && subagent.recentSteps.length > 0 && <ol className="chat-subagent-steps">
              {subagent.recentSteps.map((step, index) => <li key={`${step.tool}:${index}`} data-status={step.status}>
                <span className="chat-subagent-step-icon" aria-hidden="true">
                  {step.status === "complete" ? <Check size={13} /> : <LoaderCircle className="spin" size={13} />}
                </span>
                <code>{step.tool}</code>
                <small>{step.detail}</small>
              </li>)}
            </ol>}

            {!compact && subagent.task && <p className="chat-subagent-task">{subagent.task}</p>}
            {subagent.error && <p className="chat-subagent-error" role="alert">{subagent.error}</p>}
            {finished && subagent.result && <p className="chat-subagent-result">{subagent.result}</p>}

            <div className="chat-subagent-card-actions">
              <button type="button" className="button quiet" onClick={() => onOpenConversation(subagent.childSessionId)}>
                <ExternalLink size={14} aria-hidden="true" /> Open conversation
              </button>
              {!finished && <button type="button" className="button quiet chat-subagent-stop" disabled={busy === subagent.id} onClick={() => void stop(subagent)}>
                <SquareStop size={14} aria-hidden="true" /> Stop
              </button>}
            </div>
          </div>}
        </li>;
      })}
    </ol>

    {active.length > 0 && <button type="button" className="button quiet chat-subagent-stop-all" disabled={busy === "all"} onClick={() => void stop()}>
      <SquareStop size={14} aria-hidden="true" /> Stop all subagents
    </button>}

    <p className="chat-subagent-note">
      {harnessDelegation
        ? `Subagents run on ${delegateTarget || "the chosen provider model"} with this project's tools and approval policy. Their tool outputs go to ${harnessDelegation.providerName ?? "that provider"}. They cannot start their own subagents.`
        : "Subagents use this conversation's model, tools and approval policy. They cannot start their own subagents."}
    </p>
  </section>;
}
