/**
 * Core's project holds (#1232): while a project is held for a deployment, no
 * chat work starts in it and its running goals wait for the release. One
 * watcher per ApiClient polls the projects that a view is showing, so the
 * shell banner and the composer share one read.
 */

export interface ProjectHoldRecord {
  id: string;
  status: "holding" | "held" | "released";
  reason: string;
  deployment_id?: string | null;
  grace_seconds: number;
  grace_until?: string | null;
  created_at: string;
  held_at?: string | null;
  released_at?: string | null;
  paused_goals: { goal_id: string; session_id: string }[];
}

export interface ProjectHoldView {
  project_id: string;
  held: boolean;
  hold: ProjectHoldRecord | null;
  active_turns: number;
  running_goals: number;
}

export interface ProjectHoldState {
  held: boolean;
  /** "holding" while running turns may still finish; "held" once nothing runs. */
  phase?: "holding" | "held";
  holdId?: string;
  reason?: string;
  graceUntil?: string;
  pausedGoals: number;
  activeTurns: number;
}

export const OPEN_PROJECT: ProjectHoldState = { held: false, pausedGoals: 0, activeTurns: 0 };
/** Polls while a project is held: the grace countdown and release are seen quickly. */
export const HELD_POLL_MS = 5_000;
/** Polls otherwise: a hold placed for a deployment shows within this interval. */
export const OPEN_POLL_MS = 30_000;

export function holdState(view: ProjectHoldView | undefined): ProjectHoldState {
  const hold = view?.hold;
  if (!view?.held || !hold || hold.status === "released") return OPEN_PROJECT;
  return {
    held: true,
    phase: hold.status,
    holdId: hold.id,
    reason: hold.reason,
    graceUntil: hold.grace_until ?? undefined,
    pausedGoals: hold.paused_goals.length,
    activeTurns: view.active_turns,
  };
}

type Request = <T>(path: string, init?: RequestInit) => Promise<T>;
type Listener = (state: ProjectHoldState) => void;

interface Watched {
  state: ProjectHoldState;
  listeners: Set<Listener>;
  timer?: ReturnType<typeof setTimeout>;
  controller?: AbortController;
}

export class ProjectHoldWatch {
  private readonly projects = new Map<string, Watched>();

  constructor(private readonly request: Request) {}

  state(projectId: string | undefined): ProjectHoldState {
    return (projectId && this.projects.get(projectId)?.state) || OPEN_PROJECT;
  }

  subscribe(projectId: string, listener: Listener): () => void {
    let watched = this.projects.get(projectId);
    if (!watched) {
      watched = { state: OPEN_PROJECT, listeners: new Set() };
      this.projects.set(projectId, watched);
    }
    watched.listeners.add(listener);
    if (watched.listeners.size === 1) void this.read(projectId);
    return () => {
      const current = this.projects.get(projectId);
      if (!current) return;
      current.listeners.delete(listener);
      if (current.listeners.size) return;
      if (current.timer) clearTimeout(current.timer);
      current.controller?.abort();
      this.projects.delete(projectId);
    };
  }

  /** Read again now, for example after Core refused work with `project_held`. */
  refreshAll(): void {
    for (const projectId of this.projects.keys()) void this.read(projectId);
  }

  async release(projectId: string, holdId: string, reason?: string): Promise<ProjectHoldState> {
    const view = await this.request<ProjectHoldView>(
      `engagements/${encodeURIComponent(projectId)}/hold/${encodeURIComponent(holdId)}/release`,
      { method: "POST", body: JSON.stringify({ reason: reason ?? "Released from the interface" }) },
    );
    this.publish(projectId, holdState(view));
    return holdState(view);
  }

  private async read(projectId: string): Promise<void> {
    const watched = this.projects.get(projectId);
    if (!watched) return;
    if (watched.timer) clearTimeout(watched.timer);
    watched.controller?.abort();
    const controller = new AbortController();
    watched.controller = controller;
    let next = watched.state;
    let unsupported = false;
    try {
      const view = await this.request<ProjectHoldView>(`engagements/${encodeURIComponent(projectId)}/hold`, { signal: controller.signal });
      next = holdState(view);
    } catch (error) {
      // diagnostic-expected: the request path logs the failure; an idle project or a lost read keeps the last known state until the next poll.
      unsupported = (error as { status?: unknown }).status === 404;
    }
    if (controller.signal.aborted || this.projects.get(projectId) !== watched) return;
    this.publish(projectId, next);
    // A Core older than project holds has no route: stop asking it.
    if (unsupported) return;
    watched.timer = setTimeout(() => void this.read(projectId), next.held ? HELD_POLL_MS : OPEN_POLL_MS);
  }

  private publish(projectId: string, state: ProjectHoldState): void {
    const watched = this.projects.get(projectId);
    if (!watched) return;
    watched.state = state;
    for (const listener of watched.listeners) listener(state);
  }
}
