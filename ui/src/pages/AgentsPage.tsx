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
  const { approvals, events, previewMode, run } = useWorkspace();
  if (!previewMode) {
    return (
      <div className="page agents-page">
        <PageHeader
          eyebrow="Supervised execution"
          title="Missions"
          description="Persisted run state and activity from Nebula Core. Specialist task details appear when the tasks resource is connected."
          actions={<><StopMissionButton /><NewMissionButton /></>}
        />
        {approvals.length > 0 && <div className="callout approval-callout" role="status"><Clock3 size={19} /><div><strong>Mission waiting for operator review</strong><p>{approvals.length} exact request{approvals.length === 1 ? "" : "s"} must be reviewed before bounded work can continue.</p></div><button className="button primary" type="button" onClick={() => setActivityOpen(true)}>Review</button></div>}
        <section className="mission-hero panel">
          <div><span className="section-kicker"><span className="pulse-dot" /> {run?.status.replace("_", " ") ?? "No run"}</span><h2>{run?.title ?? "No mission selected"}</h2><p>{approvals.length} pending approval request{approvals.length === 1 ? "" : "s"}.</p></div>
          <div className="mission-hero-progress"><span><strong>{run?.completedTasks ?? 0}</strong><small>complete</small></span><span><strong>{run?.totalTasks ?? 0}</strong><small>recorded tasks</small></span><span><strong>{events.length}</strong><small>events loaded</small></span></div>
        </section>
        <section className="panel data-panel">
          <header className="panel-header compact"><div><h2>Persisted activity</h2><p>Latest replayed transitions for the selected run</p></div><GitBranch size={19} /></header>
          {events.length > 0 ? <ol className="event-list">{events.slice(0, 10).map((event) => <li key={event.id}><span className="event-icon"><Bot size={15} /></span><div><p>{event.summary}</p><small>{event.actor ?? "Nebula Core"} · #{event.sequence}</small></div></li>)}</ol> : <div className="empty-state compact"><CircleDashed size={23} /><strong>No run events</strong><p>The selected run has not recorded a transition yet.</p></div>}
        </section>
      </div>
    );
  }
  return (
    <div className="page agents-page">
      <PageHeader
        eyebrow="Supervised execution"
        title="Missions"
        description="One supervisor coordinates bounded specialists. Every transition is persisted before it is streamed."
        actions={
          <>
            <StopMissionButton />
            <NewMissionButton />
          </>
        }
      />

      {approvals.length > 0 && <div className="callout approval-callout" role="status"><Clock3 size={19} /><div><strong>Approval required</strong><p>Review the exact target, arguments, and expected effects before this mission continues.</p></div><button className="button primary" type="button" onClick={() => setActivityOpen(true)}>Review</button></div>}

      <section className="mission-hero panel">
        <div>
          <span className="section-kicker"><span className="pulse-dot" /> Mission running</span>
          <h2>External attack surface review</h2>
          <p>Supervisor is coordinating six specialists across 25 bounded tasks.</p>
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
              <footer><span>{tools}</span><span>Trail in activity center</span></footer>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
