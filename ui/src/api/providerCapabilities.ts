import type { ProviderCapabilityVerification, ProviderHealth } from "./types";

type ProviderModelSource = Pick<ProviderHealth, "defaultModel" | "modelAllowlist" | "models">;

/** Choose the exact model that a provider capability probe should verify. */
export function providerVerificationModel(provider?: ProviderModelSource): string | undefined {
  if (!provider) return undefined;
  return [provider.defaultModel, provider.modelAllowlist[0], provider.models[0]]
    .find((model): model is string => Boolean(model?.trim()))
    ?.trim();
}

/** Return a verification only when it belongs to the exact selected model. */
export function providerModelVerification(
  provider: Pick<ProviderHealth, "capabilityVerifications"> | undefined,
  model: string,
): ProviderCapabilityVerification | undefined {
  const exactModel = model.trim();
  if (!provider || !exactModel) return undefined;
  return Object.entries(provider.capabilityVerifications ?? {})
    .find(([storedModel, verification]) => storedModel.trim() === exactModel && verification.model.trim() === exactModel)?.[1];
}
