import {
  Bot,
  CheckCircle2,
  CircleDashed,
  Clock3,
  DollarSign,
  FileCheck2,
  GitBranch,
  Network,
  ScanSearch,
  ShieldCheck,
  Sparkles,
  MessageSquare,
  Radio,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { AssistantMarkdown } from "../components/AssistantMarkdown";
import { PageHeader } from "../components/PageHeader";
import { DeleteMissionButton, NewMissionButton, StopMissionButton } from "../components/MissionControls";
import { useWorkspace } from "../state/WorkspaceContext";
import { useChrome } from "../state/ChromeContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

const agents = [
  { name: "Scope planner", detail: "Policy and mission decomposition", state: "complete", icon: ShieldCheck, tools: "No executable tools" },
  { name: "Recon specialist", detail: "Passive discovery and inventory", state: "complete", icon: ScanSearch, tools: "4 read-only tools" },
  { name: "Network analyst", detail: "Service and exposure analysis", state: "waiting", icon: Network, tools: "1 approval pending" },
  { name: "Web analyst", detail: "Application and API review", state: "running", icon: Sparkles, tools: "2 bounded tools" },
  { name: "Vulnerability analyst", detail: "Deterministic advisory correlation", state: "queued", icon: GitBranch, tools: "Feed access only" },
  { name: "Evidence verifier", detail: "Independent evidence validation", state: "queued", icon: CheckCircle2, tools: "No active tools" },
];

const missionStatusCopy: Record<string, string> = {
  queued: "Queued in Core and waiting for an execution slot.",
  planning: "Building the mission plan and task dependencies.",
  running: "Specialists are actively working through the plan.",
  waiting_approval: "Paused until an operator reviews a requested action.",
  paused: "Paused; no new mission work is being scheduled.",
  cancelling: "Stopping at the next safe execution boundary.",
  cancelled: "Stopped by an operator. Saved evidence remains available.",
  failed: "The mission ended with an error. Review the latest event below.",
  interrupted: "Execution was interrupted before reaching a terminal result.",
  complete: "All recorded mission work has finished.",
};

const formatEventKind = (kind: string) => kind.replaceAll(".", " · ").replaceAll("_", " ");

export function AgentsPage({ embedded = false }: { embedded?: boolean }) {
  const { setActivityOpen } = useChrome();
  const { api, approvals, events, previewMode, run, streamState = "closed" } = useWorkspace();
  const [steeringText, setSteeringText] = useState("");
  const [steering, setSteering] = useState(false);
  const [steeringError, setSteeringError] = useState<string>();
  const discuss = async () => {
    if (!api || !run || run.backend !== "harness") return;
    const chat = await api.discussRun(run.id);
    const params = new URLSearchParams(window.location.search);
    params.set("view", "chat");
    params.set("session", chat.id);
    window.history.pushState({}, "", `${window.location.pathname}?${params}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  const steer = async (event: FormEvent) => {
    event.preventDefault();
    const text = steeringText.trim();
    if (!api || !run || run.backend !== "harness" || !text) return;
    setSteering(true);
    setSteeringError(undefined);
    try {
      await api.steerRun(run.id, text);
      setSteeringText("");
    } catch (error) {
      void logCaughtDiagnostic("interface.agents_page.caught_failure_01", "A handled interface operation failed.", error, "agents_page");
      setSteeringError(error instanceof Error ? error.message : "Could not steer the harness turn.");
    } finally {
      setSteering(false);
    }
  };
  const resultEvent = events.find((event) => event.kind === "run.completed" || event.kind === "run.failed");
  const harnessEvents = events.filter((event) => event.kind.startsWith("harness."));
  const latestEvent = events[0];
  const progress = run?.totalTasks ? Math.min(100, Math.round((run.completedTasks / run.totalTasks) * 100)) : 0;
  if (!previewMode) {
    return (
      <div className="page agents-page">
        {!embedded && <PageHeader
          title="Missions"
          description="Supervise specialists, approvals, and mission limits."
        />}
        <section className="mission-commandbar" aria-label="Mission controls">
          <div><Radio size={15} /><span><strong>{run ? "Mission control" : "No mission selected"}</strong><small>{run ? `Core status: ${run.status.replaceAll("_", " ")} · live feed ${streamState}` : "Start a mission to see its plan and live execution here."}</small></span></div>
          <div>{run?.backend === "harness" && <button className="button secondary" type="button" onClick={() => void discuss()}><MessageSquare size={15} /> Discuss in chat</button>}<StopMissionButton /><DeleteMissionButton /><NewMissionButton /></div>
        </section>
        {approvals.length > 0 && <div className="callout approval-callout" role="status"><Clock3 size={19} /><div><strong>Mission paused for review</strong><p>{approvals.length} request{approvals.length === 1 ? "" : "s"} waiting.</p></div><button className="button primary" type="button" onClick={() => setActivityOpen(true)}>Review</button></div>}
        <section className="mission-hero panel">
          <div><span className="section-kicker"><span className="pulse-dot" /> {run?.status.replaceAll("_", " ") ?? "No run"}</span><h2>{run?.title ?? "No mission selected"}</h2><p>{run ? missionStatusCopy[run.status] : "Start a mission to begin recording work."}</p>{latestEvent && <div className="mission-now" aria-live="polite"><small>Latest update</small><AssistantMarkdown content={latestEvent.summary} durable={false} runnableLanguages={new Set()} onRun={() => undefined} /><span>{formatEventKind(latestEvent.kind)} · {new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(new Date(latestEvent.occurredAt))}</span></div>}</div>
          <div className="mission-hero-progress"><span><strong>{run?.completedTasks ?? 0}</strong><small>complete</small></span><span><strong>{run?.totalTasks ?? 0}</strong><small>recorded tasks</small></span><span><strong>{approvals.length}</strong><small>need review</small></span></div>
        </section>
        {run && <div className="mission-progress" aria-label={`${progress}% of recorded mission tasks complete`}><span style={{ width: `${progress}%` }} /><small>{progress}% of recorded tasks complete · {events.length} timeline event{events.length === 1 ? "" : "s"} loaded</small></div>}
        {run?.backend === "harness" && ["running", "waiting_approval"].includes(run.status) && <form className="panel mission-steer" onSubmit={(event) => void steer(event)}><label htmlFor="harness-steering">Steer active harness turn</label><div><input id="harness-steering" value={steeringText} maxLength={20_000} placeholder="Add direction without starting another turn" onChange={(event) => setSteeringText(event.target.value)} /><button className="button secondary" type="submit" disabled={steering || !steeringText.trim()}>{steering ? "Sending…" : "Steer"}</button></div>{steeringError && <DiagnosticErrorNotice error={steeringError} fallback="The harness could not be steered." compact />}</form>}
        {resultEvent && <section className={`panel mission-result ${resultEvent.kind === "run.failed" ? "failed" : "complete"}`} aria-labelledby="mission-result-title">
          <header><span className="mission-result-icon"><FileCheck2 size={19} /></span><div><small>{resultEvent.kind === "run.failed" ? "Mission ended with errors" : "Completed mission"}</small><h2 id="mission-result-title">Mission result</h2></div><span className="mission-result-sequence">#{resultEvent.sequence}</span></header>
          <div className="mission-result-body"><AssistantMarkdown content={resultEvent.summary} durable={false} runnableLanguages={new Set()} onRun={() => undefined} /></div>
          <footer>{resultEvent.actor ?? "Nebula Core"} · {new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(resultEvent.occurredAt))}</footer>
        </section>}
        <section className="panel data-panel">
          <header className="panel-header compact"><div><h2>Activity</h2><p>{harnessEvents.length ? "Replayable harness timeline with newest updates first" : "Full loaded mission timeline with newest updates first"}</p></div><GitBranch size={19} /></header>
          {harnessEvents.length > 0 ? <div className="harness-timeline" aria-live="polite">{events.map((event) => {
            const payload = event.payload;
            const data = payload.payload && typeof payload.payload === "object" && !Array.isArray(payload.payload) ? payload.payload as Record<string, unknown> : {};
            const kind = typeof payload.item_kind === "string" ? payload.item_kind : "notice";
            const status = typeof payload.item_status === "string" ? payload.item_status : undefined;
            const title = typeof payload.title === "string" ? payload.title : event.kind.replace("harness.", "").replaceAll("_", " ");
            const delta = typeof payload.delta === "string" ? payload.delta : undefined;
            return <details className={`harness-activity-card kind-${kind}${payload.parent_item_id ? " nested" : ""}`} open={kind !== "reasoning" && kind !== "compaction"} key={event.id}>
              <summary><span className={`status-dot ${["complete", "completed", "success"].includes(status ?? "") ? "healthy" : ["failed", "error", "cancelled"].includes(status ?? "") ? "unavailable" : "pending"}`} /><strong>{title}</strong><code>{kind.replaceAll("_", " ")}</code>{status && <span>{status.replaceAll("_", " ")}</span>}</summary>
              <div className="harness-activity-body"><p>{event.summary}</p>{delta && <div className="harness-output"><small>{typeof payload.stream === "string" ? payload.stream : "output"}</small><pre>{delta}</pre></div>}{typeof data.diff === "string" && data.diff && <div className="harness-output diff"><small>Unified diff</small><pre>{data.diff}</pre></div>}{Object.keys(data).length > 0 && kind !== "reasoning" && <pre className="harness-structured">{JSON.stringify(data, null, 2)}</pre>}<small>#{event.sequence} · {new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(new Date(event.occurredAt))}</small></div>
            </details>;
          })}</div> : events.length > 0 ? <ol className="event-list mission-event-list">{events.map((event) => <li key={event.id}><span className="event-icon"><Bot size={15} /></span><div className="event-summary"><span className="event-kind">{formatEventKind(event.kind)}</span><AssistantMarkdown content={event.summary} durable={false} runnableLanguages={new Set()} onRun={() => undefined} /><small>{event.actor ?? "Nebula Core"} · #{event.sequence} · {new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(new Date(event.occurredAt))}</small>{Object.keys(event.payload).length > 0 && <details className="mission-event-details"><summary>Technical details</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>}</div></li>)}</ol> : <div className="empty-state compact"><CircleDashed size={23} /><strong>No run events</strong><p>{run ? `Core feed is ${streamState}. The first recorded transition will appear here.` : "Start a mission to create a live execution timeline."}</p></div>}
        </section>
      </div>
    );
  }
  return (
    <div className="page agents-page">
      {!embedded && <PageHeader
        title="Missions"
        description="Supervise specialists, approvals, and mission limits."
        actions={
          <>
            <StopMissionButton />
            <NewMissionButton />
          </>
        }
      />}

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
