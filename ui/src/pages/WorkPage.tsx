import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, CircleAlert, Clock3, ListTodo, Plus, RefreshCw, X } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import type { ChatSessionActivity, ChatSessionSummary } from "../api/types";
import { projectSurface } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";
import "./WorkPage.css";

type Status = "backlog" | "ready" | "in_progress" | "blocked" | "review" | "done";
type Priority = "low" | "normal" | "high" | "urgent";
type WorkItem = {
  id: string; engagement_id: string; title: string; description: string;
  status: Status; priority: Priority; assignee_session_id: string | null;
  source_kind: "chat" | "mission" | "manual" | "import"; source_id: string | null;
  created_at: string; updated_at: string; last_update_at: string | null;
};
type WorkUpdate = {
  id: string; engagement_id: string; item_id: string; summary: string;
  next_step: string | null; blocker: string | null; status: Status;
  actor_kind: "operator" | "agent" | "import"; actor_id: string;
  source_session_id: string | null; source_turn_id: string | null;
  source_run_id: string | null; created_at: string;
};

const columns: { id: Status; label: string }[] = [
  { id: "backlog", label: "Backlog" }, { id: "ready", label: "Ready" },
  { id: "in_progress", label: "In progress" }, { id: "blocked", label: "Blocked" },
  { id: "review", label: "Review" }, { id: "done", label: "Done" },
];
const label = (status: Status) => columns.find((column) => column.id === status)?.label ?? status;
const time = (value: string | null | undefined) => value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "No update yet";
const errorText = (value: unknown) => value instanceof Error ? value.message : "Core could not load Work. Try again.";

export function WorkPage() {
  const { api, engagement, engagements, coreState } = useWorkspace();
  const { projectId, itemId } = useParams();
  const navigate = useNavigate();
  const [items, setItems] = useState<WorkItem[]>([]);
  const [recent, setRecent] = useState<WorkUpdate[]>([]);
  const [itemUpdates, setItemUpdates] = useState<WorkUpdate[]>([]);
  const [sessions, setSessions] = useState<Record<string, ChatSessionSummary>>({});
  const [activity, setActivity] = useState<Record<string, ChatSessionActivity["state"]>>({});
  const [activityProjects, setActivityProjects] = useState<Record<string, string>>({});
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [showCreate, setShowCreate] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [createProjectId, setCreateProjectId] = useState(engagement?.id ?? "");
  const [summary, setSummary] = useState("");
  const [nextStep, setNextStep] = useState("");
  const [blocker, setBlocker] = useState("");
  const [nextStatus, setNextStatus] = useState<Status>("in_progress");
  const selectedProject = engagements.find((project) => project.id === projectId);
  const selectedItem = items.find((item) => item.id === itemId);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!api) return;
    try {
      const loaded = await api.request<WorkItem[]>(projectId
        ? `engagements/${encodeURIComponent(projectId)}/work` : "work/items", { signal });
      if (signal?.aborted) return;
      setItems(loaded);
      if (projectId) {
        const [sessionPage, active] = await Promise.all([
          api.listChatSessions(projectId, signal), api.listChatSessionActivity(projectId, signal),
        ]);
        if (signal?.aborted) return;
        setSessions(Object.fromEntries(sessionPage.items.map((session) => [session.id, session])));
        setActivity(Object.fromEntries(active.map((session) => [session.sessionId, session.state])));
        setActivityProjects(Object.fromEntries(active.map((session) => [session.sessionId, projectId])));
      } else {
        const updates = await api.request<WorkUpdate[]>("work/updates", { signal });
        if (signal?.aborted) return;
        setRecent(updates);
        const activeProjects = engagements.filter((project) => project.status !== "archived");
        const states = await Promise.allSettled(activeProjects.map(async (project) => ({
          projectId: project.id,
          activity: await api.listChatSessionActivity(project.id, signal),
          sessions: await api.listChatSessions(project.id, signal),
        })));
        if (signal?.aborted) return;
        const loaded = states.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
        setActivity(Object.fromEntries(loaded.flatMap((result) => result.activity.map((entry) => [entry.sessionId, entry.state] as const))));
        setActivityProjects(Object.fromEntries(loaded.flatMap((result) => result.activity.map((entry) => [entry.sessionId, result.projectId] as const))));
        setSessions(Object.fromEntries(loaded.flatMap((result) => result.sessions.items.map((session) => [session.id, session] as const))));
      }
      setError(undefined);
    } catch (failure) {
      if (!signal?.aborted) setError(errorText(failure));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, engagements, projectId]);

  useEffect(() => { setEnabled(selectedProject?.workEnabled ?? false); }, [projectId, selectedProject?.workEnabled]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void refresh(controller.signal);
    const interval = window.setInterval(() => void refresh(controller.signal), 30_000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [refresh]);

  useEffect(() => {
    if (!api || !projectId || !itemId) { setItemUpdates([]); return; }
    const controller = new AbortController();
    const load = () => api.request<WorkUpdate[]>(`engagements/${encodeURIComponent(projectId)}/work/${encodeURIComponent(itemId)}/updates`, { signal: controller.signal })
      .then(setItemUpdates).catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorText(failure)); });
    void load();
    const interval = window.setInterval(() => void load(), 30_000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [api, projectId, itemId]);

  useEffect(() => { if (selectedItem) setNextStatus(selectedItem.status); }, [selectedItem?.id, selectedItem?.status]);
  useEffect(() => { if (engagement?.id && !createProjectId) setCreateProjectId(engagement.id); }, [engagement?.id, createProjectId]);

  const counts = useMemo(() => Object.fromEntries(columns.map((column) => [column.id, items.filter((item) => item.status === column.id).length])) as Record<Status, number>, [items]);
  const working = useMemo(() => items.filter((item) => item.assignee_session_id && activity[item.assignee_session_id] === "working" && item.status !== "done"), [items, activity]);
  const workingAgents = useMemo(() => Object.entries(activity).filter(([, state]) => state === "working").map(([sessionId]) => ({
    sessionId,
    projectId: activityProjects[sessionId],
    title: sessions[sessionId]?.title || "Active conversation",
    item: working.find((candidate) => candidate.assignee_session_id === sessionId),
  })).filter((entry) => Boolean(entry.projectId)), [activity, activityProjects, sessions, working]);
  const stalled = useMemo(() => working.filter((item) => Date.now() - new Date(item.last_update_at ?? item.created_at).getTime() > 30 * 60_000), [working]);
  const projectName = (id: string) => engagements.find((project) => project.id === id)?.name ?? "Project unavailable";
  const assignee = (item: WorkItem) => item.assignee_session_id ? sessions[item.assignee_session_id]?.title ?? "Agent session" : "Unassigned";

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !title.trim()) return;
    const target = projectId ?? createProjectId;
    if (!target) { setError("Choose a project first."); return; }
    setBusy(true); setError(undefined);
    try {
      const item = await api.request<WorkItem>(`engagements/${encodeURIComponent(target)}/work`, {
        method: "POST", body: JSON.stringify({ title: title.trim(), description: description.trim(), status: "ready" }),
      });
      setTitle(""); setDescription(""); setShowCreate(false);
      await refresh();
      navigate(projectSurface(target, "work", item.id));
    } catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  };

  const checkIn = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !projectId || !selectedItem || !summary.trim()) return;
    setBusy(true); setError(undefined);
    try {
      await api.request<WorkUpdate>(`engagements/${encodeURIComponent(projectId)}/work/${encodeURIComponent(selectedItem.id)}/updates`, {
        method: "POST", body: JSON.stringify({ summary: summary.trim(), next_step: nextStep.trim() || null, blocker: blocker.trim() || null, status: nextStatus }),
      });
      setSummary(""); setNextStep(""); setBlocker("");
      await refresh();
      setItemUpdates(await api.request<WorkUpdate[]>(`engagements/${encodeURIComponent(projectId)}/work/${encodeURIComponent(selectedItem.id)}/updates`));
    } catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  };

  const toggle = async () => {
    if (!api || !projectId) return;
    setBusy(true); setError(undefined);
    try {
      const saved = await api.request<{ work_enabled: boolean }>(`engagements/${encodeURIComponent(projectId)}/work/setting`, {
        method: "PATCH", body: JSON.stringify({ enabled: !enabled }),
      });
      setEnabled(saved.work_enabled);
      await refresh();
    } catch (failure) { setError(errorText(failure)); }
    finally { setBusy(false); }
  };

  return <div className="work-page page">
    <header className="work-head">
      <div>
        {projectId && <Link className="work-back" to="/work"><ArrowLeft size={14} /> All work</Link>}
        <p className="work-eyebrow">{projectId ? selectedProject?.name ?? "Project" : "All projects"}</p>
        <h1>Work</h1>
        <p className="work-lede">{projectId ? "Plan tasks and follow agent check-ins in one place." : "See what agents are doing across your projects."}</p>
      </div>
      <div className="work-head-actions">
        <button className="button quiet work-refresh" type="button" aria-label="Refresh Work" title="Refresh Work" onClick={() => void refresh()}><RefreshCw size={16} /></button>
        {projectId && <button className="button quiet" type="button" disabled={busy || coreState !== "online"} onClick={() => void toggle()}>{enabled ? "Agent tools on" : "Enable agent tools"}</button>}
        <button className="button primary" type="button" disabled={!api || coreState !== "online"} onClick={() => setShowCreate(true)}><Plus size={16} /> New item</button>
      </div>
    </header>
    {projectId && <p className="work-setting-note">{enabled ? "Agents can update this project through Nebula's built-in MCP. Nebula prompts active agents after 20 minutes without a check-in." : "Agent tools are off. Existing Work records remain visible. Enable them when agents should post updates."}</p>}
    {error && <div className="work-error" role="alert"><CircleAlert size={17} /><span>{error}</span><button type="button" className="button quiet" onClick={() => void refresh()}>Retry</button></div>}
    {loading ? <div className="work-loading" role="status">Loading Work…</div> : <>
      <section className="work-summary" aria-label="Work summary">
        <div><strong>{workingAgents.length}</strong><span>Working now</span></div>
        <div><strong>{stalled.length}</strong><span>Check-ins due</span></div>
        <div><strong>{counts.blocked}</strong><span>Blocked</span></div>
        <div><strong>{counts.review}</strong><span>In review</span></div>
      </section>
      {!projectId ? <>
        <div className="work-overview-grid">
          <section className="work-panel" aria-labelledby="work-active-title">
            <div className="work-panel-head"><h2 id="work-active-title">Agents working now</h2><span>{workingAgents.length}</span></div>
            {workingAgents.length ? <ul className="work-activity-list">{workingAgents.map((agent) => <li key={agent.sessionId}>
              <span className="work-live-dot" aria-hidden="true" /><div><strong>{agent.item?.title ?? agent.title}</strong><small>{projectName(agent.projectId)} · {agent.item ? time(agent.item.last_update_at) : "No Work item linked"}</small></div>
              <Link aria-label={`Open ${agent.item?.title ?? agent.title}`} to={agent.item ? projectSurface(agent.projectId, "work", agent.item.id) : `${projectSurface(agent.projectId, "workbench")}?session=${encodeURIComponent(agent.sessionId)}`}><ArrowRight size={16} /></Link>
            </li>)}</ul> : <p className="work-empty-inline">No agents are working right now.</p>}
          </section>
          <section className="work-panel" aria-labelledby="work-attention-title">
            <div className="work-panel-head"><h2 id="work-attention-title">Needs attention</h2><span>{counts.blocked + stalled.length}</span></div>
            {[...items.filter((item) => item.status === "blocked"), ...stalled.filter((item) => item.status !== "blocked")].slice(0, 8).map((item) => <Link className="work-attention-row" key={item.id} to={projectSurface(item.engagement_id, "work", item.id)}><CircleAlert size={16} /><span><strong>{item.title}</strong><small>{projectName(item.engagement_id)} · {item.status === "blocked" ? "Blocked" : "Check-in due"}</small></span><ArrowRight size={15} /></Link>)}
            {!counts.blocked && !stalled.length && <p className="work-empty-inline">No blocked items or overdue check-ins.</p>}
          </section>
        </div>
        <section className="work-panel work-projects" aria-labelledby="work-projects-title"><div className="work-panel-head"><h2 id="work-projects-title">Projects</h2><span>{engagements.length}</span></div>
          <div className="work-project-list">{engagements.filter((project) => project.status !== "archived").map((project) => {
            const projectItems = items.filter((item) => item.engagement_id === project.id);
            return <Link key={project.id} to={projectSurface(project.id, "work")}><span><strong>{project.name}</strong><small>{project.workEnabled ? "Agent tools on" : "Agent tools off"}</small></span><span>{projectItems.filter((item) => item.status === "in_progress").length} active · {projectItems.filter((item) => item.status === "blocked").length} blocked</span><ArrowRight size={16} /></Link>;
          })}</div>
        </section>
        <section className="work-panel" aria-labelledby="work-recent-title"><div className="work-panel-head"><h2 id="work-recent-title">Recent check-ins</h2><span>{recent.length}</span></div>
          {recent.slice(0, 12).map((update) => { const item = items.find((candidate) => candidate.id === update.item_id); return <Link className="work-update-preview" key={update.id} to={projectSurface(update.engagement_id, "work", update.item_id)}><span><strong>{item?.title ?? "Work item"}</strong><small>{projectName(update.engagement_id)} · {time(update.created_at)}</small></span><p>{update.summary}</p></Link>; })}
          {!recent.length && <p className="work-empty-inline">No check-ins yet. Agents can post one after you enable their project tools.</p>}
        </section>
      </> : <>
        <div className={`work-project-layout${itemId ? " has-detail" : ""}`}>
          <section className="work-board" aria-label="Project board">
            {columns.map((column) => <section className="work-column" key={column.id} aria-labelledby={`work-column-${column.id}`}>
              <header><h2 id={`work-column-${column.id}`}>{column.label}</h2><span>{counts[column.id]}</span></header>
              <div className="work-column-items">{items.filter((item) => item.status === column.id).map((item) => <Link key={item.id} className={`work-card${item.id === itemId ? " selected" : ""}`} to={projectSurface(projectId, "work", item.id)}>
                <span className={`work-priority ${item.priority}`}>{item.priority}</span><strong>{item.title}</strong>
                <small>{assignee(item)}{item.assignee_session_id && activity[item.assignee_session_id] === "working" ? " · Working" : ""}</small>
                <span className="work-card-time"><Clock3 size={13} /> {time(item.last_update_at)}</span>
              </Link>)}{!counts[column.id] && <p className="work-column-empty">No items</p>}</div>
            </section>)}
          </section>
          {itemId && <aside className="work-detail" aria-labelledby="work-detail-title">
            <div className="work-detail-top"><span>Work item</span><Link title="Close item" aria-label="Close item" to={projectSurface(projectId, "work")}><X size={17} /></Link></div>
            {selectedItem ? <>
              <h2 id="work-detail-title">{selectedItem.title}</h2><div className="work-detail-meta"><span className={`work-status ${selectedItem.status}`}>{label(selectedItem.status)}</span><span>{selectedItem.priority} priority</span></div>
              {selectedItem.description && <p className="work-description">{selectedItem.description}</p>}
              <dl className="work-fields"><div><dt>Assigned to</dt><dd>{selectedItem.assignee_session_id ? <Link to={`${projectSurface(projectId, "workbench")}?session=${encodeURIComponent(selectedItem.assignee_session_id)}`}>{assignee(selectedItem)}</Link> : "Unassigned"}</dd></div><div><dt>Last update</dt><dd>{time(selectedItem.last_update_at)}</dd></div></dl>
              <h3>Check-ins</h3><ol className="work-timeline">{itemUpdates.map((update) => <li key={update.id}><span className="work-timeline-dot" /><div><div className="work-timeline-top"><strong>{update.actor_kind === "agent" ? "Agent" : update.actor_kind === "import" ? "Imported" : "Operator"}</strong><time dateTime={update.created_at}>{time(update.created_at)}</time></div><p>{update.summary}</p>{update.next_step && <small>Next: {update.next_step}</small>}{update.blocker && <small className="work-blocker">Blocked: {update.blocker}</small>}{update.source_session_id && <Link to={`${projectSurface(projectId, "workbench")}?session=${encodeURIComponent(update.source_session_id)}`}>Open conversation <ArrowRight size={13} /></Link>}</div></li>)}{!itemUpdates.length && <li className="work-no-updates">No check-ins yet.</li>}</ol>
              <form className="work-checkin-form" onSubmit={(event) => void checkIn(event)}><h3>Post an update</h3><label>Progress<textarea required maxLength={4000} value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="What changed?" /></label><label>Next step<input maxLength={2000} value={nextStep} onChange={(event) => setNextStep(event.target.value)} /></label><label>Blocker<input maxLength={2000} value={blocker} onChange={(event) => setBlocker(event.target.value)} /></label><label>Status<select value={nextStatus} onChange={(event) => setNextStatus(event.target.value as Status)}>{columns.map((column) => <option key={column.id} value={column.id}>{column.label}</option>)}</select></label><button className="button primary" type="submit" disabled={busy || !summary.trim()}>Save update</button></form>
            </> : <div role="alert" className="work-empty-inline">This item is unavailable. <Link to={projectSurface(projectId, "work")}>Return to board</Link></div>}
          </aside>}
        </div>
      </>}
    </>}
    {showCreate && <div className="work-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setShowCreate(false); }}><form className="work-dialog" role="dialog" aria-modal="true" aria-labelledby="work-create-title" onSubmit={(event) => void create(event)}><header><h2 id="work-create-title">New work item</h2><button className="work-dialog-close" type="button" aria-label="Close" disabled={busy} onClick={() => setShowCreate(false)}><X size={18} /></button></header>{!projectId && <label>Project<select required value={createProjectId} onChange={(event) => setCreateProjectId(event.target.value)}><option value="">Choose a project</option>{engagements.filter((project) => project.status !== "archived").map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>}<label>Title<input autoFocus required maxLength={300} value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>Description<textarea maxLength={20000} value={description} onChange={(event) => setDescription(event.target.value)} /></label><footer><button type="button" className="button quiet" disabled={busy} onClick={() => setShowCreate(false)}>Cancel</button><button type="submit" className="button primary" disabled={busy || !title.trim()}><ListTodo size={16} /> Create item</button></footer></form></div>}
  </div>;
}
