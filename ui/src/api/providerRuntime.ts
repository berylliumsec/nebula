import type { ProviderHealth } from "./types";

function discoveredModels(provider: ProviderHealth): string[] {
  return provider.availableModels?.length ? provider.availableModels : provider.models;
}

function discoveryMessage(models: string[]): string {
  return models.length
    ? `${models.length} model${models.length === 1 ? "" : "s"} available from the last health check.`
    : "The last health check reported no model in this profile's allowed models.";
}

/**
 * Fold a provider profile record into the cached provider without discarding what
 * a health check discovered.
 *
 * A profile read only carries durable configuration: its model list is the allowed
 * models and its state is always "unchecked". Replacing a checked provider with one
 * empties the operator's model picker, so keep the discovered catalog and re-derive
 * the selectable models from the profile's allowed models. The health verdict itself
 * only survives when the last check succeeded on a profile that stayed enabled; a
 * failed or disabled one leaves the record's own state to say that it is unknown.
 */
export function providerWithDiscoveredModels(
  previous: ProviderHealth | undefined,
  next: ProviderHealth,
): ProviderHealth {
  // Discovery belongs to the endpoint it ran against.
  const checked = previous?.lastCheckedAt
    && previous.id === next.id
    && previous.endpoint === next.endpoint
    && previous.providerType === next.providerType
    ? previous
    : undefined;
  const available = checked ? discoveredModels(checked) : [];
  if (!checked || !available.length) return next;
  const models = next.modelAllowlist.length
    ? available.filter((model) => next.modelAllowlist.includes(model))
    : available;
  const healthy = checked.state === "healthy" && checked.enabled && next.enabled;
  const unchanged = models.length === checked.models.length
    && models.every((model, index) => model === checked.models[index]);
  return {
    ...next,
    models,
    availableModels: available,
    modelDescriptors: checked.modelDescriptors,
    modelCount: models.length,
    latencyMs: checked.latencyMs,
    lastCheckedAt: checked.lastCheckedAt,
    state: healthy ? checked.state : next.state,
    message: next.enabled
      ? healthy && unchanged ? checked.message : discoveryMessage(models)
      : next.message,
  };
}

/** Fold a provider list read into the cached providers, keeping discovered models. */
export function providersWithDiscoveredModels(
  previous: readonly ProviderHealth[],
  next: readonly ProviderHealth[],
): ProviderHealth[] {
  return next.map((provider) => providerWithDiscoveredModels(
    previous.find((item) => item.id === provider.id),
    provider,
  ));
}
