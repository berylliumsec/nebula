export interface ChatScrollGeometry {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** The active response starts at the latest assistant message, after older turns. */
export function currentAgentTurnIndex(messages: ReadonlyArray<{role: string}>): number {
  if (!messages.length) return -1;
  if (messages[messages.length - 1].role === "user") return messages.length - 1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "assistant") return index;
  }
  return messages.length - 1;
}

/** Layout changes must not cancel the operator's intent to follow the transcript. */
export function followsChatBottom(previous: ChatScrollGeometry | undefined, current: ChatScrollGeometry, following: boolean): boolean {
  if (current.scrollHeight - current.scrollTop - current.clientHeight <= 4) return true;
  // Moving up away from the bottom is reader intent even while streamed content grows,
  // but not when the viewport itself resized: a status strip appearing below the
  // transcript can nudge scrollTop in the same frame without any reader input.
  const viewportResized = previous !== undefined && previous.clientHeight !== current.clientHeight;
  if (previous && !viewportResized && current.scrollTop < previous.scrollTop - 4) return false;
  if (following && (!previous || previous.scrollHeight !== current.scrollHeight || previous.clientHeight !== current.clientHeight)) return true;
  return false;
}
