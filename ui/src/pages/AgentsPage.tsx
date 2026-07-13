import {
  Bot,
  CheckCircle2,
  CircleDashed,
  Clock3,
  DollarSign,
  GitBranch,
  Network,
  ScanSearch,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { ContextStatus } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { NewMissionButton, StopMissionButton } from "../components/MissionControls";
import { useWorkspace } from "../state/WorkspaceContext";
import { useChrome } from "../state/ChromeContext";

const agents = [
  { name: "Scope planner", detail: "Policy and mission decomposition", state: "complete", icon: ShieldCheck, tools: "No executable tools" },
  { name: "Recon specialist", detail: "Passive discovery and inventory", state: "complete", icon: ScanSearch, tools: "4 read-only tools" },
  { name: "Network analyst", detail: "Service and exposure analysis", state: "waiting", icon: Network, tools: "1 approval pending" },
  { name: "Web analyst", detail: "Application and API review", state: "running", icon: Sparkles, tools: "2 bounded tools" },
  { name: "Vulnerability analyst", detail: "Deterministic advisory correlation", state: "queued", icon: GitBranch, tools: "Feed access only" },
  { name: "Evidence verifier", detail: "Independent evidence validation", state: "queued", icon: CheckCircle2, tools: "No active tools" },
];

export function AgentsPage() {
  const { setActivityOpen } = useChrome();
  const { api, approvals, events, previewMode, run } = useWorkspace();
  const [contextStatus, setContextStatus] = useState<ContextStatus>();
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState<string>();
  useEffect(() => {
    if (!api || !run || previewMode) {
      setContextStatus(undefined);
      setContextLoading(false);
      setContextError(undefined);
      return;
    }
    const controller = new AbortController();
    setContextLoading(true);
    setContextError(undefined);
    void api.getRunContext(run.id, controller.signal)
      .then((context) => {
        setContextStatus(context);
        setContextLoading(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setContextStatus(undefined);
          setContextLoading(false);
          setContextError("Mission working memory is temporarily unavailable.");
        }
      });
    return () => controller.abort();
  }, [api, events.length, previewMode, run]);
  if (!previewMode) {
    return (
      <div className="page agents-page">
        <PageHeader
          title="Missions"
          description="Supervise specialists, approvals, and mission limits."
          actions={<><StopMissionButton /><NewMissionButton /></>}
        />
        {approvals.length > 0 && <div className="callout approval-callout" role="status"><Clock3 size={19} /><div><strong>Mission paused for review</strong><p>{approvals.length} request{approvals.length === 1 ? "" : "s"} waiting.</p></div><button className="button primary" type="button" onClick={() => setActivityOpen(true)}>Review</button></div>}
        <section className="mission-hero panel">
          <div><span className="section-kicker"><span className="pulse-dot" /> {run?.status.replace("_", " ") ?? "No run"}</span><h2>{run?.title ?? "No mission selected"}</h2><p>{approvals.length} pending approval request{approvals.length === 1 ? "" : "s"}.</p></div>
          <div className="mission-hero-progress"><span><strong>{run?.completedTasks ?? 0}</strong><small>complete</small></span><span><strong>{run?.totalTasks ?? 0}</strong><small>recorded tasks</small></span><span><strong>{events.length}</strong><small>events loaded</small></span></div>
        </section>
        <section className="panel data-panel" aria-label="Mission working memory">
          <header className="panel-header compact"><div><h2>Working memory</h2><p>Derived context for the active mission</p></div><Sparkles size={19} /></header>
          {contextLoading ? <div className="empty-state compact" role="status"><CircleDashed size={23} /><strong>Loading working memory</strong></div> : contextError ? <div className="empty-state compact" role="alert"><CircleDashed size={23} /><strong>Unable to inspect memory</strong><p>{contextError}</p></div> : contextStatus?.snapshot?.memory && contextStatus.status ? <div className="empty-state compact"><Sparkles size={23} /><strong>Memory {contextStatus.status.replace("_", " ")}</strong><p>{contextStatus.snapshot.memory.summary}</p><small>Through event #{contextStatus.compactedThrough} · {contextStatus.snapshot.providerId}/{contextStatus.snapshot.model} · {contextStatus.snapshot.usage.totalTokens} compaction tokens · ${contextStatus.snapshot.costUsd.toFixed(4)}</small><div className="scope-chip-list">{contextStatus.snapshot.sourceReferences.slice(0, 8).map((source) => <Link aria-label={`Open ${source.sourceKind.replace("_", " ")} ${source.sourceId}`} key={`${source.sourceKind}-${source.sourceId}`} to={source.sourceKind.includes("evidence") ? `/evidence?evidence=${encodeURIComponent(source.sourceId)}` : `/missions?task=${encodeURIComponent(source.sourceId)}`}>{source.sourceKind.replace("_", " ")}: {source.sourceId}</Link>)}</div>{contextStatus.snapshot.memory.evidenceIds.length > 0 && <div className="scope-chip-list">{contextStatus.snapshot.memory.evidenceIds.map((id) => <Link key={id} to={`/evidence?evidence=${encodeURIComponent(id)}`}>Evidence: {id}</Link>)}</div>}</div> : contextStatus?.status === "failed" ? <div className="empty-state compact" role="alert"><CircleDashed size={23} /><strong>Compaction failed</strong><p>Retry the mission step with the configured provider. No canonical result or checkpoint was removed.</p></div> : <div className="empty-state compact"><CircleDashed size={23} /><strong>{contextStatus?.status === "stale" ? "Working memory is stale" : "Compaction not needed"}</strong><p>{contextStatus?.status === "stale" ? "New canonical task data is available; the next required model context will refresh memory." : "Current dependency summaries fit within the configured model context."}</p></div>}
        </section>
        <section className="panel data-panel">
          <header className="panel-header compact"><div><h2>Activity</h2><p>Latest mission transitions</p></div><GitBranch size={19} /></header>
          {events.length > 0 ? <ol className="event-list">{events.slice(0, 10).map((event) => <li key={event.id}><span className="event-icon"><Bot size={15} /></span><div><p>{event.summary}</p><small>{event.actor ?? "Nebula Core"} · #{event.sequence}</small></div></li>)}</ol> : <div className="empty-state compact"><CircleDashed size={23} /><strong>No run events</strong><p>The selected run has not recorded a transition yet.</p></div>}
        </section>
      </div>
    );
  }
  return (
    <div className="page agents-page">
      <PageHeader
        title="Missions"
        description="Supervise specialists, approvals, and mission limits."
        actions={
          <>
            <StopMissionButton />
            <NewMissionButton />
          </>
        }
      />

      {approvals.length > 0 && <div className="callout approval-callout" role="status"><Clock3 size={19} /><div><strong>Approval required</strong><p>{approvals.length} request{approvals.length === 1 ? "" : "s"} waiting.</p></div><button className="button primary" type="button" onClick={() => setActivityOpen(true)}>Review</button></div>}

      <section className="mission-hero panel">
        <div>
          <span className="section-kicker"><span className="pulse-dot" /> Mission running</span>
          <h2>External attack surface review</h2>
          <p>Six specialists · 25 tasks</p>
        </div>
        <div className="mission-hero-progress">
          <span><strong>12</strong><small>complete</small></span>
          <span><strong>3</strong><small>active</small></span>
          <span><strong>10</strong><small>queued</small></span>
        </div>
      </section>

      <div className="agent-layout">
        <section className="panel agent-graph-panel">
          <header className="panel-header compact"><div><h2>Mission graph</h2><p>Dependencies and live execution state</p></div><GitBranch size={19} /></header>
          <div className="agent-graph" aria-label="Agent mission dependency graph">
            <div className="graph-node supervisor"><span><Bot size={20} /></span><div><strong>Supervisor</strong><small>Routing and synthesis</small></div><em>running</em></div>
            <div className="graph-line vertical" />
            <div className="graph-branches" aria-hidden="true"><span /><span /><span /></div>
            <div className="graph-row">
              {agents.slice(0, 3).map(({ name, state, icon: Icon }) => (
                <div className={`graph-node ${state}`} key={name}><span><Icon size={18} /></span><div><strong>{name}</strong><small>{state}</small></div></div>
              ))}
            </div>
            <div className="graph-row secondary">
              {agents.slice(3).map(({ name, state, icon: Icon }) => (
                <div className={`graph-node ${state}`} key={name}><span><Icon size={18} /></span><div><strong>{name}</strong><small>{state}</small></div></div>
              ))}
            </div>
          </div>
        </section>

        <section className="panel budget-panel">
          <header className="panel-header compact"><div><h2>Mission guardrails</h2><p>Hard limits enforced by Core</p></div><ShieldCheck size={19} /></header>
          <div className="budget-item"><div><span><DollarSign size={15} /> Model cost</span><strong>$18.42 / $50</strong></div><div className="progress-track small"><span style={{ width: "37%" }} /></div></div>
          <div className="budget-item"><div><span><Clock3 size={15} /> Duration</span><strong>1h 28m / 4h</strong></div><div className="progress-track small"><span style={{ width: "37%" }} /></div></div>
          <div className="budget-item"><div><span><Sparkles size={15} /> Tool calls</span><strong>86 / 250</strong></div><div className="progress-track small"><span style={{ width: "34%" }} /></div></div>
          <dl className="limit-list"><div><dt>Max concurrency</dt><dd>4 agents</dd></div><div><dt>Delegation depth</dt><dd>1 level</dd></div><div><dt>Per-target active work</dt><dd>1 task</dd></div><div><dt>Retries per task</dt><dd>2</dd></div></dl>
        </section>
      </div>

      <section className="agent-roster">
        <div className="section-heading"><div><h2>Specialists</h2><p>Least-privilege tools and structured context</p></div></div>
        <div className="agent-card-grid">
          {agents.map(({ name, detail, state, icon: Icon, tools }) => (
            <article className="agent-card" key={name}>
              <header><span className={`agent-icon ${state}`}><Icon size={19} /></span><span className={`agent-state ${state}`}>{state === "running" ? <span className="pulse-dot" /> : state === "queued" ? <CircleDashed size={12} /> : null}{state}</span></header>
              <h3>{name}</h3><p>{detail}</p>
              <footer><span>{tools}</span></footer>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
