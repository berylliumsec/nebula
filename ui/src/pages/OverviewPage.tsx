import {
  ArrowUpRight,
  CheckCircle2,
  CircleAlert,
  Clock3,
  ShieldCheck,
} from "lucide-react";
import { Link } from "react-router-dom";
import type { ExecutionLanguage } from "../api/types";
import { AssistantMarkdown } from "../components/AssistantMarkdown";
import { PageHeader } from "../components/PageHeader";
import { ProjectSummaryCards } from "../components/ProjectSummaryCards";
import { ProjectWorkOverview } from "../components/ProjectWorkOverview";
export { formatProjectModelCost } from "../components/ProjectSummaryCards";
import { NewMissionButton, StopMissionButton } from "../components/MissionControls";
import { useWorkspace } from "../state/WorkspaceContext";
import { useChrome } from "../state/ChromeContext";
import { useTheme } from "../state/ThemeContext";
import { projectRoot, projectSurface } from "../resourceRoutes";

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
  const { resolvedTheme } = useTheme();
  const studioDark = resolvedTheme === "studio-dark";
  const { approvals, assets, engagement, engagements, events, findings, health, run } = useWorkspace();
  const parentProject = engagements?.find((project) => project.id === engagement?.parentEngagementId);
  const validatedFindings = findings.filter((finding) => ["validated", "confirmed"].includes(finding.status));
  const completedTasks = run?.completedTasks ?? 0;
  const totalTasks = run?.totalTasks ?? 0;
  const progress = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;
  const missionTitle = run?.title;
  const missionStatus = run?.status.replace("_", " ");
  const priorityFinding = findings.find((finding) => finding.severity === "critical") ?? findings[0];
  const hasCoverage = assets.length > 0 || findings.length > 0 || events.length > 0 || approvals.length > 0;
  const heroDestination = engagement?.id
    ? `${projectSurface(engagement.id, "workbench")}?view=${run ? "activity" : "chat"}`
    : "/?view=chat";
  return (
    <div className={`page overview-page${studioDark ? " studio-overview" : ""}`}>
      <PageHeader
        eyebrow={engagement?.clientName ?? "Nebula project"}
        title={engagement?.name ?? "No project available"}
        description={run
            ? `${run.title} · ${run.status.replace("_", " ")}`
            : "Project status at a glance."}
        actions={<>{parentProject && <Link className="button quiet" to={projectRoot(parentProject.id)}>{parentProject.name} <ArrowUpRight size={15} aria-hidden="true" /></Link>}<Link className="button quiet" to="/projects">All projects <ArrowUpRight size={15} aria-hidden="true" /></Link><NewMissionButton showSetupGuidance={false} /></>}
      />

      <nav className="project-overview-mobile-links" aria-label="Project hierarchy">
        {parentProject && <Link to={projectRoot(parentProject.id)}>{parentProject.name} <ArrowUpRight size={15} aria-hidden="true" /></Link>}
        <Link to="/projects">All projects <ArrowUpRight size={15} aria-hidden="true" /></Link>
      </nav>

      {approvals.length > 0 && (
        <div className="callout approval-callout" role="status">
          <Clock3 size={19} aria-hidden="true" />
          <div><strong>{approvals.length} approval{approvals.length === 1 ? "" : "s"} waiting</strong><p>Mission paused for review.</p></div>
          <button className="button primary" type="button" onClick={() => setActivityOpen(true)}>Review</button>
        </div>
      )}

      {studioDark && engagement?.id && <section className="studio-project-hero" aria-label="Project orientation">
        <div>
          <span className="studio-hero-eyebrow">YOUR WORKSPACE, IN FOCUS</span>
          <h2>{run ? "Keep the mission moving." : "A clearer view of what matters."}</h2>
          <p>{run
            ? `${run.title} is ${run.status.replace("_", " ")}. Review the latest activity and decide what needs your attention.`
            : "See your project state, follow active work, and take the next step from one quiet space."}</p>
          <Link to={heroDestination} className="studio-hero-link">
            {run ? "Explore activity" : "Open workbench"} <ArrowUpRight size={16} aria-hidden="true" />
          </Link>
        </div>
        <div className="studio-hero-orbit" aria-hidden="true"><span /><span /><span /></div>
      </section>}

      {!studioDark && engagement?.id && <ProjectWorkOverview projectId={engagement.id} />}

      {(hasCoverage || (studioDark && engagement?.id)) && <ProjectSummaryCards assets={assets} findings={findings} run={run} />}

      {studioDark && engagement?.id && <ProjectWorkOverview projectId={engagement.id} />}

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

        <section className="panel assessment-panel">
          <header className="panel-header compact">
            <div><h2>Assessment posture</h2><p>Current scope, validation, and review state</p></div>
            <ShieldCheck size={20} aria-hidden="true" />
          </header>
          <dl className="policy-facts">
            <div><dt>Runner</dt><dd><span className={`status-dot ${health?.runner === "ready" ? "healthy" : "unavailable"}`} /> {health?.runner ?? "Core unavailable"}</dd></div>
            <div><dt>Assets</dt><dd>{assets.length} loaded</dd></div>
            <div><dt>Validated</dt><dd>{validatedFindings.length} finding{validatedFindings.length === 1 ? "" : "s"}</dd></div>
            <div><dt>Pending</dt><dd><Clock3 size={14} /> {approvals.length} approval{approvals.length === 1 ? "" : "s"}</dd></div>
          </dl>
          {priorityFinding ? (
            <div className="priority-finding">
              <span className="risk-badge exploit">{priorityFinding.cveIds[0] ?? priorityFinding.severity}</span>
              <strong>{priorityFinding.title}</strong>
              <p>{priorityFinding.evidenceCount} evidence record{priorityFinding.evidenceCount === 1 ? "" : "s"} · {priorityFinding.status.replace("_", " ")}</p>
              <Link to="/findings">Review finding <ArrowUpRight size={14} /></Link>
            </div>
          ) : <div className="assessment-guidance"><strong>No findings recorded</strong><p>Start a mission or add an asset when you are ready to assess this project.</p><Link to="/findings">Open findings <ArrowUpRight size={14} /></Link></div>}
          {hasCoverage && <div className="coverage-list compact">
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
          </div>}
        </section>
      </div>
    </div>
  );
}
