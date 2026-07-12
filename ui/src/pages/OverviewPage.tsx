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
import { PageHeader } from "../components/PageHeader";
import { NewMissionButton, StopMissionButton } from "../components/MissionControls";
import { useWorkspace } from "../state/WorkspaceContext";

const missionSteps = [
  { label: "Validate scope and policy", state: "complete", actor: "Scope planner" },
  { label: "Passive asset discovery", state: "complete", actor: "Recon specialist" },
  { label: "Analyze exposed services", state: "running", actor: "Network analyst" },
  { label: "Correlate vulnerability intelligence", state: "queued", actor: "Vulnerability analyst" },
  { label: "Verify evidence and draft report", state: "queued", actor: "Evidence verifier" },
];

type EventStepState = "complete" | "running" | "waiting" | "failed" | "stopped" | "queued";

function eventStepState(kind: string): EventStepState {
  if (kind.includes("failed")) return "failed";
  if (kind.includes("cancelled") || kind === "run.stop_requested") return "stopped";
  if (kind.includes("waiting") || kind === "approval.requested" || kind === "tool.requested") return "waiting";
  if (kind.includes("completed") || kind.includes("verified") || kind.includes("resolved") || kind.includes("created") || kind === "finding.updated") return "complete";
  if (kind.includes("started") || kind.includes("status_changed") || kind === "agent.message") return "running";
  return "queued";
}

export function OverviewPage() {
  const { approvals, assets, engagement, events, findings, health, previewMode, run } = useWorkspace();
  const validatedFindings = findings.filter((finding) => ["validated", "confirmed"].includes(finding.status));
  const criticalFindings = findings.filter((finding) => finding.severity === "critical").length;
  const highFindings = findings.filter((finding) => finding.severity === "high").length;
  const completedTasks = run?.completedTasks ?? 0;
  const totalTasks = run?.totalTasks ?? 0;
  const progress = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;
  const priorityFinding = findings.find((finding) => finding.severity === "critical") ?? findings[0];
  return (
    <div className="page overview-page">
      <PageHeader
        eyebrow={engagement?.clientName ?? (previewMode ? "Acme external assessment" : "Nebula engagement")}
        title={previewMode ? "Good afternoon, Jordan" : engagement?.name ?? "No engagement available"}
        description={previewMode
          ? "Your supervised mission is progressing within scope. One action needs review."
          : run
            ? `${run.title} is ${run.status.replace("_", " ")}. ${approvals.length} approval request${approvals.length === 1 ? "" : "s"} pending.`
            : "Core is online. Create an analysis-only mission to begin supervised work."}
        actions={
          <>
            <button className="button secondary" type="button" disabled title="Scanner normalization is release-gated">Import scan unavailable</button>
            <NewMissionButton />
          </>
        }
      />

      {previewMode && (
        <div className="callout preview-callout" role="status">
          <CircleAlert size={18} aria-hidden="true" />
          <div>
            <strong>Exploring the Nebula 3 workspace</strong>
            <p>Representative engagement data is visible until the versioned Nebula Core API connects.</p>
          </div>
        </div>
      )}

      <section className="metric-grid" aria-label="Engagement summary">
        <article className="metric-card accent-blue">
          <span className="metric-icon"><Target size={19} /></span>
          <div><small>Loaded assets</small><strong>{assets.length}</strong><span>Current engagement</span></div>
          <span className="metric-trend">Core data</span>
        </article>
        <article className="metric-card accent-violet">
          <span className="metric-icon"><Bot size={19} /></span>
          <div><small>Mission status</small><strong>{run ? run.status.replace("_", " ") : "—"}</strong><span>{run ? run.title : "No run selected"}</span></div>
          <span className="metric-trend positive">{events.length} events</span>
        </article>
        <article className="metric-card accent-red">
          <span className="metric-icon"><FileCheck2 size={19} /></span>
          <div><small>Validated findings</small><strong>{validatedFindings.length}</strong><span>{criticalFindings} critical · {highFindings} high</span></div>
          <span className="metric-trend warning">{findings.length} total</span>
        </article>
        <article className="metric-card accent-green">
          <span className="metric-icon"><DollarSign size={19} /></span>
          <div><small>Recorded model cost</small><strong>{run?.spentUsd === undefined ? "—" : `$${run.spentUsd.toFixed(2)}`}</strong><span>Persisted run metadata</span></div>
          <span className="metric-trend positive">auditable</span>
        </article>
      </section>

      <div className="overview-grid">
        <section className="panel mission-panel">
          <header className="panel-header">
            <div>
              <span className="section-kicker"><span className="pulse-dot" /> {run ? `Mission ${run.status.replace("_", " ")}` : "No active mission"}</span>
              <h2>{run?.title ?? "Start an analysis-only supervised mission"}</h2>
              <p>{run?.startedAt ? `Started ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(run.startedAt))}` : "No start time recorded"}</p>
            </div>
            <div className="panel-header-actions">
              <StopMissionButton className="button quiet" />
              <Link className="button secondary" to="/agents">Open mission <ArrowUpRight size={15} /></Link>
            </div>
          </header>
          <div className="progress-row">
            <div>
              <span>Mission progress</span>
              <strong>{totalTasks > 0 ? `${progress}%` : "—"}</strong>
            </div>
            <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
            <small>{totalTasks > 0 ? `${completedTasks} of ${totalTasks} bounded tasks complete` : "Task totals are not present in run metadata"}</small>
          </div>
          {previewMode ? (
            <ol className="mission-steps">
              {missionSteps.map((step) => (
                <li className={step.state} key={step.label}>
                  <span className="step-state">
                    {step.state === "complete" ? <CheckCircle2 size={16} /> : step.state === "running" ? <span /> : null}
                  </span>
                  <div><strong>{step.label}</strong><small>{step.actor}</small></div>
                  <span className="step-label">{step.state}</span>
                </li>
              ))}
            </ol>
          ) : (
            <ol className="mission-steps">
              {events.slice(0, 5).map((event) => { const state = eventStepState(event.kind); return (
                <li className={state} key={event.id}>
                  <span className="step-state">{state === "complete" ? <CheckCircle2 size={16} /> : state === "running" ? <span /> : state === "waiting" ? <Clock3 size={13} /> : state === "failed" || state === "stopped" ? <CircleAlert size={13} /> : null}</span>
                  <div><strong>{event.summary}</strong><small>{event.actor ?? "Nebula Core"}</small></div>
                  <span className="step-label">#{event.sequence}</span>
                </li>
              ); })}
              {events.length === 0 && <li><div><strong>No persisted run events yet</strong><small>Activity appears after Core records a transition.</small></div></li>}
            </ol>
          )}
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
          ) : <div className="priority-finding"><strong>No findings recorded</strong><p>Candidate and verified findings will appear here.</p></div>}
        </section>

        <section className="panel coverage-panel">
          <header className="panel-header compact">
            <div><h2>Assessment coverage</h2><p>Deterministic progress by surface</p></div>
            <ScanSearch size={19} aria-hidden="true" />
          </header>
          <div className="coverage-list">
            {(previewMode ? [
              ["External discovery", 92, "31 / 34 assets"],
              ["Service analysis", 67, "24 / 36 services"],
              ["Web & API", 46, "6 / 13 applications"],
              ["Evidence verification", 38, "12 / 32 observations"],
            ] : [
              ["Assets loaded", assets.length ? 100 : 0, `${assets.length} records`],
              ["Findings loaded", findings.length ? 100 : 0, `${findings.length} records`],
              ["Run ledger replay", events.length ? 100 : 0, `${events.length} events in view`],
              ["Approval review", approvals.length ? 0 : 100, `${approvals.length} pending`],
            ]).map(([label, value, detail]) => (
              <div className="coverage-row" key={String(label)}>
                <div><strong>{label}</strong><span>{detail}</span></div>
                <div className="progress-track small"><span style={{ width: `${value}%` }} /></div>
                <strong>{value}%</strong>
              </div>
            ))}
          </div>
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
