import type { ModelDescriptor } from "./types";

/** Display catalog hints without changing the exact ID sent to Core. */
export function modelOptionLabel(id: string, descriptors?: ModelDescriptor[]): string {
  const model = descriptors?.find((candidate) => candidate.id === id);
  if (!model) return id;
  const label = model.name === id ? id : `${model.name} (${id})`;
  return model.contextWindow
    ? `${label} · ${model.contextWindow.toLocaleString("en-US")} context`
    : label;
}

export function modelCatalogSummary(id: string, descriptors?: ModelDescriptor[]): string | undefined {
  const model = descriptors?.find((candidate) => candidate.id === id);
  if (!model) return undefined;
  const parts = [
    model.inputModalities.length ? model.inputModalities.join(" + ") : undefined,
    model.supportedParameters.includes("tools") ? "tools advertised" : undefined,
    model.maxOutputTokens ? `${model.maxOutputTokens.toLocaleString("en-US")} max output` : undefined,
    model.expirationDate ? `retires ${model.expirationDate.slice(0, 10)}` : undefined,
  ].filter((part): part is string => Boolean(part));
  return parts.join(" · ") || undefined;
}
