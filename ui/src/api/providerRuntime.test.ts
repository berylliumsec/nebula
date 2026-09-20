import { describe, expect, it } from "vitest";
import { providerWithDiscoveredModels, providersWithDiscoveredModels } from "./providerRuntime";
import type { ProviderHealth } from "./types";

function profile(overrides: Partial<ProviderHealth> = {}): ProviderHealth {
  return {
    id: "provider-1",
    revision: 2,
    name: "Local acceptance",
    providerType: "vllm",
    kind: "local",
    local: true,
    state: "unchecked",
    enabled: true,
    endpoint: "http://127.0.0.1:8000/v1",
    models: [],
    availableModels: [],
    modelAllowlist: [],
    defaultModel: "security-model-reasoning",
    effectiveDefaultModel: "security-model-reasoning",
    permitsSensitiveData: false,
    autoShareToolResults: false,
    residency: [],
    options: {},
    metadata: { default_model: "security-model-reasoning" },
    modelCount: 0,
    privacy: "local_only",
    capabilities: [],
    message: "Profile loaded; run a health check to discover available models.",
    ...overrides,
  };
}

function checked(overrides: Partial<ProviderHealth> = {}): ProviderHealth {
  return profile({
    revision: 1,
    state: "healthy",
    models: ["security-model", "security-model-reasoning"],
    availableModels: ["security-model", "security-model-reasoning"],
    modelDescriptors: [{ id: "security-model", name: "Security model", description: null, canonicalSlug: null, contextWindow: 8_000, maxOutputTokens: 1_000, inputModalities: ["text"], outputModalities: ["text"], supportedParameters: [], pricing: {} }],
    modelCount: 2,
    lastCheckedAt: "2026-09-19T10:00:00Z",
    latencyMs: 12,
    message: "2 models discovered. Select a model to use this provider.",
    ...overrides,
  });
}

describe("providerWithDiscoveredModels", () => {
  it("keeps discovered models when a profile read follows a health check", () => {
    const folded = providerWithDiscoveredModels(checked(), profile({ revision: 3 }));
    expect(folded.revision).toBe(3);
    expect(folded.models).toEqual(["security-model", "security-model-reasoning"]);
    expect(folded.availableModels).toEqual(["security-model", "security-model-reasoning"]);
    expect(folded.modelDescriptors).toHaveLength(1);
    expect(folded.modelCount).toBe(2);
    expect(folded.state).toBe("healthy");
    expect(folded.lastCheckedAt).toBe("2026-09-19T10:00:00Z");
    expect(folded.message).toBe("2 models discovered. Select a model to use this provider.");
  });

  it("re-derives the selectable models from a narrowed allowed list", () => {
    const folded = providerWithDiscoveredModels(checked(), profile({ modelAllowlist: ["security-model-reasoning"] }));
    expect(folded.models).toEqual(["security-model-reasoning"]);
    expect(folded.availableModels).toEqual(["security-model", "security-model-reasoning"]);
    expect(folded.modelCount).toBe(1);
    expect(folded.message).toBe("1 model available from the last health check.");
  });

  it("reports when no discovered model is allowed any more", () => {
    const folded = providerWithDiscoveredModels(checked(), profile({ modelAllowlist: ["retired-model"] }));
    expect(folded.models).toEqual([]);
    expect(folded.message).toBe("The last health check reported no model in this profile's allowed models.");
  });

  it("does not let a failed health check stick to a later profile read", () => {
    const failed = checked({ state: "degraded", message: "Provider health check failed." });
    const folded = providerWithDiscoveredModels(failed, profile({ revision: 3 }));
    expect(folded.state).toBe("unchecked");
    expect(folded.models).toEqual(["security-model", "security-model-reasoning"]);
    expect(folded.message).toBe("2 models available from the last health check.");
  });

  it("lets a disabled profile own its own state while keeping the catalog", () => {
    const folded = providerWithDiscoveredModels(checked(), profile({ enabled: false, state: "offline", message: "Provider profile is disabled." }));
    expect(folded.state).toBe("offline");
    expect(folded.message).toBe("Provider profile is disabled.");
    expect(folded.models).toEqual(["security-model", "security-model-reasoning"]);
  });

  it("leaves a re-enabled profile unchecked without discarding its models", () => {
    const disabled = checked({ enabled: false, state: "offline", message: "Provider profile is disabled." });
    const folded = providerWithDiscoveredModels(disabled, profile({ revision: 4 }));
    expect(folded.state).toBe("unchecked");
    expect(folded.models).toEqual(["security-model", "security-model-reasoning"]);
    expect(folded.message).toBe("2 models available from the last health check.");
  });

  it("drops discovery that belongs to a different endpoint", () => {
    const folded = providerWithDiscoveredModels(checked(), profile({ endpoint: "http://127.0.0.1:9000/v1" }));
    expect(folded.models).toEqual([]);
    expect(folded.state).toBe("unchecked");
    expect(folded.lastCheckedAt).toBeUndefined();
  });

  it("uses the profile read when nothing was discovered yet", () => {
    expect(providerWithDiscoveredModels(undefined, profile({ modelAllowlist: ["saved"], models: ["saved"] })).models).toEqual(["saved"]);
    expect(providerWithDiscoveredModels(profile(), profile()).lastCheckedAt).toBeUndefined();
  });

  it("folds a provider list without touching providers it never checked", () => {
    const listed = [profile({ revision: 3 }), profile({ id: "provider-2", models: ["listed"], modelAllowlist: ["listed"] })];
    const folded = providersWithDiscoveredModels([checked()], listed);
    expect(folded[0].models).toEqual(["security-model", "security-model-reasoning"]);
    expect(folded[1].models).toEqual(["listed"]);
  });
});
