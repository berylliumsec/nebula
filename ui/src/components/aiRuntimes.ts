import type { HarnessProfile, ProviderHealth } from "../api/types";

export interface AIRuntimeOption {
  key: string;
  kind: "provider" | "harness";
  id: string;
  name: string;
  models: string[];
  defaultModel: string;
  local: boolean;
  permitsSensitiveData: boolean;
}

function providerSupportsStructuredOutput(provider: ProviderHealth): boolean {
  return provider.capabilities.some((capability) =>
    capability.toLowerCase().includes("strict structured"),
  );
}

export function aiRuntimeOptions(
  providers: ProviderHealth[],
  harnesses: HarnessProfile[],
  options: { requireStructuredProvider?: boolean } = {},
): AIRuntimeOption[] {
  const providerOptions = providers
    .filter((provider) =>
      provider.enabled
      && provider.models.length > 0
      && (!options.requireStructuredProvider || providerSupportsStructuredOutput(provider)))
    .map((provider): AIRuntimeOption => ({
      key: `provider:${provider.id}`,
      kind: "provider",
      id: provider.id,
      name: provider.name,
      models: provider.models,
      defaultModel: provider.effectiveDefaultModel
        ?? provider.defaultModel
        ?? provider.models[0]
        ?? "",
      local: provider.local === true
        || provider.kind === "local"
        || provider.privacy === "local_only",
      permitsSensitiveData: provider.permitsSensitiveData,
    }));
  const harnessOptions = harnesses
    .filter((harness) => harness.enabled && harness.models.length > 0)
    .map((harness): AIRuntimeOption => ({
      key: `harness:${harness.id}`,
      kind: "harness",
      id: harness.id,
      name: harness.name,
      models: harness.models,
      defaultModel: harness.defaultModel ?? harness.models[0] ?? "",
      local: harness.localOnly,
      permitsSensitiveData: harness.permitsSensitiveData,
    }));
  return [...providerOptions, ...harnessOptions];
}

export function aiRuntimeLabel(runtime: AIRuntimeOption): string {
  return `${runtime.name} · ${runtime.kind === "harness" ? "Codex" : "provider"} · ${runtime.local ? "local" : "cloud"}`;
}
