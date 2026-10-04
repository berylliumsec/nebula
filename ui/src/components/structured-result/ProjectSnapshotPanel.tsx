import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { ExternalLink, GripHorizontal, LayoutDashboard, Minimize2, MoveDiagonal2, X } from "lucide-react";
import { Link } from "react-router-dom";
import { projectRoot, projectSurface, resourcePath } from "../../resourceRoutes";
import { currentProjectWorkItem, type ProjectWorkItem, type ProjectWorkUpdate } from "../../projectWorkProgress";
import { ProjectSummaryCards } from "../ProjectSummaryCards";
import { useWorkspace } from "../../state/WorkspaceContext";
import { IconAction } from "../IconAction";
import {
  arrowDirection,
  clampPoint,
  clampRect,
  currentViewport,
  readLauncher,
  readRect,
  STEP,
  writeLauncher,
  writeRect,
  type ProjectSnapshotPoint,
  type ProjectSnapshotRect,
} from "./projectSnapshotGeometry";

/** Below this width the view is a sheet: there is no room to float beside. */
const SHEET_QUERY = "(max-width: 760px)";

function useSheet(): boolean {
  const [sheet, setSheet] = useState(() => typeof matchMedia === "function" && matchMedia(SHEET_QUERY).matches);
  useEffect(() => {
    if (typeof matchMedia !== "function") return;
    const media = matchMedia(SHEET_QUERY);
    const update = () => setSheet(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return sheet;
}

/** Re-fit a floating surface whenever the window or the zoomed viewport moves. */
function useViewportChange(onChange: () => void, active: boolean) {
  const latest = useRef(onChange);
  latest.current = onChange;
  useEffect(() => {
    if (!active) return;
    const fit = () => latest.current();
    window.addEventListener("resize", fit);
    window.visualViewport?.addEventListener("resize", fit);
    window.visualViewport?.addEventListener("scroll", fit);
    return () => {
      window.removeEventListener("resize", fit);
      window.visualViewport?.removeEventListener("resize", fit);
      window.visualViewport?.removeEventListener("scroll", fit);
    };
  }, [active]);
}

/**
 * The conversation's toolbar and composer, which a first placement keeps
 * clear. Once the operator moves the panel, where they put it wins.
 */
function pageClearance() {
  const toolbar = document.querySelector(".session-toolbar-actions")?.getBoundingClientRect();
  const composer = document.querySelector("form.chat-composer")?.getBoundingClientRect();
  return {
    below: toolbar && toolbar.height > 0 ? toolbar.bottom : undefined,
    above: composer && composer.height > 0 ? composer.top : undefined,
  };
}

type Gesture = { kind: "move" | "resize"; pointerX: number; pointerY: number; start: ProjectSnapshotRect };

type SnapshotWorkItem = ProjectWorkItem;
type SnapshotWorkUpdate = ProjectWorkUpdate;
type CurrentWork = { item: SnapshotWorkItem; update: SnapshotWorkUpdate };

function workStatusLabel(status: string): string {
  const words = status.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Prefer the active conversation's saved work; otherwise show the newest active check-in. */
export function currentSnapshotItem(items: SnapshotWorkItem[], sessionId?: string): SnapshotWorkItem | undefined {
  return currentProjectWorkItem(items, sessionId);
}

export interface ProjectSnapshotPanelProps {
  projectId: string;
  sessionId?: string;
  minimized: boolean;
  onMinimize: () => void;
  onRestore: () => void;
  onClose: () => void;
}

/** The project dashboard summary beside the conversation, using Workspace's Core state. */
export function ProjectSnapshotPanel(props: ProjectSnapshotPanelProps) {
  const [rect, setRect] = useState<ProjectSnapshotRect>(() => readRect(currentViewport(), pageClearance()));
  const [launcher, setLauncher] = useState<ProjectSnapshotPoint | undefined>(() => readLauncher());
  const sheet = useSheet();

  return createPortal(props.minimized
    ? <ProjectSnapshotLauncher position={launcher} onMove={setLauncher} onRestore={props.onRestore} />
    : <FloatingProjectSnapshot {...props} rect={rect} onRect={setRect} sheet={sheet} />,
  document.body);
}

function FloatingProjectSnapshot({
  projectId, sessionId, onMinimize, onClose, rect, onRect, sheet,
}: ProjectSnapshotPanelProps & {
  rect: ProjectSnapshotRect;
  onRect: (rect: ProjectSnapshotRect) => void;
  sheet: boolean;
}) {
  const titleId = useId();
  const panel = useRef<HTMLElement>(null);
  const gesture = useRef<Gesture | undefined>(undefined);
  const { api, engagement, assets, findings, run, approvals } = useWorkspace();
  const [currentWork, setCurrentWork] = useState<CurrentWork | null>(null);
  const [workLoading, setWorkLoading] = useState(true);
  const [workError, setWorkError] = useState<string>();
  const workReadSequence = useRef(0);
  const loadWork = useCallback(async (signal?: AbortSignal) => {
    const sequence = ++workReadSequence.current;
    if (!api) { setWorkError("Core is unavailable."); setWorkLoading(false); return; }
    try {
      const linked = sessionId ? await api.request<SnapshotWorkUpdate[]>(`work/updates?source_session_id=${encodeURIComponent(sessionId)}&limit=1`, { signal }) : [];
      let item: SnapshotWorkItem | undefined;
      let workProjectId = projectId;
      if (linked[0]) {
        workProjectId = linked[0].engagement_id;
        item = await api.request<SnapshotWorkItem>(`engagements/${encodeURIComponent(workProjectId)}/work/${encodeURIComponent(linked[0].item_id)}`, { signal });
      } else {
        const items = await api.request<SnapshotWorkItem[]>(`engagements/${encodeURIComponent(projectId)}/work`, { signal });
        item = currentSnapshotItem(items, sessionId);
      }
      const updates = item ? await api.request<SnapshotWorkUpdate[]>(`engagements/${encodeURIComponent(workProjectId)}/work/${encodeURIComponent(item.id)}/updates`, { signal }) : [];
      if (signal?.aborted || sequence !== workReadSequence.current) return;
      setCurrentWork(item && updates[0] ? { item, update: updates[0] } : null);
      setWorkError(undefined);
    } catch (error) {
      // diagnostic-expected: the panel shows a retry action while Core remains unavailable.
      if (signal?.aborted || sequence !== workReadSequence.current) return;
      setWorkError(error instanceof Error ? error.message : "Work progress could not be loaded.");
    } finally {
      if (!signal?.aborted && sequence === workReadSequence.current) setWorkLoading(false);
    }
  }, [api, projectId, sessionId]);
  useEffect(() => {
    const controller = new AbortController();
    setWorkLoading(true);
    void loadWork(controller.signal);
    if (!api) return () => controller.abort();
    const follow = async () => {
      while (!controller.signal.aborted) {
        try {
          await api.watchWorkChanges(() => void loadWork(controller.signal), () => void loadWork(controller.signal), controller.signal);
        } catch { /* diagnostic-expected: keep the saved summary readable while the event stream reconnects. */ }
        if (controller.signal.aborted) break;
        await new Promise<void>((resolve) => {
          const timeout = window.setTimeout(resolve, 1_000);
          controller.signal.addEventListener("abort", () => { window.clearTimeout(timeout); resolve(); }, { once: true });
        });
      }
    };
    void follow();
    return () => { workReadSequence.current += 1; controller.abort(); };
  }, [api, loadWork]);

  const place = (next: ProjectSnapshotRect, persist: boolean) => {
    const fitted = clampRect(next, currentViewport());
    onRect(fitted);
    if (persist) writeRect(fitted);
  };
  useViewportChange(() => place(rect, false), !sheet);
  // Opening moves focus into the view, so a keyboard user lands where it is.
  useLayoutEffect(() => { panel.current?.focus(); }, []);

  const begin = (kind: Gesture["kind"]) => (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = { kind, pointerX: event.clientX, pointerY: event.clientY, start: rect };
  };
  const track = (event: PointerEvent<HTMLButtonElement>) => {
    const active = gesture.current;
    if (!active) return;
    const dx = event.clientX - active.pointerX;
    const dy = event.clientY - active.pointerY;
    place(active.kind === "move"
      ? { ...active.start, x: active.start.x + dx, y: active.start.y + dy }
      : { ...active.start, width: active.start.width + dx, height: active.start.height + dy }, false);
  };
  const end = () => {
    if (!gesture.current) return;
    gesture.current = undefined;
    writeRect(rect);
  };
  const nudge = (kind: Gesture["kind"]) => (event: KeyboardEvent<HTMLButtonElement>) => {
    const direction = arrowDirection(event.key);
    if (!direction) return;
    event.preventDefault();
    const [dx, dy] = direction.map((unit) => unit * STEP);
    place(kind === "move"
      ? { ...rect, x: rect.x + dx, y: rect.y + dy }
      : { ...rect, width: rect.width + dx, height: rect.height + dy }, true);
  };
  const gestureHandlers = (kind: Gesture["kind"]) => ({
    onPointerDown: begin(kind),
    onPointerMove: track,
    onPointerUp: end,
    onPointerCancel: end,
    onLostPointerCapture: end,
    onKeyDown: nudge(kind),
  });

  return <section
    ref={panel}
    className={`project-snapshot-panel${sheet ? " sheet" : ""}`}
    role="dialog"
    aria-labelledby={titleId}
    tabIndex={-1}
    style={sheet ? undefined : { left: rect.x, top: rect.y, width: rect.width, height: rect.height }}
    onKeyDown={(event) => {
      // Escape puts the view away without losing it; Close is a choice.
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        onMinimize();
      }
    }}
  >
    <div className="project-snapshot-header">
      {!sheet && <button
        type="button"
        className="icon-button subtle project-snapshot-handle"
        aria-label="Move Project Snapshot"
        title="Drag to move · arrow keys to reposition"
        {...gestureHandlers("move")}
      ><GripHorizontal size={18} aria-hidden="true" /></button>}
      <div className="project-snapshot-title">
        <h2 id={titleId}><LayoutDashboard size={14} aria-hidden="true" /> Project Snapshot</h2>
        <small role="status">{engagement?.name ?? "Project status"}</small>
      </div>
      <IconAction icon={Minimize2} label="Minimize Project Snapshot" title="Minimize Project Snapshot" onClick={onMinimize} />
      <IconAction icon={X} label="Close Project Snapshot" onClick={onClose} />
    </div>

    <div className="project-snapshot-scroll">
      <section className="project-snapshot-work" aria-label="Current progress">
        <div className="project-snapshot-work-heading"><strong>Current progress</strong>{currentWork && <time dateTime={currentWork.update.created_at}>{new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(currentWork.update.created_at))}</time>}</div>
        {currentWork ? <>
          <div className="project-snapshot-work-item"><strong>{currentWork.item.title}</strong><small>{workStatusLabel(currentWork.item.status)} · {currentWork.item.priority} priority</small></div>
          <p>{currentWork.update.summary}</p>
          {currentWork.update.next_step && <small>Next: {currentWork.update.next_step}</small>}
          {currentWork.update.blocker && <small className="project-snapshot-blocker">Blocked: {currentWork.update.blocker}</small>}
          <div className="project-snapshot-work-links"><Link to={projectSurface(currentWork.item.engagement_id, "work", currentWork.item.id)}>Open work item</Link>{(sessionId ?? currentWork.update.source_session_id) && <Link to={resourcePath(sessionId ? projectId : currentWork.update.source_engagement_id ?? projectId, "conversation", (sessionId ?? currentWork.update.source_session_id)!)}>Open conversation</Link>}</div>
        </> : workError ? <div role="alert" className="project-snapshot-work-state">Could not load Work progress. <button type="button" className="button quiet" onClick={() => { setWorkLoading(true); void loadWork(); }}>Retry</button></div>
          : <p className="project-snapshot-work-state">{workLoading ? "Loading Work progress…" : "No Work check-in has been saved for this project."}</p>}
      </section>
      <ProjectSummaryCards assets={assets} findings={findings} run={run} />
      {run?.totalTasks ? <div className="project-snapshot-progress"><span>Mission progress</span><strong>{run.completedTasks} of {run.totalTasks} tasks</strong><progress value={run.completedTasks} max={run.totalTasks} /></div> : null}
      {approvals.length > 0 && <p className="project-snapshot-attention">{approvals.length} approval{approvals.length === 1 ? "" : "s"} waiting for review</p>}
    </div>

    <div className="project-snapshot-footer">
      <Link className="button quiet" to={projectRoot(projectId)}>
        <ExternalLink size={14} aria-hidden="true" /> Open dashboard
      </Link>
      {!sheet && <button
        type="button"
        className="icon-button subtle project-snapshot-resize"
        aria-label="Resize Project Snapshot"
        title="Drag to resize · arrow keys to change the size"
        {...gestureHandlers("resize")}
      ><MoveDiagonal2 size={16} aria-hidden="true" /></button>}
    </div>
  </section>;
}

function ProjectSnapshotLauncher({ position, onMove, onRestore }: {
  position?: ProjectSnapshotPoint;
  onMove: (point: ProjectSnapshotPoint) => void;
  onRestore: () => void;
}) {
  const shell = useRef<HTMLDivElement>(null);
  const show = useRef<HTMLButtonElement>(null);
  const drag = useRef<{ pointerX: number; pointerY: number; left: number; top: number } | undefined>(undefined);
  const status = "Project status at a glance";

  const place = (point: ProjectSnapshotPoint, persist: boolean) => {
    const box = shell.current?.getBoundingClientRect();
    const fitted = clampPoint(point, { width: box?.width ?? 300, height: box?.height ?? 56 }, currentViewport());
    onMove(fitted);
    if (persist) writeLauncher(fitted);
  };
  useViewportChange(() => { if (position) place(position, false); }, Boolean(position));
  // Minimizing hands focus to the way back.
  useLayoutEffect(() => { show.current?.focus(); }, []);

  const origin = () => {
    const box = shell.current?.getBoundingClientRect();
    return { x: box?.left ?? 0, y: box?.top ?? 0 };
  };

  return <div
    ref={shell}
    className="project-snapshot-launcher"
    style={position ? { left: position.x, top: position.y, right: "auto", bottom: "auto" } : undefined}
  >
    <button
      type="button"
      className="icon-button subtle project-snapshot-handle"
      aria-label="Move minimized Project Snapshot"
      title="Drag to move · arrow keys to reposition"
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        const start = origin();
        drag.current = { pointerX: event.clientX, pointerY: event.clientY, left: start.x, top: start.y };
      }}
      onPointerMove={(event) => {
        const active = drag.current;
        if (active) place({ x: active.left + event.clientX - active.pointerX, y: active.top + event.clientY - active.pointerY }, false);
      }}
      onPointerUp={() => { drag.current = undefined; if (position) writeLauncher(position); }}
      onPointerCancel={() => { drag.current = undefined; }}
      onLostPointerCapture={() => { drag.current = undefined; }}
      onKeyDown={(event) => {
        const direction = arrowDirection(event.key);
        if (!direction) return;
        event.preventDefault();
        const start = origin();
        place({ x: start.x + direction[0] * STEP, y: start.y + direction[1] * STEP }, true);
      }}
    ><GripHorizontal size={18} aria-hidden="true" /></button>
    <button ref={show} type="button" className="project-snapshot-launcher-show" aria-label={`Show Project Snapshot, ${status}`} onClick={onRestore}>
      <span>
        <strong><LayoutDashboard size={13} aria-hidden="true" /> Project Snapshot</strong>
        <small role="status">{status}</small>
      </span>
      <em>Show</em>
    </button>
  </div>;
}
