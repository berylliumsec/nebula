import type { ChatCompletionRequest } from "../api/types";

export function detachHarnessStream(
  controller: AbortController | undefined,
  backend: ChatCompletionRequest["backend"] | undefined,
  detachedStreams: WeakSet<AbortController>,
): boolean {
  if (!controller || backend !== "harness") return false;
  detachedStreams.add(controller);
  controller.abort();
  return true;
}
