import { describe, expect, it } from "vitest";
import { detachHarnessStream } from "./chatStreamLifecycle";

describe("detachHarnessStream", () => {
  it("aborts only the viewer transport and records a harness detachment", () => {
    const controller = new AbortController();
    const detached = new WeakSet<AbortController>();

    expect(detachHarnessStream(controller, "harness", detached)).toBe(true);
    expect(controller.signal.aborted).toBe(true);
    expect(detached.has(controller)).toBe(true);
  });

  it("does not detach provider streams", () => {
    const controller = new AbortController();
    const detached = new WeakSet<AbortController>();

    expect(detachHarnessStream(controller, "provider", detached)).toBe(false);
    expect(controller.signal.aborted).toBe(false);
    expect(detached.has(controller)).toBe(false);
  });
});
