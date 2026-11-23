import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ButlerStage } from "../butlerStage";
import { ButlerStageLabel } from "../components/ButlerStageLabel";
import { WorkStageLabel } from "../components/WorkStageLabel";
import type { ChatWorkStage } from "../api/types";
import { ArrowLeft, ChevronDown, ChevronRight, ExternalLink } from "lucide-react";
import type { ApiClient } from "../api/client";
import { boardElapsed } from "../api/butlerBoard";
import { getButlerBriefIfChanged, getButlerBriefTurnsBefore, groupTurnsByStep, turnChips, type ButlerBrief, type ButlerBriefTurn } from "../api/butlerBrief";
import { startVisiblePoll } from "../api/visiblePoll";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./ButlerChatBrief.module.css";

const STATE_LABEL = { waiting: "Needs you", working: "Working", failed: "Failed", idle: "Idle" } as const;
const GOAL_LIVE = new Set(["active", "running", "queued"]);

export function chatTurnPath(projectId: string, sessionId: string, turnId?: string): string {
  const params = new URLSearchParams({ view: "chat", session: sessionId });
  if (turnId) params.set("turn", turnId);
  return `/projects/${encodeURIComponent(projectId)}/workbench?${params}`;
}

function money(value: number | undefined): string | undefined {
  if (value === undefined) return undefined;
  return value < 0.01 ? "<$0.01" : `$${value.toFixed(2)}`;
}

/**
 * One chat at a glance: what it is for, what it needs right now, its plan
 * steps, and one line per turn. Core owns all of it; the brief polls while
 * visible. The full transcript stays one click away.
 */
export function ButlerChatBrief({ api, sessionId, turnId, onBack, onAsk, onOpenChat, onOpenTurn, onOpenFile, onDecide, deciding, note, onStop, proposalActions, position, butlerCase, workStage }: {
  api: ApiClient;
  sessionId: string;
  /** The running turn, from the board, for Stop. */
  turnId?: string;
  onBack: () => void;
  onAsk: (brief: ButlerBrief) => void;
  onOpenChat: (projectId: string) => void;
  onOpenTurn: (projectId: string, turnId: string) => void;
  onOpenFile: (path: string, projectId: string) => void;
  onDecide: (actionId: string, decision: "approve" | "reject") => void;
  deciding: boolean;
  note?: string;
  onStop?: () => void;
  /** Butler's open (or freshly resolved) case about this chat, and how to open its Butler check. */
  butlerCase?: { stage: ButlerStage; onOpen?: () => void };
  /** The project Work stage of the chat's assigned Work item, from the board. */
  workStage?: ChatWorkStage;
  /** Start / Edit / Cancel for a chat Butler proposed (the board knows it is a proposal). */
  proposalActions?: ReactNode;
  /** "2 of 5" among the board's cards, for ↑/↓. */
  position?: string;
}) {
  const [brief, setBrief] = useState<ButlerBrief>();
  const [error, setError] = useState("");
  const [older, setOlder] = useState<{ turns: ButlerBriefTurn[]; before?: number; loading: boolean; error?: string }>({ turns: [], loading: false });
  const [openSteps, setOpenSteps] = useState<Set<number | "recent">>(new Set());
  const [goalBusy, setGoalBusy] = useState(false);
  const [goalNote, setGoalNote] = useState("");
  const etag = useRef<string | undefined>(undefined);
  const poll = useRef<{ poke: () => void } | undefined>(undefined);
  const heading = useRef<HTMLHeadingElement>(null);
  const [clock, setClock] = useState(() => Date.now());

  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 15_000); return () => window.clearInterval(timer); }, []);

  useEffect(() => {
    setBrief(undefined);
    setError("");
    setOlder({ turns: [], loading: false });
    setOpenSteps(new Set());
    setGoalNote("");
    etag.current = undefined;
    const controller = new AbortController();
    poll.current = startVisiblePoll({
      intervalMs: 4000,
      maxIntervalMs: 20_000,
      signal: controller.signal,
      read: async (signal) => {
        try {
          const answer = await getButlerBriefIfChanged(api, sessionId, etag.current, signal);
          setError("");
          if (!answer) return "idle";
          etag.current = answer.etag;
          setBrief(answer.brief);
          return answer.brief.state === "working" || answer.brief.state === "waiting" ? "active" : "idle";
        } catch (caught) {
          if (signal.aborted) return "stop";
          void logCaughtDiagnostic("interface.butler_mode.brief_failed", "Butler mode could not read a chat brief.", caught, "butler_mode");
          setError(caught instanceof Error ? caught.message : "Core could not read this chat.");
          return "idle";
        }
      },
    });
    return () => controller.abort();
  }, [api, sessionId]);

  useEffect(() => { heading.current?.focus(); }, [sessionId]);

  // The step the chat is on opens by default; the operator's choices then stand.
  const activeStep = brief?.goal?.activePlanIndex;
  useEffect(() => {
    if (activeStep !== undefined) setOpenSteps((current) => current.size ? current : new Set([activeStep]));
  }, [activeStep, sessionId]);

  const turns = useMemo(() => [...older.turns, ...(brief?.turns ?? [])], [brief, older.turns]);
  const byStep = useMemo(() => groupTurnsByStep(turns), [turns]);
  const olderBefore = older.turns.length ? older.before : brief?.olderBefore;

  const loadOlder = useCallback(async () => {
    if (olderBefore === undefined) return;
    setOlder((current) => ({ ...current, loading: true, error: undefined }));
    try {
      const page = await getButlerBriefTurnsBefore(api, sessionId, olderBefore);
      setOlder((current) => ({ turns: [...page.turns, ...current.turns], before: page.olderBefore, loading: false }));
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.brief_older_failed", "Butler mode could not read older turns.", caught, "butler_mode");
      setOlder((current) => ({ ...current, loading: false, error: caught instanceof Error ? caught.message : "Core could not read older turns." }));
    }
  }, [api, olderBefore, sessionId]);

  const changeGoal = async (action: "pause" | "resume") => {
    setGoalBusy(true);
    setGoalNote("");
    try {
      const goal = await api.getChatGoal(sessionId);
      await api.writeChatGoal(sessionId, { expectedRevision: goal.revision, action });
      setGoalNote(action === "pause" ? "Pause requested. The chat stops after its current step." : "Resume requested.");
      etag.current = undefined;
      poll.current?.poke();
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.goal_action_failed", "Butler mode could not change a goal.", caught, "butler_mode");
      setGoalNote(`${caught instanceof Error ? caught.message : "Core could not change the goal."} Retry, or open the chat.`);
    } finally {
      setGoalBusy(false);
    }
  };

  const toggleStep = (key: number | "recent") => setOpenSteps((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; });

  const back = <div className={styles.crumbs}>
    <button type="button" className={styles.back} aria-label="Back to board" onClick={onBack}><ArrowLeft size={15} aria-hidden="true" /> Board</button>
    {position && <span className={styles.muted} title="Use ↑ and ↓ to move between chats">{position} · ↑↓</span>}
  </div>;

  if (!brief) return <section className={styles.brief} aria-label="Chat brief">
    {back}
    {error ? <div className={styles.error} role="alert">{error} The brief retries on its own.</div> : <p className={styles.muted} role="status">Reading the chat…</p>}
  </section>;

  const goal = brief.goal;
  const started = boardElapsed(brief.startedAt, clock);
  const meta = [brief.project.name, brief.model, started ? `started ${started} ago` : undefined, `${brief.turnCount} ${brief.turnCount === 1 ? "turn" : "turns"}`, money(brief.costUsd)].filter(Boolean).join(" · ");
  const attention = brief.attention;
  const goalLive = goal && GOAL_LIVE.has(goal.status);
  const unstepped = byStep.get(undefined) ?? [];
  const stepTurns = (index: number) => byStep.get(index) ?? [];

  const turnList = (list: ButlerBriefTurn[]) => <ol className={styles.turns}>{list.map((turn) => <li key={turn.turnId} className={styles.turn}>
    <div className={styles.turnHead}><span className={styles.turnLabel}>Turn {turn.index}</span><span className={styles.turnLine} data-status={turn.status}>{turn.line || "No answer yet."}</span></div>
    {(turnChips(turn).length > 0 || turn.files.length > 0) && <div className={styles.chips}>
      {turnChips(turn).map((chip) => <span key={chip} className={styles.chip}>{chip}</span>)}
      {turn.files.slice(0, 4).map((path) => <button key={path} type="button" className={styles.fileChip} title={`Open ${path} in Code & files`} onClick={() => onOpenFile(path, brief.project.id)}>{path.split("/").pop()}</button>)}
      {turn.files.length > 4 && <span className={styles.muted}>+{turn.files.length - 4}</span>}
    </div>}
    <button type="button" className={styles.link} onClick={() => onOpenTurn(brief.project.id, turn.turnId)}>Show raw turn <ExternalLink size={12} aria-hidden="true" /></button>
  </li>)}</ol>;

  return <section className={styles.brief} aria-label={`Chat brief: ${brief.title}`}>
    {back}
    <header className={styles.header}>
      <div className={styles.titleRow}>
        <i className={styles.dot} data-state={brief.state} aria-hidden="true" />
        <h2 ref={heading} tabIndex={-1}>{brief.title}</h2>
        <span className={styles.statePill} data-state={proposalActions ? "waiting" : brief.state}>{attention?.kind === "approval" ? "Needs approval" : proposalActions || attention?.kind === "proposal" ? "Proposed by Butler" : STATE_LABEL[brief.state]}</span>
      </div>
      <p className={styles.muted}>{meta}</p>
      <div className={styles.actions}>
        {attention?.kind === "approval" && attention.actionId && <>
          <button type="button" className={styles.primary} disabled={deciding} onClick={() => onDecide(attention.actionId!, "approve")}>Approve</button>
          <button type="button" className={styles.danger} disabled={deciding} onClick={() => onDecide(attention.actionId!, "reject")}>Deny</button>
        </>}
        {proposalActions}
        {goalLive && <button type="button" className={styles.secondary} disabled={goalBusy} onClick={() => void changeGoal("pause")}>Pause goal</button>}
        {goal?.status === "paused" && <button type="button" className={styles.secondary} disabled={goalBusy} onClick={() => void changeGoal("resume")}>Resume goal</button>}
        {brief.state === "working" && turnId && onStop && <button type="button" className={styles.secondary} onClick={onStop}>Stop</button>}
        <button type="button" className={styles.secondary} onClick={() => onAsk(brief)}>Ask Butler about this</button>
      </div>
      {(note || goalNote) && <p className={styles.note} role="status">{note || goalNote}</p>}
    </header>
    {error && <div className={styles.error} role="alert">{error} Showing what Core last returned.</div>}

    {goal && <div className={styles.card}>
      <span className={styles.label}>Goal{goal.plan.length ? ` · ${goal.activePlanIndex !== undefined ? `step ${goal.activePlanIndex + 1} of ${goal.plan.length}` : `${goal.plan.length} planned steps`}` : ""} · {goal.status}</span>
      <p>{goal.objective}</p>
      {goal.completionCriteria && <p className={`${styles.muted} ${styles.criteria}`}>Done when: {goal.completionCriteria}</p>}
    </div>}

    {(attention || goal?.blockedReason) && <div className={styles.now} data-kind={attention?.kind ?? "blocked"}>
      <span className={styles.label}>Right now</span>
      <p>{attention?.summary ?? goal?.blockedReason}</p>
      {attention?.command && <pre className={styles.command}>{attention.command}</pre>}
    </div>}

    {brief.files.length > 0 && <div className={styles.files}>
      <span className={styles.label}>Files</span>
      {brief.files.slice(0, 8).map((file) => <button key={file.path} type="button" className={styles.fileChip} title={`Open ${file.path} in Code & files`} onClick={() => onOpenFile(file.path, brief.project.id)}>{file.path.split("/").pop()}</button>)}
      {brief.files.length > 8 && <span className={styles.muted}>+{brief.files.length - 8} more</span>}
    </div>}

    <h3 className={styles.sectionTitle}>Progress</h3>
    {olderBefore !== undefined && <button type="button" className={styles.link} disabled={older.loading} onClick={() => void loadOlder()}>{older.loading ? "Loading earlier turns…" : "Show earlier turns"}</button>}
    {older.error && <div className={styles.error} role="alert">{older.error}</div>}
    {goal && goal.plan.length > 0 && <ol className={styles.steps}>{goal.plan.map((step, index) => {
      const status = goal.activePlanIndex === undefined ? "planned" : index < goal.activePlanIndex ? "done" : index === goal.activePlanIndex ? "active" : "todo";
      const list = stepTurns(index);
      const open = openSteps.has(index);
      return <li key={index} className={styles.step} data-status={status}>
        <button type="button" className={styles.stepHead} aria-expanded={open} disabled={!list.length} onClick={() => toggleStep(index)}>
          <span className={styles.stepMark} data-status={status} aria-hidden="true">{status === "done" ? "✓" : ""}</span>
          <span className={styles.stepText}><span>{index + 1} · {step}</span><small>{list.length ? `${list.length} ${list.length === 1 ? "turn" : "turns"}` : status === "todo" ? "Not started" : status === "done" ? "Done" : ""}</small></span>
          {list.length > 0 && (open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />)}
        </button>
        {open && list.length > 0 && turnList(list)}
      </li>;
    })}</ol>}
    {unstepped.length > 0 && <div className={styles.recent}>
      {goal && goal.plan.length > 0 ? <button type="button" className={styles.stepHead} aria-expanded={openSteps.has("recent") || goal.activePlanIndex === undefined} onClick={() => toggleStep("recent")}>
        <span className={styles.stepText}><span>Turns not tied to a step</span><small>{unstepped.length}</small></span>
        {openSteps.has("recent") || goal.activePlanIndex === undefined ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
      </button> : null}
      {(!goal || !goal.plan.length || openSteps.has("recent") || goal.activePlanIndex === undefined) && turnList(unstepped)}
    </div>}
    {turns.length === 0 && <p className={styles.muted}>No turns yet.</p>}

    <div className={styles.footer}>
      <span className={styles.muted}>Full transcript · {brief.turnCount} {brief.turnCount === 1 ? "turn" : "turns"}{brief.subagentCount ? ` · ${brief.subagentCount} ${brief.subagentCount === 1 ? "subagent" : "subagents"}` : ""}</span>
      {workStage && <WorkStageLabel stage={workStage} />}
      {butlerCase && <ButlerStageLabel stage={butlerCase.stage} />}
      {butlerCase?.onOpen && <button type="button" className={styles.link} onClick={butlerCase.onOpen}>Open Butler check <ExternalLink size={12} aria-hidden="true" /></button>}
      <button type="button" className={styles.link} onClick={() => onOpenChat(brief.project.id)}>Open full chat <ExternalLink size={12} aria-hidden="true" /></button>
    </div>
  </section>;
}
