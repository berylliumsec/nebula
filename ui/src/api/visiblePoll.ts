/**
 * Polling for surfaces an operator is looking at.
 *
 * Core owns conversation state and the browser reads it back. A background tab
 * or a phone with the screen off is not looking, so its polls wait until the
 * page is visible again and then read immediately. A read that finds nothing
 * new backs off, doubling its wait up to a ceiling; any change, activity or an
 * explicit poke returns to the base cadence, so live work stays responsive.
 */

/** What one read found: keep the base cadence, back off, or stop polling. */
export type PollOutcome = "active" | "idle" | "stop";

export interface VisiblePollOptions {
  /** One read. Resolve "idle" when nothing changed and no work is running. */
  read: (signal: AbortSignal) => Promise<PollOutcome | void>;
  /** The cadence while something changes or runs. */
  intervalMs: number;
  /** Idle reads double the wait up to this; omit it to keep a fixed cadence. */
  maxIntervalMs?: number;
  /** Aborting it stops the poll and removes its listeners. */
  signal: AbortSignal;
  /** Read once as soon as the poll starts (the default). */
  immediate?: boolean;
}

export interface VisiblePoll {
  /** Read now and return to the base cadence, for example after a stream event. */
  poke: () => void;
  /** Return to the base cadence without reading now, for example after a mutation. */
  reset: () => void;
}

export function pageHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function startVisiblePoll(options: VisiblePollOptions): VisiblePoll {
  const { read, intervalMs, maxIntervalMs = intervalMs, signal, immediate = true } = options;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let delay = intervalMs;
  let reading = false;
  let rerun = false;
  let stopped = false;
  // A poll that came due while the page was hidden reads when it returns.
  let due = false;

  const clear = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  const schedule = () => {
    clear();
    if (signal.aborted || stopped) return;
    timer = setTimeout(() => {
      timer = undefined;
      if (pageHidden()) { due = true; return; }
      void run();
    }, delay);
  };
  const run = async (): Promise<void> => {
    if (signal.aborted || stopped) return;
    if (reading) { rerun = true; return; }
    if (pageHidden()) { due = true; clear(); return; }
    due = false;
    reading = true;
    let outcome: PollOutcome | void = "active";
    try {
      outcome = await read(signal);
    } catch {
      // diagnostic-expected: each read reports its own failure; the poll keeps its cadence.
      outcome = "active";
    } finally {
      reading = false;
    }
    if (signal.aborted) return;
    if (outcome === "stop") { stopped = true; clear(); return; }
    delay = outcome === "idle" ? Math.min(delay * 2, Math.max(intervalMs, maxIntervalMs)) : intervalMs;
    if (rerun) { rerun = false; delay = intervalMs; void run(); return; }
    schedule();
  };
  const visibility = () => {
    if (pageHidden()) {
      // Nothing is read while nobody is looking.
      if (timer !== undefined) { clear(); due = true; }
      return;
    }
    if (!due) return;
    delay = intervalMs;
    void run();
  };
  const dispose = () => { clear(); document.removeEventListener("visibilitychange", visibility); };
  signal.addEventListener("abort", dispose, { once: true });
  document.addEventListener("visibilitychange", visibility);
  if (immediate) void run();
  else schedule();
  return {
    poke: () => {
      if (signal.aborted || stopped) return;
      delay = intervalMs;
      clear();
      void run();
    },
    reset: () => {
      if (signal.aborted || stopped || delay === intervalMs) return;
      delay = intervalMs;
      if (!reading && timer !== undefined) schedule();
    },
  };
}

/**
 * Keep the previous value when a read returns the same content, so React
 * state (and every component reading it) does not change on an idle poll.
 */
export function sameJson(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    // diagnostic-expected: an unserializable value is treated as changed.
    return false;
  }
}
