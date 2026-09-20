/** How long a turn took, in the two shapes the transcript needs. */

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;

function pad(value: number): string {
  return value.toString().padStart(2, "0");
}

/** A finished turn: 8.2s under a minute, 4m 12s under an hour, 1h 04m above. */
export function formatTurnElapsed(ms: number): string {
  const safe = Number.isFinite(ms) && ms > 0 ? ms : 0;
  if (safe < MINUTE) return `${(safe / SECOND).toFixed(1)}s`;
  if (safe < HOUR) return `${Math.floor(safe / MINUTE)}m ${pad(Math.floor((safe % MINUTE) / SECOND))}s`;
  return `${Math.floor(safe / HOUR)}h ${pad(Math.floor((safe % HOUR) / MINUTE))}m`;
}

/** A running turn, counting up like a stopwatch: 0:12, 12:05, 1:02:31. */
export function formatLiveElapsed(ms: number): string {
  const safe = Number.isFinite(ms) && ms > 0 ? ms : 0;
  const seconds = Math.floor((safe % MINUTE) / SECOND);
  if (safe < HOUR) return `${Math.floor(safe / MINUTE)}:${pad(seconds)}`;
  return `${Math.floor(safe / HOUR)}:${pad(Math.floor((safe % HOUR) / MINUTE))}:${pad(seconds)}`;
}

/** The line the expanded usage adds; the approval wait is part of the elapsed. */
export function elapsedDetail(elapsedMs?: number, approvalWaitMs?: number): string | undefined {
  if (elapsedMs === undefined) return undefined;
  const elapsed = `${formatTurnElapsed(elapsedMs)} elapsed`;
  if (!approvalWaitMs) return elapsed;
  return `${elapsed} · ${formatTurnElapsed(approvalWaitMs)} waiting for approval`;
}

/** Milliseconds since an ISO timestamp, or undefined when it cannot be read. */
export function elapsedSince(startedAt: string, now = Date.now()): number | undefined {
  const started = Date.parse(startedAt);
  return Number.isFinite(started) ? Math.max(0, now - started) : undefined;
}
