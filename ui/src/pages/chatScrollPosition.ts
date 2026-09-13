export interface ChatScrollGeometry {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** Layout changes must not cancel the operator's intent to follow the transcript. */
export function followsChatBottom(previous: ChatScrollGeometry | undefined, current: ChatScrollGeometry, following: boolean): boolean {
  if (current.scrollHeight - current.scrollTop - current.clientHeight <= 4) return true;
  if (following && (!previous || previous.scrollHeight !== current.scrollHeight || previous.clientHeight !== current.clientHeight)) return true;
  return false;
}
