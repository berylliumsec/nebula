import type { HarnessProfile } from "../api/types";
import { settingCatalog, type SettingCatalogEntry } from "../settingsCatalog";

type Runtime = { kind: "provider" | "harness"; id: string; model: string };

function normalized(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/** Route an explicit navigation request through the same Settings lens as search. */
export function settingToOpen(prompt: string): SettingCatalogEntry | undefined {
  const match = prompt.trim().match(/^(?:(?:please|can you|could you|would you)\s+)?(?:open|show|bring up|take me to|go to)\s+(.+?)[.!?]*$/i);
  if (!match) return undefined;
  const target = normalized(match[1])
    .replace(/^the\s+/, "")
    .replace(/\s+(?:settings?|page|controls)$/, "")
    .trim();
  if (target.length < 3) return undefined;
  const exact = settingCatalog.filter((entry) =>
    [entry.label, ...entry.keywords].some((alias) => normalized(alias) === target));
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return undefined;
  const partial = settingCatalog.filter((entry) => normalized(entry.label).includes(target));
  return partial.length === 1 ? partial[0] : undefined;
}

/** Ask only for options the selected model explicitly advertises. */
export function assistantTurnOptions(
  runtime: Runtime,
  harnesses: readonly HarnessProfile[],
): { model: string; reasoningEffort?: "low"; harnessReasoningEffort?: string; harnessServiceTier?: string } {
  if (runtime.kind === "provider") {
    // Core's provider adapters translate low where the exact model supports it
    // and omit unsupported vendor parameters. Providers have no fast-mode ask.
    return { model: runtime.model, reasoningEffort: "low" };
  }

  const profile = harnesses.find((item) => item.id === runtime.id);
  const current = profile?.modelOptions?.find((item) => item.model === runtime.model);
  const currentFastTier = current?.serviceTiers.find((item) => normalized(item.id) === "fast");
  const fastSibling = !currentFastTier && !runtime.model.endsWith("-build-fast")
    ? `${runtime.model}-build-fast`
    : undefined;
  const model = fastSibling && profile?.models.includes(fastSibling) ? fastSibling : runtime.model;
  const options = profile?.modelOptions?.find((item) => item.model === model);
  const low = options?.reasoningEfforts.find((item) => normalized(item.id) === "low");
  const fast = options?.serviceTiers.find((item) => normalized(item.id) === "fast");
  return {
    model,
    ...(low ? { harnessReasoningEffort: low.id } : {}),
    ...(fast ? { harnessServiceTier: fast.id } : {}),
  };
}
