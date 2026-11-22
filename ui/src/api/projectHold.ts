/**
 * Core's project holds (#1232): while a project is held for a deployment, no
 * chat work starts in it and its running goals wait for the release. One
 * watcher per ApiClient polls the projects that a view is showing, so the
 * shell banner and the composer share one read.
 *
 * A chat hold covers one chat (and its subagent and goal-child chats) while
 * the rest of the project keeps working: the operator sets current research
 * aside when priorities shift. Holds placed together share a batch.
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
  session_id?: string | null;
  batch_id?: string | null;
}

export interface ProjectHoldView {
  project_id: string;
  held: boolean;
  hold: ProjectHoldRecord | null;
  chat_holds?: ProjectHoldRecord[];
  active_turns: number;
  running_goals: number;
}

/** One held chat: its own hold, or the hold of the chat it reports to. */
export interface ChatHold {
  id: string;
  sessionId: string;
  batchId?: string;
  reason: string;
  createdAt: string;
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
  /** Chats on hold while the project stays open. */
  chatHolds: ChatHold[];
}

export const OPEN_PROJECT: ProjectHoldState = { held: false, pausedGoals: 0, activeTurns: 0, chatHolds: [] };
/** Polls while a project is held: the grace countdown and release are seen quickly. */
export const HELD_POLL_MS = 5_000;
/** Polls otherwise: a hold placed for a deployment shows within this interval. */
export const OPEN_POLL_MS = 30_000;

export function chatHolds(view: ProjectHoldView | undefined): ChatHold[] {
  return (view?.chat_holds ?? [])
    .filter(hold => hold.session_id && hold.status !== "released")
    .map(hold => ({
      id: hold.id,
      sessionId: hold.session_id as string,
      batchId: hold.batch_id ?? undefined,
      reason: hold.reason,
      createdAt: hold.created_at,
    }));
}

/**
 * The chat hold that covers `sessionId`: its own, or one on a chat it reports
 * to. `parentOf` walks subagent and goal-child links only (never forks).
 */
export function chatHoldFor(
  holds: readonly ChatHold[],
  sessionId: string | undefined,
  parentOf: (sessionId: string) => string | undefined = () => undefined,
): ChatHold | undefined {
  const seen = new Set<string>();
  for (let current = sessionId; current && !seen.has(current); current = parentOf(current)) {
    seen.add(current);
    const hold = holds.find(item => item.sessionId === current);
    if (hold) return hold;
  }
  return undefined;
}

export interface ChatHoldPlacement {
  batchId?: string;
  sessionIds: string[];
}

export function holdState(view: ProjectHoldView | undefined): ProjectHoldState {
  const hold = view?.hold;
  const chats = chatHolds(view);
  if (!view?.held || !hold || hold.status === "released") return chats.length ? { ...OPEN_PROJECT, chatHolds: chats } : OPEN_PROJECT;
  return {
    held: true,
    phase: hold.status,
    holdId: hold.id,
    reason: hold.reason,
    graceUntil: hold.grace_until ?? undefined,
    pausedGoals: hold.paused_goals.length,
    activeTurns: view.active_turns,
    chatHolds: chats,
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

  /**
   * Hold chosen chats, or every chat with work running now. Acts at once: no
   * confirmation, since release (or Undo) restores exactly what was held.
   */
  async holdChats(
    projectId: string,
    choice: { sessionIds?: string[]; allActive?: boolean; reason?: string },
  ): Promise<ChatHoldPlacement> {
    const view = await this.request<ProjectHoldView & { batch_id?: string | null; holds?: ProjectHoldRecord[] }>(
      `engagements/${encodeURIComponent(projectId)}/holds`,
      {
        method: "POST",
        body: JSON.stringify({
          session_ids: choice.sessionIds ?? [],
          all_active: choice.allActive === true,
          ...(choice.reason ? { reason: choice.reason } : {}),
        }),
      },
    );
    this.publish(projectId, holdState(view));
    return {
      batchId: view.batch_id ?? undefined,
      sessionIds: (view.holds ?? []).map(hold => hold.session_id).filter((id): id is string => Boolean(id)),
    };
  }

  /** Release a batch, or the holds on chosen chats; Core resumes what each paused. */
  async releaseChats(projectId: string, choice: { batchId?: string; sessionIds?: string[] }): Promise<ProjectHoldState> {
    const view = await this.request<ProjectHoldView>(`engagements/${encodeURIComponent(projectId)}/holds/release`, {
      method: "POST",
      body: JSON.stringify({
        ...(choice.batchId ? { batch_id: choice.batchId } : {}),
        session_ids: choice.sessionIds ?? [],
        reason: "Released from the interface",
      }),
    });
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
