/**
 * Project reads while a project's execution worker is away.
 *
 * A project's worker retires when the project goes idle and Core readmits one
 * when the operator acts again (unarchive, a project command, a new chat).
 * Meanwhile, and while the new worker starts, project reads answer 503
 * `project_runtime_unavailable`. Nothing failed and nothing is lost, so a view
 * that hits this waits here instead of showing an error, and reads again as
 * soon as one of the failed reads answers.
 *
 * Only waiting views keep the probe alive: one GET of the most recent failed
 * read, honouring Retry-After and backing off to five seconds, paused while the
 * page is hidden. Core says why the worker is away: "starting" and "closing"
 * end on their own, while an "idle" project waits for the operator to resume
 * work in it (reads never admit a worker), so it is probed less often and
 * described differently. A write from this page (unarchive, a project command,
 * a message) may be what readmits the worker, so it restarts the backoff.
 */

export const PROJECT_RUNTIME_UNAVAILABLE = "project_runtime_unavailable";

const BASE_DELAY_MS = 1_000;
const MAX_DELAY_MS = 5_000;
const IDLE_DELAY_MS = 10_000;
const MAX_RETRY_AFTER_MS = 10_000;
const MAX_TRACKED_READS = 16;

/** Why no ready worker owns the project. */
export type ProjectRuntimeReason = "starting" | "closing" | "idle";
/** "reconnecting" or "idle" while a view waits; "available" otherwise. */
export type ProjectRuntimeState = "available" | "reconnecting" | "idle";

export interface ProjectRuntimeProbeResult {
  /** The runtime answered the read (with anything but runtime unavailability). */
  answered: boolean;
  retryAfterMs?: number;
  reason?: ProjectRuntimeReason;
}

export type ProjectRuntimeProbe = (path: string, signal: AbortSignal) => Promise<ProjectRuntimeProbeResult>;

/**
 * True when a read failed only because no ready worker owns its project yet.
 * Core marks exactly these 503s; a failed worker or a Core outage is not one.
 */
export function isProjectRuntimeUnavailable(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as { status?: unknown; code?: unknown };
  return value.status === 503 && value.code === PROJECT_RUNTIME_UNAVAILABLE;
}

/** Core's reason for a project runtime 503; "starting" when it gave none. */
export function projectRuntimeReason(error: unknown): ProjectRuntimeReason {
  const details = error && typeof error === "object" ? (error as { details?: unknown }).details : undefined;
  const reason = details && typeof details === "object" ? (details as { reason?: unknown }).reason : undefined;
  return reason === "idle" || reason === "closing" ? reason : "starting";
}

/** Milliseconds from a Retry-After header (delta seconds or an HTTP date). */
export function parseRetryAfter(value: string | null | undefined, now = Date.now()): number | undefined {
  if (!value) return undefined;
  const text = value.trim();
  if (/^\d+$/.test(text)) return Number(text) * 1000;
  const date = Date.parse(text);
  return Number.isFinite(date) ? Math.max(0, date - now) : undefined;
}

/** 1 s, 2 s, 4 s, then 5 s (10 s for an idle project); a longer Retry-After (up to 10 s) is honoured. */
export function projectRuntimeRetryDelay(attempt: number, retryAfterMs?: number, reason: ProjectRuntimeReason = "starting"): number {
  const ceiling = reason === "idle" ? IDLE_DELAY_MS : MAX_DELAY_MS;
  const backoff = Math.min(ceiling, BASE_DELAY_MS * 2 ** Math.max(0, Math.min(attempt, 8)));
  const floor = retryAfterMs !== undefined && Number.isFinite(retryAfterMs)
    ? Math.min(MAX_RETRY_AFTER_MS, Math.max(0, retryAfterMs))
    : 0;
  return Math.max(backoff, floor);
}

/** The exact read, query included: another project's read of the same route must not count. */
function readKey(path: string): string {
  return path.replace(/^\//, "").split("#", 1)[0];
}

function pageHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

function aborted(): DOMException {
  return new DOMException("The view stopped waiting for the project runtime.", "AbortError");
}

export class ProjectRuntimeRecovery {
  private readonly failed = new Map<string, { path: string; retryAfterMs?: number; reason: ProjectRuntimeReason }>();
  private readonly waiters = new Set<() => void>();
  private readonly listeners = new Set<(state: ProjectRuntimeState) => void>();
  private timer?: ReturnType<typeof setTimeout>;
  private probing?: AbortController;
  private attempt = 0;
  private watchingVisibility = false;
  private lastState: ProjectRuntimeState = "available";

  constructor(private readonly probe: ProjectRuntimeProbe) {}

  /** While a view waits for a project read Core could not route yet: "reconnecting", or "idle" when nothing will start the worker until the operator resumes work. */
  get state(): ProjectRuntimeState {
    if (!this.failed.size || !this.waiters.size) return "available";
    return this.latest()?.reason === "idle" ? "idle" : "reconnecting";
  }

  subscribe(listener: (state: ProjectRuntimeState) => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  /** A project read answered 503 project_runtime_unavailable. */
  unavailable(path: string, retryAfterMs?: number, reason: ProjectRuntimeReason = "starting"): void {
    const key = readKey(path);
    this.failed.delete(key);
    this.failed.set(key, { path, retryAfterMs, reason });
    while (this.failed.size > MAX_TRACKED_READS) this.failed.delete(this.failed.keys().next().value!);
    this.notify();
    this.schedule();
  }

  /** This page sent a write, which may readmit the worker: probe again soon. */
  nudge(): void {
    if (!this.failed.size || !this.waiters.size) return;
    this.attempt = 0;
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    this.schedule();
  }

  /** A read answered with anything else; if it had failed, its runtime is back. */
  answered(path: string): void {
    if (this.failed.has(readKey(path))) this.recovered();
  }

  /**
   * Resolve once a failed project read answers again. While any view waits,
   * the most recent failed read is retried in the background.
   */
  waitForRuntime(signal?: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      if (signal?.aborted) { reject(aborted()); return; }
      const done = () => { signal?.removeEventListener("abort", abort); resolve(); };
      const abort = () => {
        this.waiters.delete(done);
        this.notify();
        if (!this.waiters.size) this.stop();
        reject(aborted());
      };
      signal?.addEventListener("abort", abort, { once: true });
      this.waiters.add(done);
      // A success already cleared every failed read: read again shortly. The
      // delay keeps a caller whose failure was not recorded from spinning.
      if (!this.failed.size) { setTimeout(() => { if (this.waiters.delete(done)) { this.notify(); done(); } }, BASE_DELAY_MS); return; }
      this.notify();
      this.schedule();
    });
  }

  private latest() {
    return [...this.failed.values()].at(-1);
  }

  private notify(): void {
    const state = this.state;
    if (state === this.lastState) return;
    this.lastState = state;
    for (const listener of [...this.listeners]) listener(state);
  }

  private schedule(): void {
    if (this.timer !== undefined || this.probing) return;
    if (!this.failed.size || !this.waiters.size) { this.stop(); return; }
    if (pageHidden()) { this.watchVisibility(); return; }
    const latest = this.latest();
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.run();
    }, projectRuntimeRetryDelay(this.attempt, latest?.retryAfterMs, latest?.reason));
  }

  private async run(): Promise<void> {
    const latest = this.latest();
    if (!latest || !this.waiters.size) return;
    if (pageHidden()) { this.watchVisibility(); return; }
    const controller = new AbortController();
    this.probing = controller;
    let result: ProjectRuntimeProbeResult = { answered: false };
    try {
      result = await this.probe(latest.path, controller.signal);
    } catch {
      // diagnostic-expected: an unreachable Core is still waiting; the Core connection monitor reports it and this probe backs off.
      result = { answered: false };
    } finally {
      if (this.probing === controller) this.probing = undefined;
    }
    if (controller.signal.aborted) return;
    if (result.answered) { this.recovered(); return; }
    const current = this.failed.get(readKey(latest.path));
    if (current) {
      if (result.retryAfterMs !== undefined) current.retryAfterMs = result.retryAfterMs;
      if (result.reason) current.reason = result.reason;
      this.notify();
    }
    this.attempt += 1;
    this.schedule();
  }

  private recovered(): void {
    this.failed.clear();
    this.attempt = 0;
    this.stop();
    const waiting = [...this.waiters];
    this.waiters.clear();
    this.notify();
    for (const done of waiting) done();
  }

  private stop(): void {
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    this.probing?.abort();
    this.probing = undefined;
    if (this.watchingVisibility && typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.visible);
      this.watchingVisibility = false;
    }
  }

  private watchVisibility(): void {
    if (this.watchingVisibility || typeof document === "undefined") return;
    this.watchingVisibility = true;
    document.addEventListener("visibilitychange", this.visible);
  }

  private readonly visible = () => {
    if (pageHidden()) return;
    document.removeEventListener("visibilitychange", this.visible);
    this.watchingVisibility = false;
    // Returning to the page reads at once, then resumes the backoff.
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    void this.run();
  };
}
