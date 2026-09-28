import { describe, expect, it } from "vitest";
import { emptyAssistantDefaults, preferredAssistantRuntime } from "./assistantDefaults";
import type { HarnessProfile, ProviderHealth } from "./types";

const provider = {
  id: "provider-1", enabled: true, state: "healthy", models: ["first", "chosen"],
  defaultModel: "first", effectiveDefaultModel: "first",
} as ProviderHealth;
const harness = {
  id: "harness-1", enabled: true, healthy: true, models: ["harness-first", "harness-chosen"],
  defaultModel: "harness-first",
} as HarnessProfile;

describe("preferredAssistantRuntime", () => {
  it("restores the selected provider and model ahead of runtime discovery", () => {
    const defaults = { ...emptyAssistantDefaults(), backend: "provider" as const, providerId: provider.id, model: "chosen" };
    expect(preferredAssistantRuntime(defaults, [provider], [harness]))
      .toEqual({ kind: "provider", id: provider.id, model: "chosen" });
  });

  it("uses a usable fallback when the saved runtime or model disappears", () => {
    const defaults = { ...emptyAssistantDefaults(), backend: "provider" as const, providerId: provider.id, model: "retired" };
    expect(preferredAssistantRuntime(defaults, [provider], [harness]))
      .toEqual({ kind: "provider", id: provider.id, model: "first" });
    expect(preferredAssistantRuntime(defaults, [], [harness]))
      .toEqual({ kind: "harness", id: harness.id, model: "harness-first" });
  });

  it("restores a selected harness model", () => {
    const defaults = { ...emptyAssistantDefaults(), backend: "harness" as const, harnessId: harness.id, model: "harness-chosen" };
    expect(preferredAssistantRuntime(defaults, [provider], [harness]))
      .toEqual({ kind: "harness", id: harness.id, model: "harness-chosen" });
  });
});
