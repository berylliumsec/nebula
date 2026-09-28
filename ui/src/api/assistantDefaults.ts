import type { AssistantDefaults, HarnessProfile, ProviderHealth } from "./types";
import { defaultModelRuntime, providerDefaultModel, type DefaultModelRuntime } from "./runtimeDefaults";

export function emptyAssistantDefaults(): AssistantDefaults {
  return {
    backend: null,
    providerId: null,
    harnessId: null,
    model: null,
    reasoningEffort: null,
    harnessReasoningEffort: null,
    harnessServiceTier: null,
    harnessMode: null,
    mcpServerIds: [],
    hookIds: [],
    allowSubagents: false,
    subagentProviderId: null,
    subagentModel: null,
    maxActiveSubagents: null,
    subagentReasoningEffort: null,
    allowAgentMessaging: false,
  };
}

export function preferredAssistantRuntime(
  defaults: AssistantDefaults,
  providers: readonly ProviderHealth[],
  harnesses: readonly HarnessProfile[],
): DefaultModelRuntime | undefined {
  if (defaults.backend === "provider") {
    const provider = providers.find((item) => item.id === defaults.providerId && item.enabled);
    if (provider && (provider.state === "healthy" || provider.state === "unchecked")) {
      const model = defaults.model && provider.models.includes(defaults.model)
        ? defaults.model : providerDefaultModel(provider);
      if (model) return { kind: "provider", id: provider.id, model };
    }
  }
  if (defaults.backend === "harness") {
    const harness = harnesses.find((item) => item.id === defaults.harnessId && item.enabled && item.healthy);
    if (harness) {
      const model = defaults.model && harness.models.includes(defaults.model)
        ? defaults.model : harness.defaultModel?.trim() || harness.models[0];
      if (model) return { kind: "harness", id: harness.id, model };
    }
  }
  return defaultModelRuntime(providers, harnesses);
}
