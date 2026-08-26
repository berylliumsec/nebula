import { describe, expect, it } from "vitest";
import { detachChatStream } from "./chatStreamLifecycle";

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
