import { describe, expect, it } from "vitest";
import { defaultModelRuntime, providerDefaultModel } from "./runtimeDefaults";

describe("defaultModelRuntime", () => {
  it.each([["second", ["first", "second"]], ["configured", []], ["configured", ["stale"]]])("honors configured harness default %s even when discovery is incomplete", (defaultModel, models) => {
    expect(defaultModelRuntime([], [{ id: "harness", enabled: true, healthy: true, defaultModel, models }]))
      .toEqual({ kind: "harness", id: "harness", model: defaultModel });
  });

  it("ignores blank defaults and unhealthy configured harnesses", () => {
    expect(defaultModelRuntime([], [
      { id: "offline", enabled: true, healthy: false, defaultModel: "configured", models: [] },
      { id: "ready", enabled: true, healthy: true, defaultModel: "  ", models: ["discovered"] },
    ])).toEqual({ kind: "harness", id: "ready", model: "discovered" });
  });
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

describe("providerDefaultModel", () => {
  it("keeps the saved default that Settings recorded", () => {
    expect(providerDefaultModel({
      defaultModel: "security-model-reasoning",
      models: ["security-model", "security-model-reasoning"],
    })).toBe("security-model-reasoning");
  });

  it("falls back to the first discovered model when no default is saved", () => {
    expect(providerDefaultModel({ models: ["security-model", "security-model-reasoning"] }))
      .toBe("security-model");
  });

  it("uses the allowed model Core reports when the profile saved no default", () => {
    expect(providerDefaultModel({ effectiveDefaultModel: "allowed-model", models: ["discovered", "allowed-model"] }))
      .toBe("allowed-model");
  });

  it("ignores a saved default the runtime no longer reports", () => {
    expect(providerDefaultModel({ defaultModel: "retired-model", models: ["security-model"] }))
      .toBe("security-model");
  });

  it("has no model to offer before discovery", () => {
    expect(providerDefaultModel({ defaultModel: "security-model", models: [] })).toBe("");
    expect(providerDefaultModel()).toBe("");
  });
});
