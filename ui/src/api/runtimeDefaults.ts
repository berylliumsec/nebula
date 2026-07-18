import type { HarnessProfile, ProviderHealth } from "./types";

type ProviderCandidate = Pick<ProviderHealth, "enabled" | "id" | "models" | "state">;
type HarnessCandidate = Pick<HarnessProfile, "enabled" | "healthy" | "id" | "models">;

export type DefaultModelRuntime =
  | { kind: "harness"; id: string; model: string }
  | { kind: "provider"; id: string; model: string };

function firstModel(models: readonly string[]): string | undefined {
  return models.find((model) => model.trim().length > 0);
}

/**
 * Selects a usable runtime for a new chat or mission. Harnesses win when both
 * runtime kinds are ready, and model ordering is preserved from discovery.
 */
export function defaultModelRuntime(
  providers: readonly ProviderCandidate[],
  harnesses: readonly HarnessCandidate[],
): DefaultModelRuntime | undefined {
  for (const harness of harnesses) {
    const model = firstModel(harness.models);
    if (harness.enabled && harness.healthy && model) {
      return { kind: "harness", id: harness.id, model };
    }
  }

  for (const provider of providers) {
    const model = firstModel(provider.models);
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
