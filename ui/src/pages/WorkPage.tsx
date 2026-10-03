import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, ChevronDown, ChevronRight, CircleAlert, Clock3, ListTodo, Plus, RefreshCw, X } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import type { ChatSessionActivity } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { projectSurface, resourcePath } from "../resourceRoutes";
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
type WorkAgentActivity = {
  session_id: string; engagement_id: string; title: string;
  state: "working" | "waiting"; turn_id: string;
};

const columns: { id: Status; label: string }[] = [
  { id: "backlog", label: "Backlog" }, { id: "ready", label: "Ready" },
  { id: "in_progress", label: "In progress" }, { id: "blocked", label: "Blocked" },
  { id: "review", label: "Review" }, { id: "done", label: "Done" },
];
const label = (status: Status) => columns.find((column) => column.id === status)?.label ?? status;
const time = (value: string | null | undefined) => value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "No update yet";
const errorText = (value: unknown) => value instanceof Error ? value.message : "Core could not load Work. Try again.";
const workPageSize = 500;
const visibleProjectLimit = 80;

export function WorkPage() {
  const { api, engagement, engagements, coreState, retryResource } = useWorkspace();
  const { projectId, itemId } = useParams();
  const navigate = useNavigate();
  const [items, setItems] = useState<WorkItem[]>([]);
  const [recent, setRecent] = useState<WorkUpdate[]>([]);
  const [itemUpdates, setItemUpdates] = useState<WorkUpdate[]>([]);
  const [sessionTitles, setSessionTitles] = useState<Record<string, string>>({});
  const [activity, setActivity] = useState<Record<string, ChatSessionActivity["state"]>>({});
  const [activityProjects, setActivityProjects] = useState<Record<string, string>>({});
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [liveState, setLiveState] = useState<"connecting" | "live" | "reconnecting" | "offline">("connecting");
  const [showCreate, setShowCreate] = useState(false);
  const [projectQuery, setProjectQuery] = useState("");
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(() => new Set());
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [createProjectId, setCreateProjectId] = useState(engagement?.id ?? "");
  const [summary, setSummary] = useState("");
  const [nextStep, setNextStep] = useState("");
  const [blocker, setBlocker] = useState("");
  const [nextStatus, setNextStatus] = useState<Status>("in_progress");
  const selectedProject = engagements.find((project) => project.id === projectId);
  const activeProjects = useMemo(() => engagements.filter((project) => project.status !== "archived"), [engagements]);
  const projectById = useMemo(() => new Map(activeProjects.map((project) => [project.id, project])), [activeProjects]);
  const childrenByParent = useMemo(() => {
    const children = new Map<string, typeof activeProjects>();
    for (const project of activeProjects) {
      if (!project.parentEngagementId || !projectById.has(project.parentEngagementId)) continue;
      const siblings = children.get(project.parentEngagementId) ?? [];
      siblings.push(project);
      children.set(project.parentEngagementId, siblings);
    }
    return children;
  }, [activeProjects, projectById]);
  const directChildren = childrenByParent.get(projectId ?? "") ?? [];
  const selectedItem = items.find((item) => item.id === itemId);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!api) return;
    try {
      let loaded: WorkItem[];
      if (projectId) {
        loaded = await api.request<WorkItem[]>(`engagements/${encodeURIComponent(projectId)}/work`, { signal });
      } else {
        loaded = [];
        for (let offset = 0; ; offset += workPageSize) {
          const page = await api.request<WorkItem[]>(`work/items?offset=${offset}&limit=${workPageSize}`, { signal });
          loaded.push(...page);
          if (page.length < workPageSize) break;
        }
      }
      if (signal?.aborted) return;
      setItems(loaded);
      if (projectId && itemId) {
        const updates = await api.request<WorkUpdate[]>(`engagements/${encodeURIComponent(projectId)}/work/${encodeURIComponent(itemId)}/updates`, { signal });
        if (signal?.aborted) return;
        setItemUpdates(updates);
      } else {
        setItemUpdates([]);
      }
      if (projectId) {
        const [sessionPage, active] = await Promise.all([
          api.listChatSessions(projectId, signal), api.listChatSessionActivity(projectId, signal),
        ]);
        if (signal?.aborted) return;
        setSessionTitles(Object.fromEntries(sessionPage.items.map((session) => [session.id, session.title])));
        setActivity(Object.fromEntries(active.map((session) => [session.sessionId, session.state])));
        setActivityProjects(Object.fromEntries(active.map((session) => [session.sessionId, projectId])));
      } else {
        const updates = await api.request<WorkUpdate[]>("work/updates", { signal });
        if (signal?.aborted) return;
        setRecent(updates);
        const agents = await api.request<WorkAgentActivity[]>("work/agents", { signal });
        if (signal?.aborted) return;
        setActivity(Object.fromEntries(agents.map((agent) => [agent.session_id, agent.state])));
        setActivityProjects(Object.fromEntries(agents.map((agent) => [agent.session_id, agent.engagement_id])));
        setSessionTitles(Object.fromEntries(agents.map((agent) => [agent.session_id, agent.title])));
      }
      setError(undefined);
    } catch (failure) {
      if (!signal?.aborted) {
        void logCaughtDiagnostic("interface.work_page.refresh_failed", "Work data could not be loaded.", failure, "work_page");
        setError(errorText(failure));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, projectId, itemId]);

  useEffect(() => { setEnabled(selectedProject?.workEnabled ?? false); }, [projectId, selectedProject?.workEnabled]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!api || coreState !== "online") { setLiveState("offline"); return; }
    setLiveState("connecting");
    const controller = new AbortController();
    let pending = false;
    let projectsPending = false;
    let refreshing = false;
    const reload = (kind: "work" | "projects") => {
      pending = true;
      projectsPending ||= kind === "projects";
      if (refreshing) return;
      refreshing = true;
      void (async () => {
        while (pending && !controller.signal.aborted) {
          const refreshProjects = projectsPending;
          pending = false;
          projectsPending = false;
          try {
            if (refreshProjects) await retryResource("projects");
            await refresh(controller.signal);
          } catch (failure) {
            if (!controller.signal.aborted) {
              void logCaughtDiagnostic("interface.work_page.live_refresh_failed", "Live Work data could not be loaded.", failure, "work_page");
              setError(errorText(failure));
            }
          }
        }
        refreshing = false;
      })();
    };
    void (async () => {
      while (!controller.signal.aborted) {
        try {
          await api.watchWorkChanges(reload, () => {
            setLiveState("live");
            reload("work");
          }, controller.signal);
        } catch (failure) {
          if (!controller.signal.aborted) {
            void logCaughtDiagnostic("interface.work_page.live_stream_failed", "Work live updates disconnected.", failure, "work_page");
          }
        }
        if (controller.signal.aborted) break;
        setLiveState("reconnecting");
        await new Promise<void>((resolve) => {
          const timeout = window.setTimeout(resolve, 1_000);
          controller.signal.addEventListener("abort", () => { window.clearTimeout(timeout); resolve(); }, { once: true });
        });
      }
    })();
    return () => controller.abort();
  }, [api, coreState, refresh, retryResource]);

  useEffect(() => { if (selectedItem) setNextStatus(selectedItem.status); }, [selectedItem?.id, selectedItem?.status]);
  useEffect(() => { if (engagement?.id && !createProjectId) setCreateProjectId(engagement.id); }, [engagement?.id, createProjectId]);

  const counts = useMemo(() => Object.fromEntries(columns.map((column) => [column.id, items.filter((item) => item.status === column.id).length])) as Record<Status, number>, [items]);
  const projectRows = useMemo(() => {
    const query = projectQuery.trim().toLocaleLowerCase();
    const matching = new Set(activeProjects.filter((project) => !query || project.name.toLocaleLowerCase().includes(query)).map((project) => project.id));
    const included = new Set(matching);
    for (const project of activeProjects) {
      if (!matching.has(project.id)) continue;
      let ancestorId = project.parentEngagementId;
      const seen = new Set<string>();
      while (ancestorId && !seen.has(ancestorId) && projectById.has(ancestorId)) {
        included.add(ancestorId);
        seen.add(ancestorId);
        ancestorId = projectById.get(ancestorId)?.parentEngagementId;
      }
    }
    const rows: { project: typeof activeProjects[number]; depth: number }[] = [];
    const visited = new Set<string>();
    const visit = (project: typeof activeProjects[number], depth: number) => {
      if (visited.has(project.id) || !included.has(project.id)) return;
      visited.add(project.id);
      rows.push({ project, depth });
      if (query || expandedProjects.has(project.id)) {
        for (const child of childrenByParent.get(project.id) ?? []) visit(child, depth + 1);
      }
    };
    for (const project of activeProjects) {
      if (!project.parentEngagementId || !projectById.has(project.parentEngagementId)) visit(project, 0);
    }
    return { rows, count: query ? matching.size : activeProjects.length };
  }, [activeProjects, childrenByParent, expandedProjects, projectById, projectQuery]);
  const visibleProjects = projectRows.rows.slice(0, visibleProjectLimit);
  const projectCounts = useMemo(() => {
    const byProject = new Map<string, { active: number; blocked: number }>();
    for (const item of items) {
      let projectId: string | undefined = item.engagement_id;
      const seen = new Set<string>();
      while (projectId && !seen.has(projectId)) {
        seen.add(projectId);
        const counts = byProject.get(projectId) ?? { active: 0, blocked: 0 };
        if (item.status === "in_progress") counts.active += 1;
        if (item.status === "blocked") counts.blocked += 1;
        byProject.set(projectId, counts);
        projectId = projectById.get(projectId)?.parentEngagementId;
      }
    }
    return byProject;
  }, [items, projectById]);
  const working = useMemo(() => items.filter((item) => item.assignee_session_id && activity[item.assignee_session_id] === "working" && item.status !== "done"), [items, activity]);
  const workingAgents = useMemo(() => Object.entries(activity).filter(([, state]) => state === "working").map(([sessionId]) => ({
    sessionId,
    projectId: activityProjects[sessionId],
    title: sessionTitles[sessionId] || "Active conversation",
    item: working.find((candidate) => candidate.assignee_session_id === sessionId),
  })).filter((entry) => Boolean(entry.projectId)), [activity, activityProjects, sessionTitles, working]);
  const projectName = (id: string) => engagements.find((project) => project.id === id)?.name ?? "Project unavailable";
  const assignee = (item: WorkItem) => item.assignee_session_id ? sessionTitles[item.assignee_session_id] ?? "Agent session" : "Unassigned";

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
    } catch (failure) {
      void logCaughtDiagnostic("interface.work_page.create_failed", "A Work task could not be created.", failure, "work_page");
      setError(errorText(failure));
    }
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
    } catch (failure) {
      void logCaughtDiagnostic("interface.work_page.check_in_failed", "A Work update could not be saved.", failure, "work_page");
      setError(errorText(failure));
    }
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
    } catch (failure) {
      void logCaughtDiagnostic("interface.work_page.setting_failed", "Work agent access could not be changed.", failure, "work_page");
      setError(errorText(failure));
    }
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
        <span className={`work-live-state ${liveState}`} role="status">{liveState === "live" ? "Live" : liveState === "connecting" ? "Connecting…" : liveState === "offline" ? "Offline" : "Reconnecting…"}</span>
        <button className="button quiet work-refresh" type="button" aria-label="Refresh Work" title="Refresh Work" onClick={() => void refresh()}><RefreshCw size={16} /></button>
        {projectId && <button className="button quiet" type="button" disabled={busy || coreState !== "online"} onClick={() => void toggle()}>{enabled ? "Disable agent tools" : "Enable agent tools"}</button>}
        <button className="button primary" type="button" disabled={!api || coreState !== "online"} onClick={() => setShowCreate(true)}><Plus size={16} /> New item</button>
      </div>
    </header>
    {projectId && <p className="work-setting-note">{enabled ? "Agents can update this project through Nebula's built-in MCP. New updates appear live." : "Agent tools are off. Existing Work records remain visible. Enable them when agents should post updates."}</p>}
    {projectId && selectedProject?.parentEngagementId && projectById.has(selectedProject.parentEngagementId) && <Link className="work-parent-link" to={projectSurface(selectedProject.parentEngagementId, "work")}><ArrowLeft size={14} /> {projectById.get(selectedProject.parentEngagementId)?.name}</Link>}
    {error && <div className="work-error" role="alert"><CircleAlert size={17} /><span>{error}</span><button type="button" className="button quiet" onClick={() => void refresh()}>Retry</button></div>}
    {loading ? <div className="work-loading" role="status">Loading Work…</div> : <>
      <section className="work-summary" aria-label="Work summary">
        <div><strong>{workingAgents.length}</strong><span>Working now</span></div>
        <div><strong>{counts.in_progress}</strong><span>In progress</span></div>
        <div><strong>{counts.blocked}</strong><span>Blocked</span></div>
        <div><strong>{counts.review}</strong><span>In review</span></div>
      </section>
      {!projectId ? <>
        <div className="work-overview-grid">
          <section className="work-panel" aria-labelledby="work-active-title">
            <div className="work-panel-head"><h2 id="work-active-title">Agents working now</h2><span>{workingAgents.length}</span></div>
            {workingAgents.length ? <ul className="work-activity-list">{workingAgents.map((agent) => <li key={agent.sessionId}>
              <span className="work-live-dot" aria-hidden="true" /><div><strong>{agent.item?.title ?? agent.title}</strong><small>{projectName(agent.projectId)} · {agent.item ? time(agent.item.last_update_at) : "No Work item linked"}</small></div>
              <Link aria-label={`Open ${agent.item?.title ?? agent.title}`} to={agent.item ? projectSurface(agent.projectId, "work", agent.item.id) : resourcePath(agent.projectId, "conversation", agent.sessionId)}><ArrowRight size={16} /></Link>
            </li>)}</ul> : <p className="work-empty-inline">No agents are working right now.</p>}
          </section>
          <section className="work-panel" aria-labelledby="work-attention-title">
            <div className="work-panel-head"><h2 id="work-attention-title">Needs attention</h2><span>{counts.blocked}</span></div>
            {items.filter((item) => item.status === "blocked").slice(0, 8).map((item) => <Link className="work-attention-row" key={item.id} to={projectSurface(item.engagement_id, "work", item.id)}><CircleAlert size={16} /><span><strong>{item.title}</strong><small>{projectName(item.engagement_id)} · Blocked</small></span><ArrowRight size={15} /></Link>)}
            {!counts.blocked && <p className="work-empty-inline">No blocked items.</p>}
          </section>
        </div>
        <section className="work-panel work-projects" aria-labelledby="work-projects-title"><div className="work-panel-head"><h2 id="work-projects-title">Projects</h2><span>{projectRows.count}</span></div>
          <div className="work-project-search"><input type="search" aria-label="Search projects" placeholder="Search projects" value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} /></div>
          <div className="work-project-list">{visibleProjects.map(({ project, depth }) => {
            const projectItems = projectCounts.get(project.id) ?? { active: 0, blocked: 0 };
            const childCount = childrenByParent.get(project.id)?.length ?? 0;
            const expanded = Boolean(projectQuery.trim()) || expandedProjects.has(project.id);
            return <div className={`work-project-row${depth ? " is-child" : ""}`} key={project.id} style={{ paddingLeft: Math.min(depth, 5) * 22 }}>
              {childCount > 0 ? <button type="button" aria-label={`${expanded ? "Hide" : "Show"} subprojects of ${project.name}`} aria-expanded={expanded} onClick={() => setExpandedProjects((current) => { const next = new Set(current); if (next.has(project.id)) next.delete(project.id); else next.add(project.id); return next; })}>{expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</button> : <span className="work-project-spacer" />}
              <Link to={projectSurface(project.id, "work")}><span><strong>{project.name}</strong><small>{childCount ? `${childCount} subprojects · ` : ""}{project.workEnabled ? "Agent tools on" : "Agent tools off"}</small></span><span>{projectItems.active} active · {projectItems.blocked} blocked</span><ArrowRight size={16} /></Link>
            </div>;
          })}</div>
          {projectRows.rows.length > visibleProjectLimit && <p className="work-empty-inline">Showing {visibleProjectLimit} rows. Search to narrow the list.</p>}
          {!projectRows.count && <p className="work-empty-inline">No projects match your search.</p>}
        </section>
        <section className="work-panel" aria-labelledby="work-recent-title"><div className="work-panel-head"><h2 id="work-recent-title">Recent check-ins</h2><span>{recent.length}</span></div>
          {recent.slice(0, 12).map((update) => { const item = items.find((candidate) => candidate.id === update.item_id); return <Link className="work-update-preview" key={update.id} to={projectSurface(update.engagement_id, "work", update.item_id)}><span><strong>{item?.title ?? "Work item"}</strong><small>{projectName(update.engagement_id)} · {time(update.created_at)}</small></span><p>{update.summary}</p></Link>; })}
          {!recent.length && <p className="work-empty-inline">No check-ins yet. Agents can post updates from their project tools.</p>}
        </section>
      </> : <>
        {directChildren.length > 0 && <section className="work-panel work-subprojects" aria-labelledby="work-subprojects-title"><div className="work-panel-head"><h2 id="work-subprojects-title">Subprojects</h2><span>{directChildren.length}</span></div><div className="work-project-search"><input type="search" aria-label="Search subprojects" placeholder="Search subprojects" value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} /></div><div className="work-project-list">{directChildren.filter((child) => child.name.toLocaleLowerCase().includes(projectQuery.trim().toLocaleLowerCase())).slice(0, visibleProjectLimit).map((child) => <div className="work-project-row" key={child.id}><span className="work-project-spacer" /><Link to={projectSurface(child.id, "work")}><span><strong>{child.name}</strong><small>{child.workEnabled ? "Agent tools on" : "Agent tools off"}</small></span><ArrowRight size={16} /></Link></div>)}</div>{directChildren.length > visibleProjectLimit && <p className="work-empty-inline">Showing up to {visibleProjectLimit} matches. Search to narrow the list.</p>}</section>}
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
              <dl className="work-fields"><div><dt>Assigned to</dt><dd>{selectedItem.assignee_session_id ? <Link to={resourcePath(projectId, "conversation", selectedItem.assignee_session_id)}>{assignee(selectedItem)}</Link> : "Unassigned"}</dd></div><div><dt>Last update</dt><dd>{time(selectedItem.last_update_at)}</dd></div></dl>
              <h3>Check-ins</h3><ol className="work-timeline">{itemUpdates.map((update) => <li key={update.id}><span className="work-timeline-dot" /><div><div className="work-timeline-top"><strong>{update.actor_kind === "agent" ? "Agent" : update.actor_kind === "import" ? "Imported" : "Operator"}</strong><time dateTime={update.created_at}>{time(update.created_at)}</time></div><p>{update.summary}</p>{update.next_step && <small>Next: {update.next_step}</small>}{update.blocker && <small className="work-blocker">Blocked: {update.blocker}</small>}{update.source_session_id && <Link to={resourcePath(projectId, "conversation", update.source_session_id)}>Open conversation <ArrowRight size={13} /></Link>}</div></li>)}{!itemUpdates.length && <li className="work-no-updates">No check-ins yet.</li>}</ol>
              <form className="work-checkin-form" onSubmit={(event) => void checkIn(event)}><h3>Post an update</h3><label>Progress<textarea required maxLength={4000} value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="What changed?" /></label><label>Next step<input maxLength={2000} value={nextStep} onChange={(event) => setNextStep(event.target.value)} /></label><label>Blocker<input maxLength={2000} value={blocker} onChange={(event) => setBlocker(event.target.value)} /></label><label>Status<select value={nextStatus} onChange={(event) => setNextStatus(event.target.value as Status)}>{columns.map((column) => <option key={column.id} value={column.id}>{column.label}</option>)}</select></label><button className="button primary" type="submit" disabled={busy || !summary.trim()}>Save update</button></form>
            </> : <div role="alert" className="work-empty-inline">This item is unavailable. <Link to={projectSurface(projectId, "work")}>Return to board</Link></div>}
          </aside>}
        </div>
      </>}
    </>}
    {showCreate && <div className="work-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setShowCreate(false); }}><form className="work-dialog" role="dialog" aria-modal="true" aria-labelledby="work-create-title" onSubmit={(event) => void create(event)}><header><h2 id="work-create-title">New work item</h2><button className="work-dialog-close" type="button" aria-label="Close" disabled={busy} onClick={() => setShowCreate(false)}><X size={18} /></button></header>{!projectId && <label>Project<select required value={createProjectId} onChange={(event) => setCreateProjectId(event.target.value)}><option value="">Choose a project</option>{engagements.filter((project) => project.status !== "archived").map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>}<label>Title<input autoFocus required maxLength={300} value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>Description<textarea maxLength={20000} value={description} onChange={(event) => setDescription(event.target.value)} /></label><footer><button type="button" className="button quiet" disabled={busy} onClick={() => setShowCreate(false)}>Cancel</button><button type="submit" className="button primary" disabled={busy || !title.trim()}><ListTodo size={16} /> Create item</button></footer></form></div>}
  </div>;
}
