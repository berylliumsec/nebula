import type { SessionState } from "./useSessionState";

/**
 * Core starts and resumes provider turns that no viewer on this page
 * submitted: a running goal's next turn, a schedule, an approval decided on
 * another device, recovery after a restart. The session snapshot is the
 * authority for which turn is active, so the page decides from it whether to
 * attach a viewer or to reload a transcript that misses a settled turn.
 */
export type ProviderTurnFollowAction = "attach" | "reload";

/** Active executions the restore path can follow, or restore a wait or recovery for. */
const ATTACHABLE = new Set(["running", "queued", "finalizing", "waiting_callback", "recovering", "needs_stop"]);
/** Settled executions whose saved answer or outcome note belongs in the transcript. */
const SETTLED = new Set(["complete", "failed", "cancelled"]);

export interface ProviderTurnFollowInput {
  snapshot: Pick<SessionState, "turn_id" | "busy" | "execution" | "pending">;
  /**
   * Something on the page already presents the turn: a stream it follows, an
   * approval decision in flight, a callback or recovery wait, a live approval
   * card, or the retry of a failed answer.
   */
  owned: boolean;
  /** Turns whose end the page saw on a stream it followed. */
  finished: ReadonlySet<string>;
  /** Whether the transcript already holds the turn's saved answer or outcome note. */
  shown: (turnId: string) => boolean;
}

export function providerTurnFollowAction({snapshot, owned, finished, shown}: ProviderTurnFollowInput): ProviderTurnFollowAction | undefined {
  const turnId = snapshot.turn_id;
  if (!turnId || owned || finished.has(turnId)) return undefined;
  // A pending approval restores its card through the approval authority.
  if (snapshot.pending.length) return undefined;
  if (snapshot.busy) return ATTACHABLE.has(snapshot.execution) ? "attach" : undefined;
  return SETTLED.has(snapshot.execution) && !shown(turnId) ? "reload" : undefined;
}

interface TranscriptMessage {
  metadata?: Record<string, unknown>;
  outcomeTurnId?: string;
}

/** A settled provider turn is shown by its saved answer or by Core's outcome note. */
export function transcriptShowsTurn(messages: readonly TranscriptMessage[], turnId: string): boolean {
  return messages.some((message) => message.outcomeTurnId === turnId || message.metadata?.chat_turn_id === turnId);
}
