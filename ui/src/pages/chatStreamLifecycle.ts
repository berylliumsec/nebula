import type { ChatCompletionRequest } from "../api/types";

export function detachChatStream(
  controller: AbortController | undefined,
  _backend: ChatCompletionRequest["backend"] | undefined,
  detachedStreams: WeakSet<AbortController>,
): boolean {
  if (!controller) return false;
  detachedStreams.add(controller);
  controller.abort();
  return true;
}
