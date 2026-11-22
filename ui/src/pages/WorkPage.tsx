import { Fragment, useCallback, useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, ChevronDown, ChevronRight, CircleAlert, Clock3, Columns3, ListTodo, LoaderCircle, Plus, RefreshCw, X } from "lucide-react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { readWhenProjectReady } from "../api/projectRuntime";
import type { ChatSessionActivity, WorkStage } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { projectRoot, projectSurface, resourcePath } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";
import { useOptionalChrome } from "../state/ChromeContext";
import { WorkStageEditor } from "./WorkStageEditor";
import { effectiveStages, hasStageGap, stageCells, stageColumns, stageRollup, type StageCell, type StageRollupCell } from "./workStages";
import "./WorkPage.css";

type Status = "backlog" | "ready" | "in_progress" | "blocked" | "review" | "done";
type Priority = "low" | "normal" | "high" | "urgent";
type WorkItem = {
  id: string; engagement_id: string; title: string; description: string;
  status: Status; priority: Priority; assignee_session_id: string | null;
  source_kind: "chat" | "mission" | "manual" | "import"; source_id: string | null;
  stage?: string | null;
  created_at: string; updated_at: string; last_update_at: string | null; revision: number;
};
type WorkUpdate = {
  id: string; engagement_id: string; item_id: string; summary: string;
  next_step: string | null; blocker: string | null; status: Status;
  actor_kind: "operator" | "agent" | "import"; actor_id: string;
  source_session_id: string | null; source_engagement_id: string | null; source_turn_id: string | null;
  source_run_id: string | null; created_at: string;
};
type WorkAgentActivity = {
  session_id: string; engagement_id: string; title: string;
  state: "working" | "waiting"; turn_id: string;
};
type SummaryView = "working" | "waiting" | "in_progress" | "blocked" | "review";

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
const trackRequestLimit = 50;

function WorkUpdateDetails({ update, projectId }: { update: WorkUpdate; projectId: string }) {
  return <>
    <p>{update.summary}</p>
    {update.next_step && <small>Next: {update.next_step}</small>}
    {update.blocker && <small className="work-blocker">Blocked: {update.blocker}</small>}
    {update.source_session_id && <Link to={resourcePath(update.source_engagement_id ?? projectId, "conversation", update.source_session_id)}>Open conversation <ArrowRight size={13} /></Link>}
  </>;
}

type ProjectStatusCountValues = { working: number; waiting: number; inProgress: number; blocked: number; review: number };
const PROJECT_STATUS_LABELS = [["working", "working"], ["waiting", "waiting"], ["inProgress", "in progress"], ["blocked", "blocked"], ["review", "in review"]] as const;

/** Rows list only the counts that are non-zero; a project with no active work reads as idle. */
function ProjectStatusCounts({ counts }: { counts: ProjectStatusCountValues }) {
  const active = PROJECT_STATUS_LABELS.filter(([key]) => counts[key] > 0);
  if (!active.length) return <span className="work-project-status idle">Idle</span>;
  return <span className="work-project-status">{active.map(([key, label], index) => <Fragment key={key}>{index > 0 && <span className="sr-only">, </span>}<span className={`work-project-count ${key}`}>{counts[key]} {label}</span></Fragment>)}</span>;
}

/** One project's stage cells on All projects; each opens that stage on the project's board. */
function ProjectStageStrip({ projectId, projectName, cells }: { projectId: string; projectName: string; cells: StageCell[] }) {
  return <ol className="work-stage-strip" aria-label={`${projectName} stages`}>{cells.map((cell) => <li key={cell.key}>
    <Link className="work-stage-cell" data-state={cell.state} to={`${projectSurface(projectId, "work")}?group=stage&stage=${encodeURIComponent(cell.key)}`} aria-label={`${cell.label}: ${cell.state === "gap" ? "none yet" : `${cell.open} open`}${cell.detail && cell.state !== "gap" ? `, ${cell.detail}` : ""}`}>
      <span className="work-stage-cell-name"><i aria-hidden="true" />{cell.label}</span>
      <strong>{cell.state === "gap" ? "None yet" : cell.state === "empty" ? "—" : cell.open.toLocaleString()}</strong>
      <small>{cell.state === "gap" ? "" : cell.detail}</small>
    </Link>
  </li>)}</ol>;
}

/** A parent's stages counted across its subprojects; each cell lists the subprojects that reached that stage. */
function SubprojectStageRollup({ cells, href, selected }: { cells: StageRollupCell[]; href: (key: string) => string; selected: string | null }) {
  return <ol className="work-stage-strip" aria-label="Subprojects by stage">{cells.map((cell) => <li key={cell.key}>
    <Link className="work-stage-cell" data-state={cell.state} aria-current={selected === cell.key ? "true" : undefined} to={href(cell.key)} aria-label={`${cell.label}: ${cell.reached.size.toLocaleString()} of ${cell.total.toLocaleString()} subprojects${cell.detail ? `, ${cell.detail}` : ""}`}>
      <span className="work-stage-cell-name"><i aria-hidden="true" />{cell.label}</span>
      <strong>{cell.state === "gap" ? "None yet" : cell.reached.size.toLocaleString()}</strong>
      <small>{cell.reached.size ? `of ${cell.total.toLocaleString()} · ${cell.detail}` : ""}</small>
    </Link>
  </li>)}</ol>;
}

export function projectInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? "").slice(0, 2);
  return letters.toLocaleUpperCase();
}

export function WorkPage() {
  const { api, engagement, engagements, coreState, retryResource } = useWorkspace();
  const chrome = useOptionalChrome();
  const { projectId, itemId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [items, setItems] = useState<WorkItem[]>([]);
  // Every project's items, read only on a parent's page to count its subprojects by stage.
  const [familyItems, setFamilyItems] = useState<WorkItem[]>([]);
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
  const [trackingAgent, setTrackingAgent] = useState<{ sessionId: string; projectId: string; title: string } | null>(null);
  const [trackItemId, setTrackItemId] = useState("");
  const [trackTitle, setTrackTitle] = useState("");
  const [trackSearch, setTrackSearch] = useState("");
  const [trackError, setTrackError] = useState<string>();
  const [projectQuery, setProjectQuery] = useState("");
  const [summaryView, setSummaryView] = useState<SummaryView | null>(projectId ? "working" : null);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(() => new Set());
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [createProjectId, setCreateProjectId] = useState(engagement?.id ?? "");
  const [createStage, setCreateStage] = useState("");
  const [stageFilter, setStageFilter] = useState<"all" | "gap" | "blocked">("all");
  const [summary, setSummary] = useState("");
  const [nextStep, setNextStep] = useState("");
  const [blocker, setBlocker] = useState("");
  // The status the operator picked, for the item it was picked on; a live update to that item does not replace it.
  const [statusChoice, setStatusChoice] = useState<{ itemId: string; status: Status }>();
  const blockerHintId = useId();
  // Core saves any check-in with a blocker as Blocked, so the form shows that status instead of the operator's choice.
  const blocked = blocker.trim() !== "";
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
  const hasChildren = directChildren.length > 0;
  // The URL owns which grouping and stage are shown, so a stage link and reload land on the same view.
  // A subproject without its own stages uses its nearest parent's.
  const { stages: projectStages, from: stagesFrom } = effectiveStages(selectedProject, projectById);
  const groupByStage = searchParams.get("group") === "stage";
  const focusedStage = searchParams.get("stage");
  const editingStages = Boolean(projectId) && searchParams.get("stages") === "edit";
  const anyStages = activeProjects.some((project) => (project.workStages ?? []).length > 0);
  const projectsView: "stages" | "status" = anyStages && searchParams.get("view") !== "status" ? "stages" : "status";
  const subStageParam = searchParams.get("substage");
  useEffect(() => {
    if (subStageParam) document.getElementById("work-subprojects")?.scrollIntoView?.({ block: "nearest" });
  }, [subStageParam]);
  useEffect(() => {
    if (!groupByStage || !focusedStage || loading) return;
    document.getElementById(`work-stage-column-${focusedStage}`)?.closest("section")?.scrollIntoView({ block: "nearest", inline: "start" });
  }, [groupByStage, focusedStage, loading]);
  const setParam = (name: string, value: string | null) => setSearchParams((current) => {
    const next = new URLSearchParams(current);
    if (value === null) next.delete(name); else next.set(name, value);
    return next;
  }, { replace: true });
  const selectedItem = items.find((item) => item.id === itemId);
  const chosenStatus = selectedItem && statusChoice?.itemId === selectedItem.id ? statusChoice.status : undefined;
  const nextStatus: Status = chosenStatus ?? selectedItem?.status ?? "in_progress";
  const checkInStatus: Status = blocked ? "blocked" : nextStatus;
  const selectedUpdates = itemUpdates.filter((update) => update.item_id === itemId && update.engagement_id === projectId);
  const latestUpdate = selectedUpdates[0];

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!api) return;
    // A project's board reads its worker: while it starts, stay loading and read again when it answers.
    const projectRead = <T,>(read: () => Promise<T>) => readWhenProjectReady(api.projectRuntime, read, signal);
    // Core's shared listing of every project's items, read in pages.
    const readAllItems = async () => {
      const all: WorkItem[] = [];
      for (let offset = 0; ; offset += workPageSize) {
        const page = await api.request<WorkItem[]>(`work/items?offset=${offset}&limit=${workPageSize}`, { signal });
        all.push(...page);
        if (page.length < workPageSize) break;
      }
      return all;
    };
    try {
      let loaded: WorkItem[];
      if (projectId) {
        loaded = await projectRead(() => api.request<WorkItem[]>(`engagements/${encodeURIComponent(projectId)}/work`, { signal }));
      } else {
        loaded = await readAllItems();
      }
      if (signal?.aborted) return;
      setItems(loaded);
      // A parent's stage roll-up reads every project's items from Core's shared listing, which wakes no project worker.
      if (projectId && hasChildren) {
        const family = await readAllItems();
        if (signal?.aborted) return;
        setFamilyItems(family);
      } else {
        setFamilyItems([]);
      }
      if (projectId && itemId) {
        const updates = await projectRead(() => api.request<WorkUpdate[]>(`engagements/${encodeURIComponent(projectId)}/work/${encodeURIComponent(itemId)}/updates`, { signal }));
        if (signal?.aborted) return;
        setItemUpdates(updates);
      } else {
        setItemUpdates([]);
      }
      if (projectId) {
        const [sessionPage, active] = await projectRead(() => Promise.all([
          api.listChatSessions(projectId, signal), api.listChatSessionActivity(projectId, signal),
        ]));
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
  }, [api, projectId, itemId, hasChildren]);

  useEffect(() => { setEnabled(selectedProject?.workEnabled ?? false); }, [projectId, selectedProject?.workEnabled]);
  useEffect(() => { setSummaryView(projectId ? "working" : null); }, [projectId]);

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
  const projectCounts = useMemo(() => {
    const byProject = new Map<string, { working: number; waiting: number; inProgress: number; blocked: number; review: number; latest?: WorkItem }>();
    const get = (id: string) => {
      const existing = byProject.get(id);
      if (existing) return existing;
      const created = { working: 0, waiting: 0, inProgress: 0, blocked: 0, review: 0, latest: undefined as WorkItem | undefined };
      byProject.set(id, created);
      return created;
    };
    for (const item of items) {
      let projectId: string | undefined = item.engagement_id;
      const seen = new Set<string>();
      while (projectId && !seen.has(projectId)) {
        seen.add(projectId);
        const counts = get(projectId);
        if (item.status === "in_progress") counts.inProgress += 1;
        if (item.status === "blocked") counts.blocked += 1;
        if (item.status === "review") counts.review += 1;
        if (item.last_update_at && (!counts.latest?.last_update_at || item.last_update_at > counts.latest.last_update_at)) counts.latest = item;
        projectId = projectById.get(projectId)?.parentEngagementId;
      }
    }
    for (const [sessionId, state] of Object.entries(activity)) {
      if (state !== "working" && state !== "waiting") continue;
      let projectId: string | undefined = activityProjects[sessionId];
      const seen = new Set<string>();
      while (projectId && !seen.has(projectId)) {
        seen.add(projectId);
        get(projectId)[state] += 1;
        projectId = projectById.get(projectId)?.parentEngagementId;
      }
    }
    return byProject;
  }, [items, activity, activityProjects, projectById]);
  const itemsByProject = useMemo(() => {
    const byProject = new Map<string, WorkItem[]>();
    for (const item of items) byProject.set(item.engagement_id, [...(byProject.get(item.engagement_id) ?? []), item]);
    return byProject;
  }, [items]);
  // A parent with subprojects and no items of its own only holds the stages its subprojects use: it gets no cells.
  const cellsByProject = useMemo(() => new Map(activeProjects.flatMap((project) => {
    const { stages } = effectiveStages(project, projectById);
    const own = itemsByProject.get(project.id) ?? [];
    if (!stages.length || (!own.length && childrenByParent.has(project.id))) return [];
    return [[project.id, stageCells(stages, own)] as const];
  })), [activeProjects, projectById, itemsByProject, childrenByParent]);
  const gapCount = useMemo(() => [...cellsByProject.values()].filter(hasStageGap).length, [cellsByProject]);
  const blockedProjects = useMemo(() => new Set(items.filter((item) => item.status === "blocked").map((item) => item.engagement_id)), [items]);
  const visibleProjects = useMemo(() => {
    const roots = projectRows.rows.filter((row) => row.depth === 0).slice(0, visibleProjectLimit);
    const children = projectRows.rows.filter((row) => row.depth > 0).slice(0, visibleProjectLimit - roots.length);
    const visible = new Set([...roots, ...children].map((row) => row.project.id));
    return projectRows.rows.filter((row) => visible.has(row.project.id));
  }, [projectRows]);
  // A stage filter lists the matching projects themselves, outside the project tree.
  const listedProjects = useMemo(() => projectsView !== "stages" || stageFilter === "all" ? visibleProjects : activeProjects
    .filter((project) => stageFilter === "gap" ? hasStageGap(cellsByProject.get(project.id) ?? []) : blockedProjects.has(project.id))
    .filter((project) => project.name.toLocaleLowerCase().includes(projectQuery.trim().toLocaleLowerCase()))
    .slice(0, visibleProjectLimit)
    .map((project) => ({ project, depth: 0 })), [projectsView, stageFilter, visibleProjects, activeProjects, cellsByProject, blockedProjects, projectQuery]);
  const boardColumns = useMemo(() => stageColumns(projectStages, items), [projectStages, items]);
  // The URL owns the subproject stage filter: `substage` names the stage, `subreach=missing` lists those that have not reached it.
  const subStage = searchParams.get("substage");
  const subMissing = searchParams.get("subreach") === "missing";
  const rollup = useMemo(() => hasChildren && projectStages.length ? stageRollup(projectStages, directChildren.map((child) => child.id), familyItems) : [], [hasChildren, projectStages, directChildren, familyItems]);
  const rollupFilter = rollup.find((cell) => cell.key === subStage);
  const listedChildren = useMemo(() => directChildren
    .filter((child) => !rollupFilter || rollupFilter.reached.has(child.id) !== subMissing)
    .filter((child) => child.name.toLocaleLowerCase().includes(projectQuery.trim().toLocaleLowerCase())), [directChildren, rollupFilter, subMissing, projectQuery]);
  const subStageHref = (key: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (key) next.set("substage", key); else next.delete("substage");
    next.delete("subreach");
    const query = next.toString();
    return `${projectSurface(projectId ?? "", "work")}${query ? `?${query}` : ""}`;
  };
  const stageLabel = (key: string | null | undefined) => key ? projectStages.find((stage) => stage.key === key)?.label ?? key : "No stage";
  const createTarget = engagements.find((project) => project.id === (projectId ?? createProjectId));
  const createStages = effectiveStages(createTarget, projectById).stages;
  const activeItems = useMemo(() => items.filter((item) => item.status !== "done"), [items]);
  const agentEntries = useMemo(() => Object.entries(activity).filter(([, state]) => state === "working" || state === "waiting").map(([sessionId, state]) => ({
    sessionId,
    state,
    projectId: activityProjects[sessionId],
    title: sessionTitles[sessionId] || "Active conversation",
    item: activeItems.find((candidate) => candidate.engagement_id === activityProjects[sessionId] && (candidate.assignee_session_id === sessionId || (candidate.source_kind === "chat" && candidate.source_id === sessionId))),
  })).filter((entry): entry is typeof entry & { projectId: string } => Boolean(entry.projectId)), [activity, activityProjects, sessionTitles, activeItems]);
  const workingAgents = agentEntries.filter((agent) => agent.state === "working");
  const waitingAgents = agentEntries.filter((agent) => agent.state === "waiting");
  const projectName = (id: string) => engagements.find((project) => project.id === id)?.name ?? "Project unavailable";
  const assignee = (item: WorkItem) => item.assignee_session_id ? sessionTitles[item.assignee_session_id] ?? "Agent session" : "Unassigned";
  const summaryOptions: { id: SummaryView; label: string; count: number }[] = [
    { id: "working", label: "Working now", count: workingAgents.length },
    { id: "waiting", label: "Waiting", count: waitingAgents.length },
    { id: "in_progress", label: "In progress", count: counts.in_progress },
    { id: "blocked", label: "Blocked", count: counts.blocked },
    { id: "review", label: "In review", count: counts.review },
  ];
  const selectedSummary = summaryOptions.find((option) => option.id === summaryView);
  const summaryItems = summaryView && summaryView !== "working" ? items.filter((item) => item.status === summaryView) : [];
  const availableTrackItems = trackingAgent ? items.filter((item) => item.engagement_id === trackingAgent.projectId && item.status !== "done" && !item.assignee_session_id) : [];
  const visibleTrackItems = availableTrackItems.filter((item) => item.id === trackItemId || item.title.toLocaleLowerCase().includes(trackSearch.trim().toLocaleLowerCase())).sort((a, b) => Number(b.id === trackItemId) - Number(a.id === trackItemId)).slice(0, 30);

  const openTracking = (agent: { sessionId: string; projectId: string; title: string }) => {
    setTrackingAgent(agent);
    setTrackItemId(""); setTrackTitle(agent.title); setTrackSearch(""); setTrackError(undefined);
  };

  const trackWork = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !trackingAgent || (!trackItemId && !trackTitle.trim())) return;
    setBusy(true); setTrackError(undefined);
    const target = `engagements/${encodeURIComponent(trackingAgent.projectId)}/work`;
    try {
      if (trackItemId) {
        // Link at the revision just read, so a conversation that claims the item in between is never overwritten.
        let linked = false;
        for (let attempt = 0; attempt < 2 && !linked; attempt += 1) {
          const current = await api.request<WorkItem>(`${target}/${encodeURIComponent(trackItemId)}`);
          if (current.assignee_session_id === trackingAgent.sessionId) { linked = true; break; }
          if (current.assignee_session_id) throw new Error("This item was assigned to another conversation. Refresh Work and choose another item.");
          if (current.status === "done") throw new Error("This item was marked Done. Choose another item.");
          try {
            await api.request<WorkItem>(`${target}/${encodeURIComponent(trackItemId)}`, {
              method: "PATCH", body: JSON.stringify({ assignee_session_id: trackingAgent.sessionId, expected_revision: current.revision }),
            });
            linked = true;
          } catch (failure) {
            // 409: the item changed after the read; read it again once.
            if (!(failure instanceof ApiError && failure.status === 409)) throw failure;
          }
        }
        if (!linked) throw new Error("This item kept changing while it was being linked. Refresh Work and try again.");
      } else {
        // Core returns a request ID's first item even after it is Done, so each Done item names the next request.
        // Repeated clicks and other clients reach the same open item; a finished item is never reused.
        let requestId = `track:${trackingAgent.sessionId}`;
        let tracked: WorkItem | undefined;
        for (let step = 0; step < trackRequestLimit && (!tracked || tracked.status === "done"); step += 1) {
          if (tracked) requestId = `track:${trackingAgent.sessionId}:after:${tracked.id}`;
          tracked = await api.request<WorkItem>(target, {
            method: "POST", body: JSON.stringify({ title: trackTitle.trim(), status: "in_progress", source_kind: "chat", source_id: trackingAgent.sessionId, assignee_session_id: trackingAgent.sessionId, request_id: requestId }),
          });
        }
        if (!tracked || tracked.status === "done") {
          throw new Error("This conversation has too many finished items to track another. Link an existing item instead.");
        }
      }
      await refresh();
      setTrackingAgent(null);
    } catch (failure) {
      void logCaughtDiagnostic("interface.work_page.track_failed", "A conversation could not be linked to Work.", failure, "work_page");
      setTrackError(errorText(failure));
    } finally { setBusy(false); }
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !title.trim()) return;
    const target = projectId ?? createProjectId;
    if (!target) { setError("Choose a project first."); return; }
    setBusy(true); setError(undefined);
    try {
      const item = await api.request<WorkItem>(`engagements/${encodeURIComponent(target)}/work`, {
        method: "POST", body: JSON.stringify({ title: title.trim(), description: description.trim(), status: "ready", ...(createStage && createStages.some((stage) => stage.key === createStage) ? { stage: createStage } : {}) }),
      });
      setTitle(""); setDescription(""); setCreateStage(""); setShowCreate(false);
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
        // An untouched status is left out (unless a blocker makes it Blocked), so Core keeps the item's latest status instead of this page's older copy.
        method: "POST", body: JSON.stringify({ summary: summary.trim(), next_step: nextStep.trim() || null, blocker: blocker.trim() || null, status: blocked ? "blocked" : chosenStatus }),
      });
      setSummary(""); setNextStep(""); setBlocker("");
      await refresh();
      setStatusChoice(undefined);
    } catch (failure) {
      void logCaughtDiagnostic("interface.work_page.check_in_failed", "A Work update could not be saved.", failure, "work_page");
      setError(errorText(failure));
    }
    finally { setBusy(false); }
  };

  const changeStage = async (stage: string) => {
    if (!api || !projectId || !selectedItem) return;
    setBusy(true); setError(undefined);
    try {
      await api.request<WorkItem>(`engagements/${encodeURIComponent(projectId)}/work/${encodeURIComponent(selectedItem.id)}`, {
        method: "PATCH", body: JSON.stringify({ stage: stage || null, expected_revision: selectedItem.revision }),
      });
      await refresh();
    } catch (failure) {
      void logCaughtDiagnostic("interface.work_page.stage_failed", "A Work item's stage could not be changed.", failure, "work_page");
      setError(failure instanceof ApiError && failure.status === 409 ? "This item changed. Its latest version is shown; choose the stage again." : errorText(failure));
      await refresh();
    } finally { setBusy(false); }
  };

  const saveStages = async (stages: WorkStage[], expectedRevision: number | undefined) => {
    if (!api || !projectId) return;
    await api.request(`engagements/${encodeURIComponent(projectId)}/work/stages`, {
      method: "PATCH",
      body: JSON.stringify({ stages: stages.map((stage) => ({ key: stage.key, label: stage.label, flag_when_empty: stage.flagWhenEmpty })), ...(expectedRevision ? { expected_revision: expectedRevision } : {}) }),
    });
    // The project list carries the stages, so reread it before closing.
    await retryResource("projects");
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
        {projectId && <Link className="work-back" to="/projects"><ArrowLeft size={14} /> All projects</Link>}
        <p className="work-eyebrow">{projectId ? selectedProject?.name ?? "Project" : "Projects"}</p>
        <h1>{projectId ? "Project work" : "All projects"}</h1>
        <p className="work-lede">{projectId ? "Plan tasks and follow agent check-ins in one place." : "See what agents are doing across your projects."}</p>
      </div>
      <div className="work-head-actions">
        <span className={`work-live-state ${liveState}`} role="status">{liveState === "live" ? "Live" : liveState === "connecting" ? "Connecting…" : liveState === "offline" ? "Offline" : "Reconnecting…"}</span>
        <button className="button quiet work-refresh" type="button" aria-label="Refresh Work" title="Refresh Work" onClick={() => void refresh()}><RefreshCw size={16} /></button>
        {projectId && <button className="button quiet work-agent-tools-switch" type="button" aria-label={enabled ? "Disable agent tools" : "Enable agent tools"} title={enabled ? "Agent tools are on" : "Agent tools are off"} data-state={enabled ? "on" : "off"} disabled={busy || coreState !== "online"} onClick={() => void toggle()}><span>Agent tools</span><span className="work-switch-track" aria-hidden="true" /></button>}
        <button className="button primary" type="button" disabled={!api || coreState !== "online" || (!projectId && !activeProjects.length)} onClick={() => setShowCreate(true)}><Plus size={16} /> New item</button>
      </div>
    </header>
    {projectId && <p className="work-setting-note">{enabled ? "Agents can update this project through Nebula's built-in MCP. New updates appear live." : "Agent tools are off. Existing Work records remain visible. Enable them when agents should post updates."}</p>}
    {projectId && selectedProject?.parentEngagementId && projectById.has(selectedProject.parentEngagementId) && <Link className="work-parent-link" to={projectSurface(selectedProject.parentEngagementId, "work")}><ArrowLeft size={14} /> {projectById.get(selectedProject.parentEngagementId)?.name}</Link>}
    {error && <div className="work-error" role="alert"><CircleAlert size={17} /><span>{error}</span><button type="button" className="button quiet" onClick={() => void refresh()}>Retry</button></div>}
    {loading ? <div className="work-loading" role="status">Loading Work…</div> : <>
      <section className="work-summary" aria-label="Work summary">
        {summaryOptions.map((option) => <button key={option.id} type="button" aria-label={`${option.count} ${option.label}`} aria-pressed={summaryView === option.id} aria-controls={summaryView ? "work-summary-detail" : undefined} data-empty={option.count === 0 ? "true" : undefined} onClick={() => setSummaryView(option.id)}><strong>{option.count}</strong><span>{option.label}</span></button>)}
      </section>
      {selectedSummary && <section className="work-panel work-summary-detail" id="work-summary-detail" aria-label={`${selectedSummary.label} details`}>
        <div className="work-panel-head"><h2>{selectedSummary.label}</h2><span>{selectedSummary.count}</span></div>
        {summaryView === "working" || summaryView === "waiting" ? (summaryView === "working" ? workingAgents : waitingAgents).length ? <ul className="work-summary-list">{(summaryView === "working" ? workingAgents : waitingAgents).map((agent) => <li key={agent.sessionId}>
          <Link className="work-summary-main-link" to={resourcePath(agent.projectId, "conversation", agent.sessionId)}><strong>{agent.title}</strong><small>{projectName(agent.projectId)} · {summaryView === "working" ? "Working" : "Waiting for input"}{agent.item ? ` · ${agent.item.title}` : " · No Work item linked"}</small></Link>
          {agent.item && <Link className="work-summary-item-link" to={projectSurface(agent.item.engagement_id, "work", agent.item.id)}>Work item <ArrowRight size={14} aria-hidden="true" /></Link>}
          {!agent.item && <button className="work-summary-track" type="button" disabled={busy || coreState !== "online"} onClick={() => openTracking(agent)}>Track work</button>}
        </li>)}</ul> : <p className="work-empty-inline">No conversations are {summaryView === "working" ? "working right now" : "waiting for input"}.</p>
          : summaryItems.length ? <ul className="work-summary-list">{summaryItems.map((item) => <li key={item.id}>
            <Link className="work-summary-main-link" to={projectSurface(item.engagement_id, "work", item.id)}><strong>{item.title}</strong><small>{projectName(item.engagement_id)} · {assignee(item)} · {time(item.last_update_at)}</small></Link>
            <ArrowRight size={16} aria-hidden="true" />
          </li>)}</ul> : <p className="work-empty-inline">No {selectedSummary.label.toLowerCase()} Work items.</p>}
      </section>}
      {!projectId ? <>
        <div className="work-overview-grid">
          <section className="work-panel" aria-labelledby="work-active-title">
            <div className="work-panel-head"><h2 id="work-active-title">Agents working now</h2><span>{workingAgents.length}</span></div>
            {workingAgents.length ? <ul className="work-activity-list">{workingAgents.map((agent) => <li key={agent.sessionId}>
              <span className="work-live-dot" aria-hidden="true" /><div><strong>{agent.item?.title ?? agent.title}</strong><small>{projectName(agent.projectId)} · {agent.item ? time(agent.item.last_update_at) : "No Work item linked"}</small></div>
              {!agent.item && <button className="work-summary-track" type="button" disabled={busy || coreState !== "online"} onClick={() => openTracking(agent)}>Track work</button>}
              <Link aria-label={`Open ${agent.item?.title ?? agent.title}`} to={agent.item ? projectSurface(agent.projectId, "work", agent.item.id) : resourcePath(agent.projectId, "conversation", agent.sessionId)}><ArrowRight size={16} /></Link>
            </li>)}</ul> : <p className="work-empty-inline">No agents are working right now.</p>}
          </section>
          <section className="work-panel" aria-labelledby="work-attention-title">
            <div className="work-panel-head"><h2 id="work-attention-title">Needs attention</h2><span>{counts.blocked + counts.review + waitingAgents.length}</span></div>
            {items.filter((item) => item.status === "blocked").slice(0, 8).map((item) => <Link className="work-attention-row" key={item.id} to={projectSurface(item.engagement_id, "work", item.id)}><CircleAlert size={16} /><span><strong>{item.title}</strong><small>{projectName(item.engagement_id)} · Blocked</small></span><ArrowRight size={15} /></Link>)}
            {items.filter((item) => item.status === "review").slice(0, 8).map((item) => <Link className="work-attention-row" key={item.id} to={projectSurface(item.engagement_id, "work", item.id)}><Clock3 size={16} /><span><strong>{item.title}</strong><small>{projectName(item.engagement_id)} · In review</small></span><ArrowRight size={15} /></Link>)}
            {waitingAgents.slice(0, 8).map((agent) => <Link className="work-attention-row" key={agent.sessionId} to={resourcePath(agent.projectId, "conversation", agent.sessionId)}><Clock3 size={16} /><span><strong>{agent.title}</strong><small>{projectName(agent.projectId)} · Waiting for input</small></span><ArrowRight size={15} /></Link>)}
            {!counts.blocked && !counts.review && !waitingAgents.length && <p className="work-empty-inline">Nothing needs attention right now.</p>}
          </section>
        </div>
        <section className="work-panel work-projects" aria-labelledby="work-projects-title"><div className="work-panel-head"><h2 id="work-projects-title">Projects</h2><span>{projectRows.count}</span>
            {anyStages && <div className="work-segmented" role="group" aria-label="Project view"><button type="button" aria-pressed={projectsView === "stages"} onClick={() => setParam("view", null)}>Stages</button><button type="button" aria-pressed={projectsView === "status"} onClick={() => setParam("view", "status")}>Status</button></div>}</div>
          {projectsView === "stages" && <div className="work-stage-filters" role="group" aria-label="Filter projects">
            <button type="button" aria-pressed={stageFilter === "all"} onClick={() => setStageFilter("all")}>All</button>
            <button type="button" aria-pressed={stageFilter === "gap"} data-tone="gap" onClick={() => setStageFilter("gap")}><i aria-hidden="true" />Missing a stage <span>{gapCount}</span></button>
            <button type="button" aria-pressed={stageFilter === "blocked"} data-tone="blocked" onClick={() => setStageFilter("blocked")}><i aria-hidden="true" />Blocked <span>{blockedProjects.size}</span></button>
          </div>}
          <div className="work-project-search"><input type="search" aria-label="Search projects" placeholder="Search projects" value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} /></div>
          <div className="work-project-list">{listedProjects.map(({ project, depth }) => {
            const projectItems = projectCounts.get(project.id) ?? { working: 0, waiting: 0, inProgress: 0, blocked: 0, review: 0 };
            const latestUpdate = projectItems.latest && recent.find((update) => update.item_id === projectItems.latest?.id && update.created_at === projectItems.latest?.last_update_at);
            const childCount = childrenByParent.get(project.id)?.length ?? 0;
            const expanded = Boolean(projectQuery.trim()) || expandedProjects.has(project.id);
            const cells = projectsView === "stages" ? cellsByProject.get(project.id) : undefined;
            return <div className={`work-project-row${depth ? " is-child" : ""}`} key={project.id} style={{ paddingLeft: Math.min(depth, 5) * 22 }}>
              {childCount > 0 ? <button type="button" aria-label={`${expanded ? "Hide" : "Show"} subprojects of ${project.name}`} aria-expanded={expanded} onClick={() => setExpandedProjects((current) => { const next = new Set(current); if (next.has(project.id)) next.delete(project.id); else next.add(project.id); return next; })}>{expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</button> : <span className="work-project-spacer" />}
              <div className="work-project-content"><Link className="work-project-primary" to={projectRoot(project.id)}><span className="work-project-initials" aria-hidden="true">{projectInitials(project.name)}</span><span className="work-project-main"><strong>{project.name}</strong><small>{childCount ? `${childCount} subprojects · ` : ""}{project.workEnabled ? "Agent tools on" : "Agent tools off"}{projectItems.latest ? "" : " · No check-ins yet"}</small></span><ProjectStatusCounts counts={projectItems} /><ArrowRight size={16} /></Link>
                {cells && <ProjectStageStrip projectId={project.id} projectName={project.name} cells={cells} />}
                {projectsView === "stages" && !cells && !effectiveStages(project, projectById).stages.length && <Link className="work-stage-define" to={`${projectSurface(project.id, "work")}?stages=edit`}><Columns3 size={14} aria-hidden="true" /> Define stages</Link>}
                {projectItems.latest && <Link className="work-project-latest" to={projectSurface(projectItems.latest.engagement_id, "work", projectItems.latest.id)}><span><strong>Latest check-in: {projectItems.latest.title}</strong><small>{projectName(projectItems.latest.engagement_id)} · {time(projectItems.latest.last_update_at)}</small>{latestUpdate && <small>{latestUpdate.blocker ? `Blocked: ${latestUpdate.blocker}` : latestUpdate.next_step ? `Next: ${latestUpdate.next_step}` : latestUpdate.summary}</small>}</span><ArrowRight size={15} aria-hidden="true" /></Link>}</div>
            </div>;
          })}</div>
          {stageFilter !== "all" && projectsView === "stages" && !listedProjects.length && <p className="work-empty-inline">{stageFilter === "gap" ? "No project is missing a flagged stage." : "No project has blocked items."}</p>}
          {projectRows.rows.length > visibleProjectLimit && <p className="work-empty-inline">Showing {visibleProjectLimit} rows. Search to narrow the list.</p>}
          {!projectRows.count && <div className="work-empty-inline">{activeProjects.length ? "No projects match your search." : <>No projects yet.{chrome?.openProjectPicker && <> <button className="button quiet" type="button" onClick={chrome.openProjectPicker}>Create a project</button></>}</>}</div>}
        </section>
        <section className="work-panel" aria-labelledby="work-recent-title"><div className="work-panel-head"><h2 id="work-recent-title">Recent check-ins</h2><span>{recent.length}</span></div>
          {recent.slice(0, 12).map((update) => { const item = items.find((candidate) => candidate.id === update.item_id); return <Link className="work-update-preview" key={update.id} to={projectSurface(update.engagement_id, "work", update.item_id)}><span><strong>{item?.title ?? "Work item"}</strong><small>{projectName(update.engagement_id)} · {time(update.created_at)}</small></span><p>{update.summary}</p></Link>; })}
          {!recent.length && <p className="work-empty-inline">No check-ins yet. Agents can post updates from their project tools.</p>}
        </section>
      </> : <>
        {rollup.length > 0 && <section className="work-panel work-stage-rollup" aria-labelledby="work-stage-rollup-title"><div className="work-panel-head"><h2 id="work-stage-rollup-title">Subprojects by stage</h2>{loading && !familyItems.length ? <LoaderCircle className="spin" size={15} aria-label="Loading subproject stages" /> : <span>{directChildren.length.toLocaleString()}</span>}</div>
          <SubprojectStageRollup cells={rollup} selected={subStage} href={(key) => subStageHref(key === subStage ? null : key)} />
        </section>}
        <div className={`work-project-layout${itemId ? " has-detail" : ""}`}>
          <div className="work-board-tools">
            <div className="work-segmented" role="group" aria-label="Group items"><button type="button" aria-pressed={!groupByStage} onClick={() => { setParam("group", null); setParam("stage", null); }}>Status</button><button type="button" aria-pressed={groupByStage} onClick={() => setParam("group", "stage")}>Stage</button></div>
            <button type="button" className="button quiet" onClick={() => setParam("stages", "edit")}><Columns3 size={15} aria-hidden="true" /> Stages</button>
          </div>
          {groupByStage ? <section className="work-board work-stage-board" aria-label="Project board by stage">
            {stagesFrom && <p className="work-stage-inherited">Using {stagesFrom.name}'s stages.</p>}
            {!projectStages.length && <p className="work-empty-inline">This project has no stages yet. <button type="button" className="button quiet" onClick={() => setParam("stages", "edit")}>Define stages</button></p>}
            {boardColumns.map((column) => <section className="work-column" key={column.key} data-focused={focusedStage === column.key || undefined} aria-labelledby={`work-stage-column-${column.key}`}>
              <header><h2 id={`work-stage-column-${column.key}`}>{column.label}</h2><span>{column.open.length}</span></header>
              <div className="work-column-items">{column.open.map((item) => <Link key={item.id} className={`work-card${item.id === itemId ? " selected" : ""}`} to={`${projectSurface(projectId, "work", item.id)}?group=stage`}>
                <span className={`work-status ${item.status}`}>{label(item.status)}</span><strong>{item.title}</strong>
                <small>{assignee(item)}{item.assignee_session_id && activity[item.assignee_session_id] === "working" ? " · Working" : ""}</small>
                <span className="work-card-time"><Clock3 size={13} /> {time(item.last_update_at)}</span>
              </Link>)}{!column.open.length && <p className="work-column-empty">No open items</p>}{column.done > 0 && <p className="work-column-done">{column.done} done</p>}</div>
            </section>)}
          </section> : <section className="work-board" aria-label="Project board">
            {columns.map((column) => <section className="work-column" key={column.id} aria-labelledby={`work-column-${column.id}`}>
              <header><h2 id={`work-column-${column.id}`}>{column.label}</h2><span>{counts[column.id]}</span></header>
              <div className="work-column-items">{items.filter((item) => item.status === column.id).map((item) => <Link key={item.id} className={`work-card${item.id === itemId ? " selected" : ""}`} to={projectSurface(projectId, "work", item.id)}>
                <span className={`work-priority ${item.priority}`}>{item.priority}</span><strong>{item.title}</strong>
                <small>{assignee(item)}{item.assignee_session_id && activity[item.assignee_session_id] === "working" ? " · Working" : ""}</small>
                <span className="work-card-time"><Clock3 size={13} /> {time(item.last_update_at)}</span>
              </Link>)}{!counts[column.id] && <p className="work-column-empty">No items</p>}</div>
            </section>)}
          </section>}
          {itemId && <aside className="work-detail" aria-labelledby="work-detail-title">
            <div className="work-detail-top"><span>Work item</span><Link title="Close item" aria-label="Close item" to={`${projectSurface(projectId, "work")}${groupByStage ? "?group=stage" : ""}`}><X size={17} /></Link></div>
            {selectedItem ? <>
              <h2 id="work-detail-title">{selectedItem.title}</h2><div className="work-detail-meta"><span className={`work-status ${selectedItem.status}`}>{label(selectedItem.status)}</span><span>{selectedItem.priority} priority</span></div>
              {latestUpdate && <section className="work-current-update" aria-labelledby="work-current-update-title">
                <div className="work-current-update-head"><h3 id="work-current-update-title">Current progress</h3><time dateTime={latestUpdate.created_at}>{time(latestUpdate.created_at)}</time></div>
                <WorkUpdateDetails update={latestUpdate} projectId={selectedItem.engagement_id} />
              </section>}
              {selectedItem.description && (selectedItem.source_kind === "import" && latestUpdate
                ? <details className="work-original-description"><summary>Original brief</summary><p className="work-description">{selectedItem.description}</p></details>
                : <p className="work-description">{selectedItem.description}</p>)}
              <dl className="work-fields">{(projectStages.length > 0 || selectedItem.stage) && <div><dt><label htmlFor="work-item-stage">Stage</label></dt><dd><select id="work-item-stage" value={selectedItem.stage ?? ""} disabled={busy} onChange={(event) => void changeStage(event.target.value)}><option value="">No stage</option>{projectStages.map((stage) => <option key={stage.key} value={stage.key}>{stage.label}</option>)}{selectedItem.stage && !projectStages.some((stage) => stage.key === selectedItem.stage) && <option value={selectedItem.stage}>{stageLabel(selectedItem.stage)} (not listed)</option>}</select></dd></div>}<div><dt>Assigned to</dt><dd>{selectedItem.assignee_session_id ? <Link to={resourcePath(projectId, "conversation", selectedItem.assignee_session_id)}>{assignee(selectedItem)}</Link> : "Unassigned"}</dd></div><div><dt>Last update</dt><dd>{time(selectedItem.last_update_at)}</dd></div></dl>
              {selectedUpdates.length !== 1 && <><h3>Earlier check-ins</h3><ol className="work-timeline">{selectedUpdates.slice(1).map((update) => <li key={update.id}><span className="work-timeline-dot" /><div><div className="work-timeline-top"><strong>{update.actor_kind === "agent" ? "Agent" : update.actor_kind === "import" ? "Imported" : "Operator"}</strong><time dateTime={update.created_at}>{time(update.created_at)}</time></div><WorkUpdateDetails update={update} projectId={selectedItem.engagement_id} /></div></li>)}{!selectedUpdates.length && <li className="work-no-updates">No check-ins yet.</li>}</ol></>}
              <form className="work-checkin-form" onSubmit={(event) => void checkIn(event)}><h3>Post an update</h3><label>Progress<textarea required maxLength={4000} value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="What changed?" /></label><label>Next step<input maxLength={2000} value={nextStep} onChange={(event) => setNextStep(event.target.value)} /></label><label>Blocker<input maxLength={2000} value={blocker} onChange={(event) => setBlocker(event.target.value)} /></label><label>Status<select value={checkInStatus} disabled={blocked} aria-describedby={blocked ? blockerHintId : undefined} onChange={(event) => setStatusChoice({ itemId: selectedItem.id, status: event.target.value as Status })}>{columns.map((column) => <option key={column.id} value={column.id}>{column.label}</option>)}</select>{blocked && <small id={blockerHintId} className="work-field-hint">A blocker saves this update as Blocked.</small>}</label><button className="button primary" type="submit" disabled={busy || !summary.trim()}>Save update</button></form>
            </> : <div role="alert" className="work-empty-inline">This item is unavailable. <Link to={projectSurface(projectId, "work")}>Return to board</Link></div>}
          </aside>}
        </div>
        {hasChildren && <section className="work-panel work-subprojects" id="work-subprojects" aria-labelledby="work-subprojects-title"><div className="work-panel-head"><h2 id="work-subprojects-title">Subprojects</h2><span>{listedChildren.length.toLocaleString()}</span></div>
          {rollupFilter && <div className="work-subproject-filter">
            <div className="work-segmented" role="group" aria-label={`${rollupFilter.label} filter`}><button type="button" aria-pressed={!subMissing} onClick={() => setParam("subreach", null)}>Reached {rollupFilter.label} <span>{rollupFilter.reached.size.toLocaleString()}</span></button><button type="button" aria-pressed={subMissing} onClick={() => setParam("subreach", "missing")}>Not yet <span>{(rollupFilter.total - rollupFilter.reached.size).toLocaleString()}</span></button></div>
            <button type="button" className="icon-button work-filter-clear" title="Show all subprojects" aria-label="Show all subprojects" onClick={() => setSearchParams((current) => { const next = new URLSearchParams(current); next.delete("substage"); next.delete("subreach"); return next; }, { replace: true })}><X size={15} /></button>
          </div>}
          <div className="work-project-search"><input type="search" aria-label="Search subprojects" placeholder="Search subprojects" value={projectQuery} onChange={(event) => setProjectQuery(event.target.value)} /></div><div className="work-project-list">{listedChildren.slice(0, visibleProjectLimit).map((child) => <div className="work-project-row" key={child.id}><span className="work-project-spacer" /><Link to={projectSurface(child.id, "work")}><span><strong>{child.name}</strong><small>{child.workEnabled ? "Agent tools on" : "Agent tools off"}</small></span><ArrowRight size={16} /></Link></div>)}{!listedChildren.length && <p className="work-empty-inline">No subprojects match.</p>}</div>{listedChildren.length > visibleProjectLimit && <p className="work-empty-inline">Showing {visibleProjectLimit} of {listedChildren.length.toLocaleString()}. Search to narrow the list.</p>}</section>}
      </>}
    </>}
    {editingStages && selectedProject && <WorkStageEditor key={selectedProject.id} project={selectedProject} inherited={stagesFrom ? { from: stagesFrom.name, stages: projectStages } : undefined} itemCounts={Object.fromEntries(boardColumns.map((column) => [column.key, column.open.length]))} save={saveStages} close={() => setParam("stages", null)} />}
    {showCreate && <div className="work-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setShowCreate(false); }}><form className="work-dialog" role="dialog" aria-modal="true" aria-labelledby="work-create-title" onSubmit={(event) => void create(event)}><header><h2 id="work-create-title">New work item</h2><button className="work-dialog-close" type="button" aria-label="Close" disabled={busy} onClick={() => setShowCreate(false)}><X size={18} /></button></header>{!projectId && <label>Project<select required value={createProjectId} onChange={(event) => setCreateProjectId(event.target.value)}><option value="">Choose a project</option>{engagements.filter((project) => project.status !== "archived").map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>}<label>Title<input autoFocus required maxLength={300} value={title} onChange={(event) => setTitle(event.target.value)} /></label>{createStages.length > 0 && <label>Stage<select value={createStage} onChange={(event) => setCreateStage(event.target.value)}><option value="">No stage</option>{createStages.map((stage) => <option key={stage.key} value={stage.key}>{stage.label}</option>)}</select></label>}<label>Description<textarea maxLength={20000} value={description} onChange={(event) => setDescription(event.target.value)} /></label><footer><button type="button" className="button quiet" disabled={busy} onClick={() => setShowCreate(false)}>Cancel</button><button type="submit" className="button primary" aria-busy={busy} disabled={busy || !title.trim()}>{busy ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <ListTodo size={16} />} Create item</button></footer></form></div>}
    {trackingAgent && <div className="work-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setTrackingAgent(null); }}><form className="work-dialog" role="dialog" aria-modal="true" aria-labelledby="work-track-title" onSubmit={(event) => void trackWork(event)}><header><h2 id="work-track-title">Track conversation work</h2><button className="work-dialog-close" type="button" aria-label="Close" title="Close" disabled={busy} onClick={() => setTrackingAgent(null)}><X size={18} aria-hidden="true" /></button></header><p className="work-track-context">{projectName(trackingAgent.projectId)} · {trackingAgent.title}</p>
      {availableTrackItems.length > 0 && <><label>Find an existing item<input type="search" value={trackSearch} onChange={(event) => setTrackSearch(event.target.value)} placeholder="Search open items" /></label><label>Work item<select value={trackItemId} onChange={(event) => setTrackItemId(event.target.value)}><option value="">Create a new item</option>{visibleTrackItems.map((item) => <option key={item.id} value={item.id}>{item.title} · {label(item.status)}</option>)}</select></label></>}
      {!trackItemId && <label>Title<input autoFocus required maxLength={300} value={trackTitle} onChange={(event) => setTrackTitle(event.target.value)} /></label>}
      {trackError && <p className="work-track-error" role="alert">{trackError}</p>}
      <footer><button type="button" className="button quiet" disabled={busy} onClick={() => setTrackingAgent(null)}>Cancel</button><button type="submit" className="button primary" disabled={busy || (!trackItemId && !trackTitle.trim())}>{trackItemId ? "Link item" : "Create and link"}</button></footer></form></div>}
  </div>;
}
