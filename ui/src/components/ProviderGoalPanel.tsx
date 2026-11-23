import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, ChevronDown, CirclePause, CirclePlay, Flag, LoaderCircle, MessageSquarePlus, OctagonX, Pencil, RotateCcw, X } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { ChatGoal, GoalGuidanceDelivery, HarnessSkillSummary } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { useConfirmation } from "./DialogSystem";

type GoalAction = "start" | "pause" | "resume" | "cancel" | "block" | "complete";

export const DEFAULT_GOAL_RESUME_INSTRUCTION = "Resume work on the active conversation goal now. Review the latest durable progress and continue without waiting for another operator message.";

/**
 * Active seconds as Core would compute them: what it has banked, plus the
 * stretch still running. Reading the stored field alone leaves a running goal
 * reporting the time it had at its last pause — usually zero.
 */
export function activeSeconds(goal: ChatGoal, now = Date.now()): number {
  if (goal.status !== "running" || !goal.activeSince) return goal.elapsedSeconds;
  const since = Date.parse(goal.activeSince);
  if (Number.isNaN(since)) return goal.elapsedSeconds;
  return goal.elapsedSeconds + Math.max(0, (now - since) / 1_000);
}

/** Lightweight display estimate used only until Core reports exact usage. */
export function estimateLiveTokens(text: string): number {
  return estimateTokensFromBytes(utf8Length(text));
}

/** The same estimate from a running byte count, so a stream adds only its new chunk. */
export function estimateTokensFromBytes(bytes: number): number {
  return bytes > 0 ? Math.max(1, Math.ceil(bytes / 4)) : 0;
}

/** UTF-8 length of a string without encoding it. */
export function utf8Length(text: string): number {
  let bytes = 0;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length
      && text.charCodeAt(index + 1) >= 0xdc00 && text.charCodeAt(index + 1) <= 0xdfff) {
      // A surrogate pair is one four-byte code point.
      bytes += 4;
      index += 1;
    } else bytes += 3;
  }
  return bytes;
}

/** Which goal states each transition is allowed from, as Core enforces them. */
const ALLOWED_FROM: Record<GoalAction, ChatGoal["status"][]> = {
  start: ["draft"],
  pause: ["running"],
  resume: ["paused", "blocked"],
  cancel: ["draft", "running", "paused", "blocked"],
  block: ["running"],
  // A blocked goal can complete only while it holds an agent's completion for review.
  complete: ["running", "blocked"],
};

export const GOAL_NOTE_CHARACTERS = 4_000;

/** What the operator is told about when the agent hears a saved note. */
export const NOTE_DELIVERY_STATUS: Record<GoalGuidanceDelivery, string> = {
  next_step: "Note saved · the agent reads it before its next step",
  running_turn: "Note sent to the running turn",
  next_turn: "Note saved · applies from the next turn",
};

const STATE_NAMES: Record<ChatGoal["status"], string> = {
  draft: "a draft",
  running: "running",
  paused: "paused",
  blocked: "blocked",
  completed: "completed",
  cancelled: "cancelled",
};

export interface ProviderGoalDraft {
  objective: string;
  completionCriteria: string[];
  plan: string[];
  resumeInstruction?: string;
  tokenBudget?: number;
  timeBudgetSeconds?: number;
  stepBudget?: number;
  childBudget?: number;
  completionRequiresReview?: boolean;
}

/** The goal editor's closure-review choice; the agent's own completion then waits for approval. */
function ReviewChoice({ checked, onChange }: { checked: boolean; onChange(value: boolean): void }) {
  return <label className="chat-goal-review-choice">
    <input type="checkbox" checked={checked} onChange={event => onChange(event.target.checked)} />
    <span>Hold completion for review<small>The agent's completion waits for your approval.</small></span>
  </label>;
}

export function ProviderGoalPanel({ api, sessionId, goal, skills, liveTokenEstimate, tokenBudgetAvailable = true, settingsBusy = false, showFullGoal = false, onCreate, onChange, onWorkDispatched }: {
  api: ApiClient;
  sessionId?: string;
  goal?: ChatGoal;
  skills?: HarnessSkillSummary[];
  liveTokenEstimate?: number;
  tokenBudgetAvailable?: boolean;
  settingsBusy?: boolean;
  showFullGoal?: boolean;
  onCreate?(draft: ProviderGoalDraft): Promise<ChatGoal>;
  onChange(goal: ChatGoal): void;
  onWorkDispatched?(): Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(true);
  const [objectiveOpen, setObjectiveOpen] = useState(false);
  useEffect(() => { if (showFullGoal) setObjectiveOpen(true); }, [showFullGoal]);
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [plan, setPlan] = useState("");
  const [resumeInstruction, setResumeInstruction] = useState(DEFAULT_GOAL_RESUME_INSTRUCTION);
  const invalidResumeInstruction = !resumeInstruction.trim() || resumeInstruction.trim().length > 4_000;
  const [tokenBudget, setTokenBudget] = useState("");
  const [timeBudget, setTimeBudget] = useState("");
  const invalidTimeBudget = timeBudget !== "" && (!Number.isInteger(Number(timeBudget)) || Number(timeBudget) < 30);
  const [stepBudget, setStepBudget] = useState("");
  const [childBudget, setChildBudget] = useState("");
  const [requiresReview, setRequiresReview] = useState(false);
  const [transition, setTransition] = useState<"block" | "complete" | "send_back">();
  const [reason, setReason] = useState("");
  const [summary, setSummary] = useState("");
  const [evidence, setEvidence] = useState("");
  const [editingSkills, setEditingSkills] = useState(false);
  const [editingGoal, setEditingGoal] = useState(false);
  const [addingNote, setAddingNote] = useState(false);
  const [noteText, setNoteText] = useState("");
  const [noteStatus, setNoteStatus] = useState<string>();
  const editFormRef = useRef<HTMLDivElement>(null);
  const [selectedSkillPaths, setSelectedSkillPaths] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const confirm = useConfirmation();
  useLayoutEffect(() => {
    if (!editingGoal) return;
    const form = editFormRef.current;
    const objective = form?.querySelector<HTMLTextAreaElement>("textarea");
    if (!form || !objective) return;
    const viewport = form.closest<HTMLElement>(".chat-composer-context");
    if (viewport) {
      viewport.scrollTop += form.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 8;
    } else {
      form.scrollIntoView?.({ block: "start", inline: "nearest" });
    }
    objective.focus({ preventScroll: true });
  }, [editingGoal]);
  // The active stretch advances between reads, so the panel ticks it locally.
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    if (goal?.status !== "running") return;
    const timer = window.setInterval(() => setTick(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [goal?.status]);

  const act = async (action: GoalAction) => {
    if (!goal || !sessionId || busy || settingsBusy) return;
    setBusy(true); setError(undefined);
    // Approving a held completion sends no summary: Core completes the goal
    // with the agent's saved proposal. Sending it back resumes with feedback.
    const approving = action === "complete" && Boolean(goal.completionProposal) && transition !== "complete";
    const body = (revision: number) => ({
      expectedRevision: revision,
      action,
      ...(action === "block" || (action === "resume" && transition === "send_back") ? { reason: reason.trim() || undefined } : {}),
      ...(action === "complete" && !approving ? {
        completionSummary: summary.trim(),
        completionEvidence: evidence.split("\n").map(item => item.trim()).filter(Boolean).map(detail => ({ kind: "operator_confirmation", detail })),
      } : {}),
    });
    const settle = (updated: ChatGoal) => {
      onChange(updated);
      if ((action === "start" || action === "resume") && onWorkDispatched) {
        void onWorkDispatched().catch((caught) => {
          void logCaughtDiagnostic("interface.goal.dispatch_follow_failed", "The dispatched goal turn could not be followed immediately.", caught, "goal");
        });
      }
      setTransition(undefined); setReason(""); setSummary(""); setEvidence("");
    };
    try {
      settle(await api.writeChatGoal(sessionId, body(goal.revision)));
    } catch (caught) {
      // A goal advances on every turn Core dispatches, so the revision an
      // operator is looking at goes stale while the model works. Read the
      // goal again: if the transition still applies, their decision stands.
      if (caught instanceof ApiError && caught.status === 409) {
        try {
          const current = await api.getChatGoal(sessionId);
          onChange(current);
          if (ALLOWED_FROM[action].includes(current.status)) {
            settle(await api.writeChatGoal(sessionId, body(current.revision)));
            return;
          }
          setError(`This goal is already ${STATE_NAMES[current.status]}, so it was not changed.`);
          return;
        } catch (retried) {
          void logCaughtDiagnostic("interface.goal.retry_failed", "A conversation goal state change failed after its revision was refreshed.", retried, "goal");
          setError(retried instanceof Error ? retried.message : "Goal state could not be changed.");
          return;
        }
      }
      void logCaughtDiagnostic("interface.goal.transition_failed", "A conversation goal state change failed.", caught, "goal");
      setError(caught instanceof Error ? caught.message : "Goal state could not be changed.");
    } finally { setBusy(false); }
  };

  // Cancelling is a terminal transition, so it asks first; the button sits
  // beside Edit skills and used to read like a form cancel.
  const cancelGoal = async () => {
    if (!goal || busy) return;
    const approved = await confirm({
      title: "Cancel this goal?",
      message: "A cancelled goal is final. It cannot be resumed, and the conversation continues without a goal.",
      confirmLabel: "Cancel goal",
      cancelLabel: "Keep goal",
      tone: "danger",
    });
    if (approved) await act("cancel");
  };

  const create = async () => {
    const completionCriteria = criteria.split("\n").map(item => item.trim()).filter(Boolean);
    if (!objective.trim() || !completionCriteria.length || invalidTimeBudget || busy) return;
    setBusy(true); setError(undefined);
    try {
      const draft: ProviderGoalDraft = {
        objective: objective.trim(),
        completionCriteria,
        plan: plan.split("\n").map(item => item.trim()).filter(Boolean),
        ...(resumeInstruction.trim() !== DEFAULT_GOAL_RESUME_INSTRUCTION
          ? { resumeInstruction: resumeInstruction.trim() } : {}),
        ...(tokenBudget ? { tokenBudget: Number(tokenBudget) } : {}),
        ...(timeBudget ? { timeBudgetSeconds: Number(timeBudget) * 60 } : {}),
        ...(stepBudget ? { stepBudget: Number(stepBudget) } : {}),
        ...(childBudget ? { childBudget: Number(childBudget) } : {}),
        ...(requiresReview ? { completionRequiresReview: true } : {}),
      };
      let created: ChatGoal;
      if (sessionId) created = await api.createChatGoal(sessionId, draft);
      else if (onCreate) created = await onCreate(draft);
      else throw new Error("Save the conversation before adding a goal.");
      onChange(created); setExpanded(false); setResumeInstruction(DEFAULT_GOAL_RESUME_INSTRUCTION); setRequiresReview(false);
    } catch (caught) {
      void logCaughtDiagnostic("interface.goal.create_failed", "A conversation goal could not be created.", caught, "goal");
      setError(caught instanceof Error ? caught.message : "Goal could not be created.");
    } finally { setBusy(false); }
  };

  const beginGoalEdit = () => {
    if (!goal) return;
    setObjective(goal.objective);
    setCriteria(goal.completionCriteria.join("\n"));
    setPlan(goal.plan.join("\n"));
    setResumeInstruction(goal.resumeInstruction ?? DEFAULT_GOAL_RESUME_INSTRUCTION);
    setTokenBudget(goal.tokenBudget?.toString() ?? "");
    setTimeBudget(goal.timeBudgetSeconds ? String(goal.timeBudgetSeconds / 60) : "");
    setStepBudget(goal.stepBudget?.toString() ?? "");
    setChildBudget(goal.childBudget?.toString() ?? "");
    setRequiresReview(goal.completionRequiresReview === true);
    setEditingSkills(false);
    setTransition(undefined);
    setEditingGoal(true);
    setError(undefined);
  };

  const saveGoal = async () => {
    if (!goal || !sessionId || busy) return;
    const completionCriteria = criteria.split("\n").map(item => item.trim()).filter(Boolean);
    if (!objective.trim() || !completionCriteria.length || invalidTimeBudget) return;
    if (invalidResumeInstruction) return;
    const body = (revision: number) => ({
      expectedRevision: revision,
      objective: objective.trim(),
      completionCriteria,
      plan: plan.split("\n").map(item => item.trim()).filter(Boolean),
      ...(resumeInstruction.trim() !== (goal.resumeInstruction ?? DEFAULT_GOAL_RESUME_INSTRUCTION)
        ? { resumeInstruction: resumeInstruction.trim() } : {}),
      ...(tokenBudget ? { tokenBudget: Number(tokenBudget) } : {}),
      ...(timeBudget ? { timeBudgetSeconds: Number(timeBudget) * 60 } : {}),
      ...(stepBudget ? { stepBudget: Number(stepBudget) } : {}),
      ...(childBudget ? { childBudget: Number(childBudget) } : {}),
      ...(requiresReview !== (goal.completionRequiresReview === true) ? { completionRequiresReview: requiresReview } : {}),
    });
    setBusy(true); setError(undefined);
    try {
      onChange(await api.updateChatGoal(sessionId, body(goal.revision)));
      setEditingGoal(false);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        try {
          const current = await api.getChatGoal(sessionId);
          const contentUnchanged = current.objective === goal.objective
            && JSON.stringify(current.completionCriteria) === JSON.stringify(goal.completionCriteria)
            && JSON.stringify(current.plan) === JSON.stringify(goal.plan)
            && (current.resumeInstruction ?? DEFAULT_GOAL_RESUME_INSTRUCTION) === (goal.resumeInstruction ?? DEFAULT_GOAL_RESUME_INSTRUCTION)
            && current.tokenBudget === goal.tokenBudget
            && current.timeBudgetSeconds === goal.timeBudgetSeconds
            && current.stepBudget === goal.stepBudget
            && current.childBudget === goal.childBudget
            && current.completionRequiresReview === goal.completionRequiresReview;
          onChange(current);
          if (contentUnchanged) {
            onChange(await api.updateChatGoal(sessionId, body(current.revision)));
            setEditingGoal(false);
            return;
          }
          setError("The goal changed while you were editing. Your draft is still here; review the latest goal and save again.");
          return;
        } catch (refreshed) {
          void logCaughtDiagnostic("interface.goal.edit_refresh_failed", "A changed conversation goal could not be refreshed.", refreshed, "goal");
        }
      }
      void logCaughtDiagnostic("interface.goal.edit_failed", "A conversation goal edit could not be saved.", caught, "goal");
      setError(caught instanceof Error ? caught.message : "Goal changes could not be saved.");
    } finally { setBusy(false); }
  };

  const addNote = async () => {
    const text = noteText.trim();
    if (!goal || !sessionId || busy || !text || text.length > GOAL_NOTE_CHARACTERS) return;
    setBusy(true); setError(undefined); setNoteStatus(undefined);
    try {
      const result = await api.addChatGoalNote(sessionId, text);
      onChange(result.goal);
      setNoteText(""); setAddingNote(false);
      setNoteStatus(NOTE_DELIVERY_STATUS[result.delivery]);
    } catch (caught) {
      void logCaughtDiagnostic("interface.goal.note_failed", "A supervisor note could not be added to a conversation goal.", caught, "goal");
      setError(caught instanceof Error ? caught.message : "The note could not be saved. Your text is still here; try again.");
    } finally { setBusy(false); }
  };

  const removeNote = async (noteId: string) => {
    if (!goal || !sessionId || busy) return;
    setBusy(true); setError(undefined); setNoteStatus(undefined);
    try {
      const result = await api.removeChatGoalNote(sessionId, noteId);
      onChange(result.goal);
    } catch (caught) {
      void logCaughtDiagnostic("interface.goal.note_remove_failed", "A supervisor note could not be removed from a conversation goal.", caught, "goal");
      setError(caught instanceof Error ? caught.message : "The note could not be removed.");
    } finally { setBusy(false); }
  };

  const beginSkillEdit = () => {
    setSelectedSkillPaths(goal?.skillSnapshots.map(skill => skill.path) ?? []);
    setEditingSkills(true);
    setError(undefined);
  };

  const saveSkills = async () => {
    if (!goal || !sessionId || busy) return;
    const available = new Map([
      ...(skills ?? []).map(skill => [skill.path, skill] as const),
      ...goal.skillSnapshots.map(skill => [skill.path, skill] as const),
    ]);
    setBusy(true); setError(undefined);
    const selection = selectedSkillPaths.flatMap(path => {
      const skill = available.get(path);
      return skill ? [{ name: skill.name, path: skill.path }] : [];
    });
    try {
      onChange(await api.replaceChatGoalSkills(sessionId, { expectedRevision: goal.revision, skills: selection }));
      setEditingSkills(false);
    } catch (caught) {
      // Same stale-revision recovery as a transition: the model working does
      // not withdraw the operator's choice of skills.
      if (caught instanceof ApiError && caught.status === 409) {
        try {
          const current = await api.getChatGoal(sessionId);
          onChange(await api.replaceChatGoalSkills(sessionId, { expectedRevision: current.revision, skills: selection }));
          setEditingSkills(false);
          return;
        } catch (retried) {
          void logCaughtDiagnostic("interface.goal.skills_retry_failed", "Conversation goal skills could not be changed after a revision refresh.", retried, "goal");
          setError(retried instanceof Error ? retried.message : "Goal skills could not be changed.");
          return;
        }
      }
      void logCaughtDiagnostic("interface.goal.skills_failed", "Conversation goal skills could not be changed.", caught, "goal");
      setError(caught instanceof Error ? caught.message : "Goal skills could not be changed.");
    } finally { setBusy(false); }
  };

  if (!goal) return <section className="chat-goal-panel empty" aria-label="Conversation goal" data-guide="goal-panel">
    <button className="button quiet chat-goal-chip" type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}><Flag size={13} aria-hidden="true" /> Add goal</button>
    {expanded && <div className="chat-goal-form">
      <label>Objective<textarea value={objective} maxLength={20_000} required onChange={event => setObjective(event.target.value)} /></label>
      <label>Completion criteria<textarea value={criteria} required placeholder="One criterion per line" onChange={event => setCriteria(event.target.value)} /></label>
      <label>Plan<textarea value={plan} placeholder="One optional step per line" onChange={event => setPlan(event.target.value)} /></label>
      <label>Resume instruction<textarea value={resumeInstruction} maxLength={4_000} required onChange={event => setResumeInstruction(event.target.value)} /></label>
      <small>Sent when you resume this goal.</small>
      <div><button className="button quiet chat-goal-resume-default" type="button" disabled={resumeInstruction === DEFAULT_GOAL_RESUME_INSTRUCTION} onClick={() => setResumeInstruction(DEFAULT_GOAL_RESUME_INSTRUCTION)}>Use default</button></div>
      <details><summary>Limits</summary><div className="chat-goal-limits">
        {tokenBudgetAvailable && <label>Token budget<input type="number" min="1" value={tokenBudget} onChange={event => setTokenBudget(event.target.value)} /></label>}
        {!tokenBudgetAvailable && <small>Token limits need usage reported by the harness. Step and time limits remain available.</small>}
        <label>Active time limit (minutes, 30 minimum)<input type="number" min="30" step="1" value={timeBudget} onChange={event => setTimeBudget(event.target.value)} /></label>
        <label>Step budget<input type="number" min="1" value={stepBudget} onChange={event => setStepBudget(event.target.value)} /></label>
        <label>Child budget<input type="number" min="0" value={childBudget} onChange={event => setChildBudget(event.target.value)} /></label>
      </div></details>
      <ReviewChoice checked={requiresReview} onChange={setRequiresReview} />
      <div><button className="button secondary" type="button" onClick={() => setExpanded(false)}>Cancel</button><button className="button primary" type="button" disabled={busy || invalidTimeBudget || invalidResumeInstruction || !objective.trim() || !criteria.trim()} onClick={() => void create()}>{busy ? <LoaderCircle className="spin" size={14} /> : <Flag size={14} />} Save draft</button></div>
    </div>}
    {error && <p role="alert">{error}</p>}
  </section>;

  const terminal = goal.status === "completed" || goal.status === "cancelled";
  const proposal = goal.status === "blocked" ? goal.completionProposal : undefined;
  const skillOptions = [...new Map([
    ...(skills ?? []).map(skill => [skill.path, skill] as const),
    ...goal.skillSnapshots.map(skill => [skill.path, skill] as const),
  ]).values()];
  const liveTokens = Math.max(0, Math.floor(liveTokenEstimate ?? 0));
  const displayedTokens = goal.usage.totalTokens + liveTokens;
  const tokenLabel = goal.usageUnknown ? `at least ${displayedTokens.toLocaleString()} tokens · usage incomplete` : liveTokens > 0 ? `~${displayedTokens.toLocaleString()} tokens` : `${displayedTokens.toLocaleString()} tokens`;
  // A stored "running" goal makes no progress while no worker serves its project.
  const waiting = goal.status === "running" ? goal.executionWaiting : undefined;
  const handoffSessionId = goal.handoffTargetSessionId ?? goal.handoffSourceSessionId;
  const handoffUrl = handoffSessionId
    ? `/projects/${encodeURIComponent(goal.engagementId)}/workbench?view=chat&session=${encodeURIComponent(handoffSessionId)}`
    : undefined;
  return <section className={`chat-goal-panel${detailsOpen ? "" : " is-collapsed"}`} aria-label="Conversation goal" data-guide="goal-panel">
    <header><button className="chat-goal-toggle" type="button" aria-expanded={detailsOpen} aria-controls="chat-goal-details" aria-label={`${detailsOpen ? "Collapse" : "Expand"} goal controls`} title={`${detailsOpen ? "Collapse" : "Expand"} goal controls`} onClick={() => setDetailsOpen(value => !value)}><ChevronDown size={17} aria-hidden="true" /></button><span className="chat-goal-heading"><Flag size={15} aria-hidden="true" /><strong title={goal.objective}>{goal.objective}</strong></span><small><span className={`chat-goal-status ${waiting ? "waiting" : goal.status}`} title={waiting?.detail}>{waiting ? (waiting.reason === "held" ? "held" : "waiting") : goal.heldByProject && goal.status === "paused" ? "paused by update" : proposal ? "awaiting review" : goal.status.replaceAll("_", " ")}</span><span className="chat-goal-step">step {goal.currentStep}{goal.stepBudget ? `/${goal.stepBudget}` : ""}</span><span title={liveTokens > 0 ? "Estimated while this turn streams; Core replaces it with reported usage when the turn settles." : undefined}>{tokenLabel}</span><span>{Math.floor(activeSeconds(goal, tick))}s active</span>{goal.timeBudgetSeconds !== undefined && <span>{Math.max(0, Math.ceil((goal.timeBudgetSeconds - activeSeconds(goal, tick)) / 60))}m left</span>}{goal.childBudget !== undefined && <span>{goal.childrenStarted}/{goal.childBudget} children</span>}<span>{goal.skillSnapshots.length} skills</span></small></header>
    {waiting && <p role="status" className="chat-goal-waiting">{waiting.detail}</p>}
    {goal.heldByProject && goal.status === "paused"
      ? <p role="status" className="chat-goal-held">Goal paused by update · resumes automatically</p>
      : proposal
        ? <div className="chat-goal-review" role="region" aria-label="Completion awaiting review">
          {/* The decision sits beside its status so it stays in view; the proposal follows. */}
          <div className="chat-goal-review-head">
            <p role="status"><strong>Awaiting your review</strong> · the agent reports this goal complete.</p>
            {transition !== "send_back" && <div className="chat-goal-review-actions">
              <button className="button secondary" type="button" disabled={busy || settingsBusy} onClick={() => { setReason(""); setTransition("send_back"); }}><RotateCcw size={14} aria-hidden="true" /> Send back</button>
              <button className="button primary" type="button" disabled={busy || settingsBusy} onClick={() => void act("complete")}>{busy ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : <Check size={14} aria-hidden="true" />} Approve completion</button>
            </div>}
          </div>
          <p className="chat-goal-review-summary">{proposal.summary}</p>
          {proposal.evidence.length > 0 && <ul>{proposal.evidence.map((item, index) => <li key={index}>{String(item.detail ?? "Recorded evidence")}</li>)}</ul>}
          <small>Agent-reported evidence, not verified by Core.</small>
          {transition === "send_back" && <div className="chat-goal-form">
            <label>What still needs work<textarea value={reason} maxLength={2_000} placeholder="Optional feedback for the agent" onChange={event => setReason(event.target.value)} /></label>
            <div><button className="button quiet" type="button" disabled={busy} onClick={() => setTransition(undefined)}>Back</button><button className="button primary" type="button" disabled={busy || settingsBusy} onClick={() => void act("resume")}>{busy ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : <RotateCcw size={14} aria-hidden="true" />} Send back</button></div>
          </div>}
        </div>
        : goal.blockedReason && <p role="status">{goal.blockedReason}</p>}
    {handoffUrl && <p role="status" style={{minWidth: 0, overflowWrap: "anywhere"}}>{goal.handoffTargetSessionId ? "Work continues in a linked provider chat." : "Continued from a paused harness goal."} <a href={handoffUrl}>{goal.handoffTargetSessionId ? "Open continuation" : "Open source chat"}</a>{goal.handoffReason ? ` Reason: ${goal.handoffReason}` : ""}</p>}
    <div id="chat-goal-details" hidden={!detailsOpen} className="chat-goal-details">
    <details className="chat-goal-objective" open={objectiveOpen} onToggle={event => setObjectiveOpen(event.currentTarget.open)}>
      <summary>Objective and completion criteria</summary>
      <div><p>{goal.objective}</p><strong>Completion criteria</strong>
        <ul>{goal.completionCriteria.map((item, index) => <li key={index}>{item}</li>)}</ul>
        {goal.plan.length > 0 && <><strong>Plan</strong><ol>{goal.plan.map((item, index) => <li key={index}>{item}</li>)}</ol></>}
      </div>
    </details>
    {(goal.supervisorNotes?.length ?? 0) > 0 && <details className="chat-goal-notes">
      <summary>Supervisor notes · {goal.supervisorNotes?.length}</summary>
      <ol>{goal.supervisorNotes?.map(note => <li key={note.id}>
        <p>{note.text}</p>
        <small>{note.author === "butler" ? "Butler" : "You"}{note.createdAt ? ` · ${new Date(note.createdAt).toLocaleString()}` : ""}</small>
        {!terminal && <button className="icon-button chat-goal-note-remove" type="button" aria-label="Remove note" title="Remove note" disabled={busy} onClick={() => void removeNote(note.id)}><X size={14} aria-hidden="true" /></button>}
      </li>)}</ol>
    </details>}
    {noteStatus && <p role="status" className="chat-goal-note-status">{noteStatus}</p>}
    {!terminal && <div className="chat-goal-actions">
      {goal.status === "draft" && <button className="button primary" type="button" disabled={busy || settingsBusy} onClick={() => void act("start")}><CirclePlay size={14} /> Start</button>}
      {goal.status === "running" && <><button className="button secondary chat-goal-pause" type="button" disabled={busy || settingsBusy} onClick={() => void act("pause")}><CirclePause size={14} /> Pause</button><div className="chat-goal-outcomes" role="group" aria-label="Set goal outcome"><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={() => setTransition("block")}>Block</button><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={() => setTransition("complete")}><Check size={14} aria-hidden="true" /> Complete</button></div></>}
      {(goal.status === "paused" || goal.status === "blocked") && !proposal && !goal.handoffTargetSessionId && !goal.heldByProject && <button className="button primary" type="button" disabled={busy || settingsBusy} onClick={() => void act("resume")}><CirclePlay size={14} /> Resume</button>}
      <div className="chat-goal-utilities"><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={beginGoalEdit}><Pencil size={14} aria-hidden="true" /> Edit goal</button><button className="button quiet chat-goal-add-note" type="button" aria-expanded={addingNote} disabled={busy || settingsBusy} onClick={() => { setAddingNote(true); setEditingGoal(false); setTransition(undefined); setNoteStatus(undefined); setError(undefined); }}><MessageSquarePlus size={14} aria-hidden="true" /> Add note</button><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={beginSkillEdit}>Edit skills</button><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={() => void cancelGoal()}><OctagonX size={14} /> Cancel goal</button></div>
    </div>}
    {editingGoal && !terminal && <div className="chat-goal-form" data-selection-actions-disabled ref={editFormRef}>
      <label>Objective<textarea value={objective} maxLength={20_000} required onChange={event => setObjective(event.target.value)} /></label>
      <label>Completion criteria<textarea value={criteria} required placeholder="One criterion per line" onChange={event => setCriteria(event.target.value)} /></label>
      <label>Plan<textarea value={plan} placeholder="One optional step per line" onChange={event => setPlan(event.target.value)} /></label>
      <label>Resume instruction<textarea value={resumeInstruction} maxLength={4_000} required onChange={event => setResumeInstruction(event.target.value)} /></label>
      <small>Sent when you resume this goal.</small>
      <div><button className="button quiet chat-goal-resume-default" type="button" disabled={resumeInstruction === DEFAULT_GOAL_RESUME_INSTRUCTION} onClick={() => setResumeInstruction(DEFAULT_GOAL_RESUME_INSTRUCTION)}>Use default</button></div>
      <details><summary>Limits</summary><div className="chat-goal-limits">
        {(tokenBudgetAvailable || goal.tokenBudget !== undefined) && <label>Token budget<input type="number" min="1" value={tokenBudget} onChange={event => setTokenBudget(event.target.value)} /></label>}
        {!tokenBudgetAvailable && <small>Token tracking is unavailable for this harness. Remove an existing token limit to resume.</small>}
        <label>Active time limit (minutes, 30 minimum)<input type="number" min="30" step="1" value={timeBudget} onChange={event => setTimeBudget(event.target.value)} /></label>
        <label>Step budget<input type="number" min="1" value={stepBudget} onChange={event => setStepBudget(event.target.value)} /></label>
        <label>Child budget<input type="number" min="0" value={childBudget} onChange={event => setChildBudget(event.target.value)} /></label>
      </div></details>
      <ReviewChoice checked={requiresReview} onChange={setRequiresReview} />
      <div><button className="button quiet" type="button" disabled={busy} onClick={() => setEditingGoal(false)}>Back</button><button className="button primary" type="button" disabled={busy || invalidTimeBudget || invalidResumeInstruction || !objective.trim() || !criteria.trim()} onClick={() => void saveGoal()}>{busy ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : null} Save changes</button></div>
    </div>}
    {addingNote && !terminal && <div className="chat-goal-form chat-goal-note-form" data-selection-actions-disabled>
      <label>Supervisor note<textarea value={noteText} maxLength={GOAL_NOTE_CHARACTERS} autoFocus placeholder="Guidance for the agent; it reaches a running turn" onChange={event => setNoteText(event.target.value)} /></label>
      <small>{noteText.trim().length.toLocaleString()} / {GOAL_NOTE_CHARACTERS.toLocaleString()}</small>
      <div><button className="button quiet" type="button" disabled={busy} onClick={() => setAddingNote(false)}>Back</button><button className="button primary" type="button" disabled={busy || !noteText.trim()} onClick={() => void addNote()}>{busy ? <LoaderCircle className="spin" size={14} aria-hidden="true" /> : <MessageSquarePlus size={14} aria-hidden="true" />} Add note</button></div>
    </div>}
    {editingSkills && !terminal && <div className="chat-goal-form">
      <fieldset><legend>Goal skills</legend>{skillOptions.length ? skillOptions.map(skill => <label key={skill.path}><input type="checkbox" checked={selectedSkillPaths.includes(skill.path)} onChange={event => setSelectedSkillPaths(current => event.target.checked ? [...current, skill.path] : current.filter(path => path !== skill.path))} /> <span><strong>{skill.name}</strong> <small>{skill.source} · {skill.path}{goal.skillSnapshots.some(item => item.path === skill.path) && !(skills ?? []).some(item => item.path === skill.path) ? " · retained snapshot; source unavailable" : ""}</small></span></label>) : <p>No skills are currently available.</p>}</fieldset>
      <div><button className="button quiet" type="button" onClick={() => setEditingSkills(false)}>Back</button><button className="button primary" type="button" disabled={busy} onClick={() => void saveSkills()}>{busy ? <LoaderCircle className="spin" size={14} /> : null} Save skills</button></div>
    </div>}
    {transition === "block" && <div className="chat-goal-form"><label>Blocked reason<textarea required value={reason} onChange={event => setReason(event.target.value)} /></label><div><button className="button quiet" type="button" onClick={() => setTransition(undefined)}>Back</button><button className="button primary" type="button" disabled={busy || !reason.trim()} onClick={() => void act("block")}>Confirm blocked</button></div></div>}
    {transition === "complete" && <div className="chat-goal-form"><label>Completion summary<textarea required value={summary} onChange={event => setSummary(event.target.value)} /></label><label>Completion evidence<textarea required placeholder="One verified item per line" value={evidence} onChange={event => setEvidence(event.target.value)} /></label><div><button className="button quiet" type="button" onClick={() => setTransition(undefined)}>Back</button><button className="button primary" type="button" disabled={busy || !summary.trim() || !evidence.trim()} onClick={() => void act("complete")}>Confirm complete</button></div></div>}
    {goal.status === "completed" && goal.completionSummary && <details><summary>Completion evidence</summary><p>{goal.completionSummary}</p><ul>{goal.completionEvidence.map((item, index) => <li key={index}>{String(item.detail ?? item.result ?? "Recorded evidence")}</li>)}</ul></details>}
    </div>
    {error && <p role="alert">{error}</p>}
  </section>;
}
