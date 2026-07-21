import { describe, expect, it } from "vitest";
import { defaultModelRuntime } from "./runtimeDefaults";

describe("defaultModelRuntime", () => {
  it("prefers the first working harness and its first discovered model", () => {
    expect(defaultModelRuntime(
      [{ id: "provider-1", enabled: true, state: "healthy", models: ["provider-model"] }],
      [
        { id: "broken-harness", enabled: true, healthy: false, models: ["stale-model"] },
        { id: "harness-1", enabled: true, healthy: true, models: ["harness-first", "harness-second"] },
      ],
    )).toEqual({ kind: "harness", id: "harness-1", model: "harness-first" });
  });

  it("falls back to the first usable provider and its first model", () => {
    expect(defaultModelRuntime(
      [
        { id: "offline", enabled: true, state: "offline", models: ["stale-model"] },
        { id: "degraded", enabled: true, state: "degraded", models: ["uncertain-model"] },
        { id: "provider-1", enabled: true, state: "unchecked", models: ["provider-first", "provider-second"] },
      ],
      [{ id: "unchecked-harness", enabled: true, healthy: false, models: ["harness-model"] }],
    )).toEqual({ kind: "provider", id: "provider-1", model: "provider-first" });
  });

  it("returns no default when configured runtimes are not usable", () => {
    expect(defaultModelRuntime(
      [{ id: "provider-1", enabled: true, state: "healthy", models: [] }],
      [{ id: "harness-1", enabled: false, healthy: true, models: ["model"] }],
    )).toBeUndefined();
  });
});
