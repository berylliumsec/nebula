import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, CircleAlert, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import type { ApiClient } from "../api/client";
import { activeProjectWorkItems, type ProjectWorkItem, type ProjectWorkUpdate } from "../projectWorkProgress";
import { projectSurface, resourcePath } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";
import "./ProjectWorkOverview.css";

type ProgressState = {
  items: ProjectWorkItem[];
  current?: ProjectWorkItem;
  update?: ProjectWorkUpdate;
  loading: boolean;
  error?: string;
};

function timestamp(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function statusLabel(value: string): string {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

async function readProgress(api: ApiClient, projectId: string, signal?: AbortSignal): Promise<Omit<ProgressState, "loading" | "error">> {
  const items = await api.request<ProjectWorkItem[]>(`engagements/${encodeURIComponent(projectId)}/work`, { signal });
  const candidates = activeProjectWorkItems(items).filter((item) => item.last_update_at);
  for (const current of candidates) {
    const updates = await api.request<ProjectWorkUpdate[]>(`engagements/${encodeURIComponent(projectId)}/work/${encodeURIComponent(current.id)}/updates`, { signal });
    if (updates[0]) return { items, current, update: updates[0] };
  }
  return { items };
}

export function ProjectWorkOverview({ projectId }: { projectId: string }) {
  const { api, coreState } = useWorkspace();
  const [state, setState] = useState<ProgressState>({ items: [], loading: true });
  const [revision, setRevision] = useState(0);
  const requestVersion = useRef(0);
  const active = useMemo(() => activeProjectWorkItems(state.items), [state.items]);
  const others = active.filter((item) => item.id !== state.current?.id).slice(0, 3);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const version = ++requestVersion.current;
    if (!api) {
      if (!signal?.aborted) setState((previous) => ({ ...previous, loading: false, error: "Core is unavailable. Reconnect and retry." }));
      return;
    }
    try {
      const result = await readProgress(api, projectId, signal);
      if (!signal?.aborted && version === requestVersion.current) setState({ ...result, loading: false });
    } catch (error) {
      if (!signal?.aborted && version === requestVersion.current) setState((previous) => ({ ...previous, loading: false, error: error instanceof Error ? error.message : "Core could not complete the read." }));
    }
  }, [api, projectId]);

  useEffect(() => {
    const controller = new AbortController();
    setState({ items: [], loading: true });
    void refresh(controller.signal);
    if (!api) return () => controller.abort();
    const follow = async () => {
      while (!controller.signal.aborted) {
        try {
          await api.watchWorkChanges(() => void refresh(controller.signal), () => void refresh(controller.signal), controller.signal);
        } catch { /* The saved progress remains visible while the stream reconnects. */ }
        if (controller.signal.aborted) break;
        await new Promise<void>((resolve) => {
          const timeout = window.setTimeout(resolve, 1_000);
          controller.signal.addEventListener("abort", () => { window.clearTimeout(timeout); resolve(); }, { once: true });
        });
      }
    };
    void follow();
    return () => { requestVersion.current += 1; controller.abort(); };
  }, [api, projectId, refresh, revision]);

  return <section className="project-work-overview panel" aria-labelledby="project-current-progress-title">
    <div className="project-work-primary">
      <header className="project-work-overview-head">
        <h2 id="project-current-progress-title">Current progress</h2>
        <button className="icon-button subtle" type="button" aria-label="Refresh current progress" title="Refresh current progress" onClick={() => setRevision((value) => value + 1)}><RefreshCw size={16} aria-hidden="true" /></button>
      </header>
      {state.loading ? <p className="project-work-message" role="status">Loading current progress…</p>
        : state.error ? <div className="project-work-error" role="alert"><CircleAlert size={17} aria-hidden="true" /><span>Current progress could not be loaded. {state.error}</span><button className="button quiet" type="button" onClick={() => setRevision((value) => value + 1)}>Retry</button></div>
          : state.current && state.update ? <>
            <div className="project-work-current-meta"><span className="project-work-status">{statusLabel(state.current.status)}</span><strong>{state.current.title}</strong><time dateTime={state.update.created_at}>{timestamp(state.update.created_at)}</time></div>
            <p className="project-work-summary">{state.update.summary}</p>
            {state.update.next_step && <p className="project-work-next"><span>Next</span> {state.update.next_step}</p>}
            {state.update.blocker && <p className="project-work-blocker"><span>Blocked</span> {state.update.blocker}</p>}
            <div className="project-work-links">
              {state.update.source_session_id && <Link to={resourcePath(state.update.source_engagement_id ?? projectId, "conversation", state.update.source_session_id)}>Open conversation <ArrowRight size={15} aria-hidden="true" /></Link>}
              <Link to={projectSurface(projectId, "work", state.current.id)}>Open work item <ArrowRight size={15} aria-hidden="true" /></Link>
            </div>
          </> : <div className="project-work-message"><strong>No current progress yet</strong><p>Post a check-in on an active Work item to show its progress here.</p><Link to={projectSurface(projectId, "work")}>Open project board <ArrowRight size={15} aria-hidden="true" /></Link></div>}
      {coreState !== "online" && !state.loading && !state.error && <p className="project-work-connection" role="status">Offline. Showing the last saved progress loaded on this page.</p>}
    </div>
    <aside className="project-work-other" aria-label="Other active work">
      <h3>Other active work</h3>
      {others.length ? <ul>{others.map((item) => <li key={item.id}><Link to={projectSurface(projectId, "work", item.id)}><strong>{item.title}</strong><span>{statusLabel(item.status)} · {item.last_update_at ? timestamp(item.last_update_at) : "No check-in yet"}</span></Link></li>)}</ul>
        : <p>{active.length ? "No other active items." : "No active items."}</p>}
      <Link className="project-work-board-link" to={projectSurface(projectId, "work")}>View project board <ArrowRight size={15} aria-hidden="true" /></Link>
    </aside>
  </section>;
}
