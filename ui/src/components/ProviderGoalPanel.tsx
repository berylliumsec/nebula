import { useState } from "react";
import { CirclePause, CirclePlay, Flag, LoaderCircle, OctagonX } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { ChatGoal, HarnessSkillSummary } from "../api/types";

export function ProviderGoalPanel({ api, sessionId, goal, skills, onChange }: {
  api: ApiClient;
  sessionId: string;
  goal?: ChatGoal;
  skills?: HarnessSkillSummary[];
  onChange(goal: ChatGoal): void;
}) {
  const [expanded, setExpanded] = useState(false);
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

  const act = async (action: "start" | "pause" | "resume" | "cancel" | "block" | "complete") => {
    if (!goal || busy) return;
    setBusy(true); setError(undefined);
    try {
      onChange(await api.writeChatGoal(sessionId, {
        expectedRevision: goal.revision,
        action,
        ...(action === "block" ? { reason: reason.trim() } : {}),
        ...(action === "complete" ? {
          completionSummary: summary.trim(),
          completionEvidence: evidence.split("\n").map(item => item.trim()).filter(Boolean).map(detail => ({ kind: "operator_confirmation", detail })),
        } : {}),
      }));
      setTransition(undefined); setReason(""); setSummary(""); setEvidence("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Goal state could not be changed.");
    } finally { setBusy(false); }
  };

  const create = async () => {
    const completionCriteria = criteria.split("\n").map(item => item.trim()).filter(Boolean);
    if (!objective.trim() || !completionCriteria.length || busy) return;
    setBusy(true); setError(undefined);
    try {
      const created = await api.createChatGoal(sessionId, {
        objective: objective.trim(),
        completionCriteria,
        plan: plan.split("\n").map(item => item.trim()).filter(Boolean),
        ...(tokenBudget ? { tokenBudget: Number(tokenBudget) } : {}),
        ...(timeBudget ? { timeBudgetSeconds: Number(timeBudget) * 60 } : {}),
        ...(stepBudget ? { stepBudget: Number(stepBudget) } : {}),
        ...(childBudget ? { childBudget: Number(childBudget) } : {}),
      });
      onChange(created); setExpanded(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Goal could not be created.");
    } finally { setBusy(false); }
  };

  const beginSkillEdit = () => {
    setSelectedSkillPaths(goal?.skillSnapshots.map(skill => skill.path) ?? []);
    setEditingSkills(true);
    setError(undefined);
  };

  const saveSkills = async () => {
    if (!goal || busy) return;
    const available = new Map([
      ...(skills ?? []).map(skill => [skill.path, skill] as const),
      ...goal.skillSnapshots.map(skill => [skill.path, skill] as const),
    ]);
    setBusy(true); setError(undefined);
    try {
      onChange(await api.replaceChatGoalSkills(sessionId, {
        expectedRevision: goal.revision,
        skills: selectedSkillPaths.flatMap(path => {
          const skill = available.get(path);
          return skill ? [{ name: skill.name, path: skill.path }] : [];
        }),
      }));
      setEditingSkills(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Goal skills could not be changed.");
    } finally { setBusy(false); }
  };

  if (!goal) return <section className="chat-goal-panel" aria-label="Conversation goal">
    <button className="button quiet" type="button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}><Flag size={14} /> Add goal</button>
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
  return <section className="chat-goal-panel" aria-label="Conversation goal">
    <header><span><Flag size={14} /><strong>{goal.objective}</strong></span><small>{goal.status.replaceAll("_", " ")} · step {goal.currentStep}{goal.stepBudget ? `/${goal.stepBudget}` : ""} · {goal.usage.totalTokens.toLocaleString()} tokens · {Math.floor(goal.elapsedSeconds)}s active{goal.childBudget !== undefined ? ` · ${goal.childrenStarted}/${goal.childBudget} children` : ""} · {goal.skillSnapshots.length} skills</small></header>
    {goal.blockedReason && <p role="status">{goal.blockedReason}</p>}
    {!terminal && <div className="chat-goal-actions">
      {goal.status === "draft" && <button className="button primary" type="button" disabled={busy} onClick={() => void act("start")}><CirclePlay size={14} /> Start</button>}
      {goal.status === "running" && <button className="button secondary" type="button" disabled={busy} onClick={() => void act("pause")}><CirclePause size={14} /> Pause</button>}
      {goal.status === "running" && <button className="button quiet" type="button" disabled={busy} onClick={() => setTransition("block")}>Block</button>}
      {goal.status === "running" && <button className="button primary" type="button" disabled={busy} onClick={() => setTransition("complete")}>Complete</button>}
      {(goal.status === "paused" || goal.status === "blocked") && <button className="button primary" type="button" disabled={busy} onClick={() => void act("resume")}><CirclePlay size={14} /> Resume</button>}
      <button className="button quiet" type="button" disabled={busy} onClick={beginSkillEdit}>Edit skills</button>
      <button className="button quiet" type="button" disabled={busy} onClick={() => void act("cancel")}><OctagonX size={14} /> Cancel</button>
    </div>}
    {editingSkills && !terminal && <div className="chat-goal-form">
      <fieldset><legend>Goal skills</legend>{skillOptions.length ? skillOptions.map(skill => <label key={skill.path}><input type="checkbox" checked={selectedSkillPaths.includes(skill.path)} onChange={event => setSelectedSkillPaths(current => event.target.checked ? [...current, skill.path] : current.filter(path => path !== skill.path))} /> <span><strong>{skill.name}</strong> <small>{skill.source} · {skill.path}{goal.skillSnapshots.some(item => item.path === skill.path) && !(skills ?? []).some(item => item.path === skill.path) ? " · retained snapshot; source unavailable" : ""}</small></span></label>) : <p>No skills are currently available.</p>}</fieldset>
      <div><button className="button quiet" type="button" onClick={() => setEditingSkills(false)}>Back</button><button className="button primary" type="button" disabled={busy} onClick={() => void saveSkills()}>{busy ? <LoaderCircle className="spin" size={14} /> : null} Save skills</button></div>
    </div>}
    {transition === "block" && <div className="chat-goal-form"><label>Blocked reason<textarea required value={reason} onChange={event => setReason(event.target.value)} /></label><div><button className="button quiet" type="button" onClick={() => setTransition(undefined)}>Back</button><button className="button primary" type="button" disabled={busy || !reason.trim()} onClick={() => void act("block")}>Confirm blocked</button></div></div>}
    {transition === "complete" && <div className="chat-goal-form"><label>Completion summary<textarea required value={summary} onChange={event => setSummary(event.target.value)} /></label><label>Completion evidence<textarea required placeholder="One verified item per line" value={evidence} onChange={event => setEvidence(event.target.value)} /></label><div><button className="button quiet" type="button" onClick={() => setTransition(undefined)}>Back</button><button className="button primary" type="button" disabled={busy || !summary.trim() || !evidence.trim()} onClick={() => void act("complete")}>Confirm complete</button></div></div>}
    {goal.status === "completed" && goal.completionSummary && <details><summary>Completion evidence</summary><p>{goal.completionSummary}</p><ul>{goal.completionEvidence.map((item, index) => <li key={index}>{String(item.detail ?? item.result ?? "Recorded evidence")}</li>)}</ul></details>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
