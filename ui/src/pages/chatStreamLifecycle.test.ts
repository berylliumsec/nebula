import { describe, expect, it, vi } from "vitest";
import type { ChatCompletionRequest } from "../api/types";
import { beginGuardedStream, detachChatStream } from "./chatStreamLifecycle";

describe("detachChatStream", () => {
  it("aborts only the viewer transport and records a harness detachment", () => {
    const controller = new AbortController();
    const detached = new WeakSet<AbortController>();

    expect(detachChatStream(controller, "harness", detached)).toBe(true);
    expect(controller.signal.aborted).toBe(true);
    expect(detached.has(controller)).toBe(true);
  });

  it("detaches provider viewer streams without treating them as operator stops", () => {
    const controller = new AbortController();
    const detached = new WeakSet<AbortController>();

    expect(detachChatStream(controller, "provider", detached)).toBe(true);
    expect(controller.signal.aborted).toBe(true);
    expect(detached.has(controller)).toBe(true);
  });
});

describe("beginGuardedStream", () => {
  function refs() {
    return {
      generation: { current: 3 },
      abort: { current: undefined as AbortController | undefined },
      backend: { current: undefined as ChatCompletionRequest["backend"] | undefined },
    };
  }

  it("owns the viewer transport and drops events after the conversation changes", () => {
    const current = refs();
    const stream = beginGuardedStream(current, "provider");
    const onEvent = vi.fn();

    expect(current.abort.current).toBe(stream.controller);
    expect(current.backend.current).toBe("provider");
    stream.guard(onEvent)("started");
    expect(onEvent).toHaveBeenCalledWith("started");

    current.generation.current += 1;
    stream.guard(onEvent)("delta");
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(stream.isCurrent()).toBe(false);
  });

  it("drops events once the transport is detached while the selection stays current", () => {
    const current = refs();
    const stream = beginGuardedStream(current, "provider");
    const onEvent = vi.fn();

    detachChatStream(current.abort.current, current.backend.current, new WeakSet());
    stream.guard(onEvent)("delta");
    expect(onEvent).not.toHaveBeenCalled();
    expect(stream.controller.signal.aborted).toBe(true);
    expect(stream.isCurrent()).toBe(true);
  });

  it("releases only the transport it still owns", () => {
    const current = refs();
    const first = beginGuardedStream(current, "provider");
    const second = beginGuardedStream(current, "harness");

    first.release();
    expect(current.abort.current).toBe(second.controller);
    expect(current.backend.current).toBe("harness");
    second.release();
    expect(current.abort.current).toBeUndefined();
    expect(current.backend.current).toBeUndefined();
  });
});
