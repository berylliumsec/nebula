import type { HarnessProfile, ProviderHealth } from "./types";

type ProviderCandidate = Pick<ProviderHealth, "defaultModel" | "effectiveDefaultModel" | "enabled" | "id" | "models" | "state">;
type ProviderModelCandidate = Pick<ProviderHealth, "defaultModel" | "effectiveDefaultModel" | "models">;
type HarnessCandidate = Pick<HarnessProfile, "enabled" | "healthy" | "id" | "models" | "defaultModel">;

export type DefaultModelRuntime =
  | { kind: "harness"; id: string; model: string }
  | { kind: "provider"; id: string; model: string };

function firstModel(models: readonly string[]): string | undefined {
  return models.find((model) => model.trim().length > 0);
}

/**
 * The model a provider should start with: the profile's saved default whenever the
 * runtime still reports it, otherwise the first discovered model. Selecting a
 * provider must not silently replace the default its operator saved in Settings.
 */
export function providerDefaultModel(provider?: ProviderModelCandidate): string {
  const models = provider?.models ?? [];
  const saved = (provider?.defaultModel ?? provider?.effectiveDefaultModel)?.trim();
  if (saved && models.some((model) => model.trim() === saved)) return saved;
  return firstModel(models) ?? "";
}

/**
 * Selects a usable runtime for a new chat or mission. Harnesses win when both
 * runtime kinds are ready. A configured runtime default takes precedence over discovery.
 */
export function defaultModelRuntime(
  providers: readonly ProviderCandidate[],
  harnesses: readonly HarnessCandidate[],
): DefaultModelRuntime | undefined {
  for (const harness of harnesses) {
    const model = harness.defaultModel?.trim() || firstModel(harness.models);
    if (harness.enabled && harness.healthy && model) {
      return { kind: "harness", id: harness.id, model };
    }
  }

  for (const provider of providers) {
    const model = providerDefaultModel(provider);
    if (
      provider.enabled
      && (provider.state === "healthy" || provider.state === "unchecked")
      && model
    ) {
      return { kind: "provider", id: provider.id, model };
    }
  }

  return undefined;
}
