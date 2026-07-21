import {
  ArrowUpRight,
  Bot,
  CheckCircle2,
  CircleAlert,
  Clock3,
  DollarSign,
  FileCheck2,
  ScanSearch,
  ShieldCheck,
  Target,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { ExecutionLanguage } from "../api/types";
import { AssistantMarkdown } from "../components/AssistantMarkdown";
import { PageHeader } from "../components/PageHeader";
import { NewMissionButton, StopMissionButton } from "../components/MissionControls";
import { useWorkspace } from "../state/WorkspaceContext";
import { useChrome } from "../state/ChromeContext";

type EventStepState = "complete" | "running" | "waiting" | "failed" | "stopped" | "queued";

const noRunnableLanguages = new Set<ExecutionLanguage>();

function eventStepState(kind: string): EventStepState {
  if (kind.includes("failed") || kind.includes("blocked")) return "failed";
  if (kind.includes("cancelled") || kind === "run.stop_requested") return "stopped";
  if (kind.includes("waiting") || kind === "approval.requested" || kind === "tool.requested") return "waiting";
  if (kind === "task.turn_completed" || kind === "task.continuing" || kind === "task.retry_scheduled") return "running";
  if (kind.includes("completed") || kind.includes("verified") || kind.includes("resolved") || kind.includes("created") || kind === "finding.updated") return "complete";
  if (kind.includes("started") || kind.includes("status_changed") || kind === "agent.message") return "running";
  return "queued";
}

export function OverviewPage() {
  const { setActivityOpen } = useChrome();
  const { approvals, assets, engagement, events, findings, health, run } = useWorkspace();
  const validatedFindings = findings.filter((finding) => ["validated", "confirmed"].includes(finding.status));
  const criticalFindings = findings.filter((finding) => finding.severity === "critical").length;
  const highFindings = findings.filter((finding) => finding.severity === "high").length;
  const completedTasks = run?.completedTasks ?? 0;
  const totalTasks = run?.totalTasks ?? 0;
  const progress = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;
  const missionTitle = run?.title;
  const missionStatus = run?.status.replace("_", " ");
  const priorityFinding = findings.find((finding) => finding.severity === "critical") ?? findings[0];
  const hasCoverage = assets.length > 0 || findings.length > 0 || events.length > 0 || approvals.length > 0;
  return (
    <div className="page overview-page">
      <PageHeader
        eyebrow={engagement?.clientName ?? "Nebula project"}
        title={engagement?.name ?? "No project available"}
        description={run
            ? `${run.title} · ${run.status.replace("_", " ")}`
            : "Project status at a glance."}
        actions={<NewMissionButton showSetupGuidance={false} />}
      />

      {approvals.length > 0 && (
        <div className="callout approval-callout" role="status">
          <Clock3 size={19} aria-hidden="true" />
          <div><strong>{approvals.length} approval{approvals.length === 1 ? "" : "s"} waiting</strong><p>Mission paused for review.</p></div>
          <button className="button primary" type="button" onClick={() => setActivityOpen(true)}>Review</button>
        </div>
      )}

      <section className="metric-grid" aria-label="Project summary">
        <article className="metric-card accent-blue">
          <span className="metric-icon"><Target size={19} /></span>
          <div><small>Assets</small><strong>{assets.length}</strong><span>In this project</span></div>
        </article>
        <article className="metric-card accent-violet">
          <span className="metric-icon"><Bot size={19} /></span>
          <div><small>Mission</small><strong>{missionStatus ?? "—"}</strong><span title={missionTitle ?? "No active run"}>{missionTitle ?? "No active run"}</span></div>
        </article>
        <article className="metric-card accent-red">
          <span className="metric-icon"><FileCheck2 size={19} /></span>
          <div><small>Findings</small><strong>{validatedFindings.length}</strong><span>{findings.length} total · {criticalFindings + highFindings} priority</span></div>
        </article>
        <article className="metric-card accent-green">
          <span className="metric-icon"><DollarSign size={19} /></span>
          <div><small>Model cost</small><strong>{run?.spentUsd === undefined ? "—" : `$${run.spentUsd.toFixed(2)}`}</strong><span>Recorded for this mission</span></div>
        </article>
      </section>

      <div className="overview-grid">
        <section className={`panel mission-panel${events.length === 0 ? " is-empty" : ""}`}>
          <header className="panel-header">
            <div>
              {missionStatus && <span className="section-kicker"><span className="pulse-dot" /> {missionStatus}</span>}
              <h2>{missionTitle ?? "No active mission"}</h2>
              <p>{run?.startedAt ? `Started ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(run.startedAt))}` : run ? "Mission status from Core" : "Start a supervised analysis when you’re ready."}</p>
            </div>
            <div className="panel-header-actions">
              {run && <StopMissionButton className="button quiet" />}
              <Link className="button secondary" to="/?view=activity">{run ? "Open activity" : "View activity"} <ArrowUpRight size={15} /></Link>
            </div>
          </header>
          {totalTasks > 0 && <div className="progress-row">
            <div>
              <span>Mission progress</span>
              <strong>{progress}%</strong>
            </div>
            <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
            <small>{completedTasks} of {totalTasks} tasks complete</small>
          </div>}
          {events.length > 0 ? (
            <ol className="mission-steps">
              {events.slice(0, 5).map((event) => { const state = eventStepState(event.kind); return (
                <li className={state} key={event.id}>
                  <span className="step-state">{state === "complete" ? <CheckCircle2 size={16} /> : state === "running" ? <span /> : state === "waiting" ? <Clock3 size={13} /> : state === "failed" || state === "stopped" ? <CircleAlert size={13} /> : null}</span>
                  <div className="mission-step-summary">
                    <AssistantMarkdown content={event.summary} durable={false} runnableLanguages={noRunnableLanguages} onRun={() => undefined} />
                    <small>{event.actor ?? "Nebula Core"}</small>
                  </div>
                  <span className="step-label">#{event.sequence}</span>
                </li>
              ); })}
            </ol>
          ) : <div className="mission-events-empty"><Clock3 size={18} aria-hidden="true" /><div><strong>No mission activity</strong><small>Events appear after Core records a transition.</small></div></div>}
        </section>

        <section className="panel risk-panel">
          <header className="panel-header compact">
            <div><h2>Finding posture</h2><p>Validated and confirmed</p></div>
            <Link to="/findings" className="text-link">View all</Link>
          </header>
          <div className="risk-summary">
            <div className="risk-ring" aria-label={`${validatedFindings.length} validated findings`}>
              <span><strong>{validatedFindings.length}</strong><small>validated</small></span>
            </div>
            <ul className="risk-legend">
              {(["critical", "high", "medium", "low"] as const).map((severity) => (
                <li key={severity}><span className={`severity-dot ${severity}`} /><strong>{findings.filter((finding) => finding.severity === severity).length}</strong> {severity}</li>
              ))}
            </ul>
          </div>
          {priorityFinding ? (
            <div className="priority-finding">
              <span className="risk-badge exploit">{priorityFinding.cveIds[0] ?? priorityFinding.severity}</span>
              <strong>{priorityFinding.title}</strong>
              <p>{priorityFinding.evidenceCount} evidence record{priorityFinding.evidenceCount === 1 ? "" : "s"} · {priorityFinding.status.replace("_", " ")}</p>
              <Link to="/findings">Review finding <ArrowUpRight size={14} /></Link>
            </div>
          ) : <div className="priority-finding empty"><strong>No findings yet</strong></div>}
        </section>

        <section className="panel coverage-panel">
          <header className="panel-header compact">
            <div><h2>Assessment coverage</h2><p>Deterministic progress by surface</p></div>
            <ScanSearch size={19} aria-hidden="true" />
          </header>
          {hasCoverage ? <div className="coverage-list">
            {([
              ["Assets loaded", assets.length ? 100 : 0, `${assets.length} records`],
              ["Findings loaded", findings.length ? 100 : 0, `${findings.length} records`],
              ["Run ledger replay", events.length ? 100 : 0, `${events.length} events in view`],
              ["Approval review", approvals.length ? 0 : 100, `${approvals.length} pending`],
            ] as const).map(([label, value, detail]) => (
              <div className="coverage-row" key={String(label)}>
                <div><strong>{label}</strong><span>{detail}</span></div>
                <div className="progress-track small"><span style={{ width: `${value}%` }} /></div>
                <strong>{value}%</strong>
              </div>
            ))}
          </div> : <div className="coverage-empty"><strong>No mission coverage yet</strong></div>}
        </section>

        <section className="panel policy-panel">
          <header className="panel-header compact">
            <div><h2>Scope & safety</h2><p>Current mission policy</p></div>
            <ShieldCheck size={20} aria-hidden="true" />
          </header>
          <dl className="policy-facts">
            <div><dt>Runner</dt><dd><span className={`status-dot ${health?.runner === "ready" ? "healthy" : "unavailable"}`} /> {health?.runner ?? "Core unavailable"}</dd></div>
            <div><dt>Assets</dt><dd>{assets.length} loaded records</dd></div>
            <div><dt>Autonomy</dt><dd>Scoped approvals</dd></div>
            <div><dt>Pending</dt><dd><Clock3 size={14} /> {approvals.length} approvals</dd></div>
          </dl>
        </section>
      </div>
    </div>
  );
}
