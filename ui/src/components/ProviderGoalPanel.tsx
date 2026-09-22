import { useEffect, useState } from "react";
import { Check, ChevronDown, CirclePause, CirclePlay, Flag, LoaderCircle, OctagonX } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { ChatGoal, HarnessSkillSummary } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { useConfirmation } from "./DialogSystem";

type GoalAction = "start" | "pause" | "resume" | "cancel" | "block" | "complete";

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
  if (!text) return 0;
  return Math.max(1, Math.ceil(new TextEncoder().encode(text).length / 4));
}

/** Which goal states each transition is allowed from, as Core enforces them. */
const ALLOWED_FROM: Record<GoalAction, ChatGoal["status"][]> = {
  start: ["draft"],
  pause: ["running"],
  resume: ["paused", "blocked"],
  cancel: ["draft", "running", "paused", "blocked"],
  block: ["running"],
  complete: ["running"],
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
  tokenBudget?: number;
  timeBudgetSeconds?: number;
  stepBudget?: number;
  childBudget?: number;
}

export function ProviderGoalPanel({ api, sessionId, goal, skills, liveTokenEstimate, settingsBusy = false, onCreate, onChange, onWorkDispatched }: {
  api: ApiClient;
  sessionId?: string;
  goal?: ChatGoal;
  skills?: HarnessSkillSummary[];
  liveTokenEstimate?: number;
  settingsBusy?: boolean;
  onCreate?(draft: ProviderGoalDraft): Promise<ChatGoal>;
  onChange(goal: ChatGoal): void;
  onWorkDispatched?(): Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(true);
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [plan, setPlan] = useState("");
  const [tokenBudget, setTokenBudget] = useState("");
  const [timeBudget, setTimeBudget] = useState("");
  const [stepBudget, setStepBudget] = useState("");
  const [childBudget, setChildBudget] = useState("");
  const [transition, setTransition] = useState<"block" | "complete">();
  const [reason, setReason] = useState("");
  const [summary, setSummary] = useState("");
  const [evidence, setEvidence] = useState("");
  const [editingSkills, setEditingSkills] = useState(false);
  const [selectedSkillPaths, setSelectedSkillPaths] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const confirm = useConfirmation();
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
    const body = (revision: number) => ({
      expectedRevision: revision,
      action,
      ...(action === "block" ? { reason: reason.trim() } : {}),
      ...(action === "complete" ? {
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
    if (!objective.trim() || !completionCriteria.length || busy) return;
    setBusy(true); setError(undefined);
    try {
      const draft: ProviderGoalDraft = {
        objective: objective.trim(),
        completionCriteria,
        plan: plan.split("\n").map(item => item.trim()).filter(Boolean),
        ...(tokenBudget ? { tokenBudget: Number(tokenBudget) } : {}),
        ...(timeBudget ? { timeBudgetSeconds: Number(timeBudget) * 60 } : {}),
        ...(stepBudget ? { stepBudget: Number(stepBudget) } : {}),
        ...(childBudget ? { childBudget: Number(childBudget) } : {}),
      };
      let created: ChatGoal;
      if (sessionId) created = await api.createChatGoal(sessionId, draft);
      else if (onCreate) created = await onCreate(draft);
      else throw new Error("Save the conversation before adding a goal.");
      onChange(created); setExpanded(false);
    } catch (caught) {
      void logCaughtDiagnostic("interface.goal.create_failed", "A conversation goal could not be created.", caught, "goal");
      setError(caught instanceof Error ? caught.message : "Goal could not be created.");
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
      <details><summary>Limits</summary><div className="chat-goal-limits">
        <label>Token budget<input type="number" min="1" value={tokenBudget} onChange={event => setTokenBudget(event.target.value)} /></label>
        <label>Time budget (minutes)<input type="number" min="1" value={timeBudget} onChange={event => setTimeBudget(event.target.value)} /></label>
        <label>Step budget<input type="number" min="1" value={stepBudget} onChange={event => setStepBudget(event.target.value)} /></label>
        <label>Child budget<input type="number" min="0" value={childBudget} onChange={event => setChildBudget(event.target.value)} /></label>
      </div></details>
      <div><button className="button secondary" type="button" onClick={() => setExpanded(false)}>Cancel</button><button className="button primary" type="button" disabled={busy || !objective.trim() || !criteria.trim()} onClick={() => void create()}>{busy ? <LoaderCircle className="spin" size={14} /> : <Flag size={14} />} Save draft</button></div>
    </div>}
    {error && <p role="alert">{error}</p>}
  </section>;

  const terminal = goal.status === "completed" || goal.status === "cancelled";
  const skillOptions = [...new Map([
    ...(skills ?? []).map(skill => [skill.path, skill] as const),
    ...goal.skillSnapshots.map(skill => [skill.path, skill] as const),
  ]).values()];
  const liveTokens = Math.max(0, Math.floor(liveTokenEstimate ?? 0));
  const displayedTokens = goal.usage.totalTokens + liveTokens;
  const tokenLabel = liveTokens > 0 ? `~${displayedTokens.toLocaleString()} tokens` : `${displayedTokens.toLocaleString()} tokens`;
  return <section className={`chat-goal-panel${detailsOpen ? "" : " is-collapsed"}`} aria-label="Conversation goal" data-guide="goal-panel">
    <header><button className="chat-goal-toggle" type="button" aria-expanded={detailsOpen} aria-controls="chat-goal-details" aria-label={`${detailsOpen ? "Collapse" : "Expand"} goal controls`} title={`${detailsOpen ? "Collapse" : "Expand"} goal controls`} onClick={() => setDetailsOpen(value => !value)}><ChevronDown size={17} aria-hidden="true" /></button><span className="chat-goal-heading"><Flag size={15} aria-hidden="true" /><strong title={goal.objective}>{goal.objective}</strong></span><small><span className={`chat-goal-status ${goal.status}`}>{goal.status.replaceAll("_", " ")}</span><span className="chat-goal-step">step {goal.currentStep}{goal.stepBudget ? `/${goal.stepBudget}` : ""}</span><span title={liveTokens > 0 ? "Estimated while this turn streams; Core replaces it with exact provider usage when the turn settles." : undefined}>{tokenLabel}</span><span>{Math.floor(activeSeconds(goal, tick))}s active</span>{goal.childBudget !== undefined && <span>{goal.childrenStarted}/{goal.childBudget} children</span>}<span>{goal.skillSnapshots.length} skills</span></small></header>
    {goal.blockedReason && <p role="status">{goal.blockedReason}</p>}
    <div id="chat-goal-details" hidden={!detailsOpen} className="chat-goal-details">
    {!terminal && <div className="chat-goal-actions">
      {goal.status === "draft" && <button className="button primary" type="button" disabled={busy || settingsBusy} onClick={() => void act("start")}><CirclePlay size={14} /> Start</button>}
      {goal.status === "running" && <><button className="button secondary chat-goal-pause" type="button" disabled={busy || settingsBusy} onClick={() => void act("pause")}><CirclePause size={14} /> Pause</button><div className="chat-goal-outcomes" role="group" aria-label="Set goal outcome"><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={() => setTransition("block")}>Block</button><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={() => setTransition("complete")}><Check size={14} aria-hidden="true" /> Complete</button></div></>}
      {(goal.status === "paused" || goal.status === "blocked") && <button className="button primary" type="button" disabled={busy || settingsBusy} onClick={() => void act("resume")}><CirclePlay size={14} /> Resume</button>}
      <div className="chat-goal-utilities"><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={beginSkillEdit}>Edit skills</button><button className="button quiet" type="button" disabled={busy || settingsBusy} onClick={() => void cancelGoal()}><OctagonX size={14} /> Cancel goal</button></div>
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
