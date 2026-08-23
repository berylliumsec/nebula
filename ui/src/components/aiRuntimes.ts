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

function advertisedDefault(models: string[], candidate?: string): string {
  return candidate && models.includes(candidate) ? candidate : models[0] ?? "";
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
      defaultModel: advertisedDefault(
        provider.models,
        provider.effectiveDefaultModel ?? provider.defaultModel,
      ),
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
      defaultModel: advertisedDefault(harness.models, harness.defaultModel),
      local: harness.localOnly,
      permitsSensitiveData: harness.permitsSensitiveData,
    }));
  return [...providerOptions, ...harnessOptions];
}

export function aiRuntimeLabel(runtime: AIRuntimeOption): string {
  return `${runtime.name} · ${runtime.kind === "harness" ? "Codex" : "provider"} · ${runtime.local ? "local" : "cloud"}`;
}

/** Code suggestions are opt-in: a runtime must explicitly advertise code support. */
export function codeSuggestionRuntimeOptions(
  providers: ProviderHealth[],
  harnesses: HarnessProfile[],
): AIRuntimeOption[] {
  return aiRuntimeOptions(providers, harnesses).filter((runtime) => {
    const source = runtime.kind === "provider"
      ? providers.find((item) => item.id === runtime.id)?.capabilities ?? []
      : (() => {
          const caps = harnesses.find((item) => item.id === runtime.id)?.capabilities;
          return caps && (caps.fileDiffs || caps.liveCommandOutput || caps.steering) ? ["code_suggestions"] : [];
        })();
    return source.some((value) => /code|completion|suggest/i.test(value));
  });
}
