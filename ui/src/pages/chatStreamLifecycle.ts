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

export interface GuardedStreamRefs {
  /** Advances whenever the operator selects a different conversation. */
  generation: { current: number };
  abort: { current: AbortController | undefined };
  backend: { current: ChatCompletionRequest["backend"] | undefined };
}

export interface GuardedStream {
  controller: AbortController;
  /** True while the conversation that started this stream is still selected. */
  isCurrent(): boolean;
  /** Runs the callback only while the selection is current and the transport is attached. */
  guard<Args extends unknown[]>(callback: (...args: Args) => void): (...args: Args) => void;
  /** Hands the viewer transport back if this stream still owns it. */
  release(): void;
}

/**
 * Registers a resumed or followed stream as the active viewer transport so a
 * conversation switch detaches it, and gates its callbacks on the selection
 * that started it. Core replays the turn ledger from sequence 0 on follow, so
 * an unguarded stream would land the earlier conversation's events, and its
 * final message, in whichever transcript is open when they arrive.
 */
export function beginGuardedStream(refs: GuardedStreamRefs, backend: ChatCompletionRequest["backend"]): GuardedStream {
  const generation = refs.generation.current;
  const controller = new AbortController();
  refs.abort.current = controller;
  refs.backend.current = backend;
  const isCurrent = () => refs.generation.current === generation;
  return {
    controller,
    isCurrent,
    guard: (callback) => (...args) => {
      if (isCurrent() && !controller.signal.aborted) callback(...args);
    },
    release: () => {
      if (refs.abort.current !== controller) return;
      refs.abort.current = undefined;
      refs.backend.current = undefined;
    },
  };
}
