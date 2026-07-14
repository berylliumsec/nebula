import type { ProviderHealth } from "./types";

type ProviderModelSource = Pick<ProviderHealth, "defaultModel" | "modelAllowlist" | "models">;

/** Choose the exact model that a provider capability probe should verify. */
export function providerVerificationModel(provider?: ProviderModelSource): string | undefined {
  if (!provider) return undefined;
  return [provider.defaultModel, provider.modelAllowlist[0], provider.models[0]]
    .find((model): model is string => Boolean(model?.trim()))
    ?.trim();
}
