import type { ChatStreamEvent, HarnessProfile } from "../api/types";
import type { ThemePreference } from "../state/ThemeContext";
import { settingCatalog, type SettingCatalogEntry } from "../settingsCatalog";
import { assistantDestination, type AssistantDestination } from "../assistantDestinations";

type Runtime = { kind: "provider" | "harness"; id: string; model: string };
export type ProjectMode = "docker" | "host";
export type SettingClientAction =
  | { kind: "open_setting"; setting: SettingCatalogEntry }
  | { kind: "navigate"; destination: AssistantDestination }
  | { kind: "device_theme"; theme: ThemePreference }
  | { kind: "project_execution_mode"; mode: ProjectMode; from?: ProjectMode };

/** Accept only a completed, scoped tool receipt as a browser action. */
export function settingClientAction(event: ChatStreamEvent): SettingClientAction | undefined {
  if (event.type !== "tool_completed" || !["complete", "completed", "succeeded"].includes(event.status)) return undefined;
  const receipt = event.receipt;
  if (receipt?.state !== "pending_browser_apply") return undefined;
  const action = receipt.client_action;
  if (!action || typeof action !== "object" || Array.isArray(action)) return undefined;
  const value = action as Record<string, unknown>;
  const themeTool = event.capability === "nebula.settings.set_device_theme"
    || /^nebula_settings_set_device_theme_[0-9a-f]{10,}$/.test(event.capability);
  const openTool = event.capability === "nebula.settings.open"
    || /^nebula_settings_open_[0-9a-f]{10,}$/.test(event.capability);
  const modeTool = event.capability === "nebula.settings.set_project_mode"
    || /^nebula_settings_set_project_mode_[0-9a-f]{10,}$/.test(event.capability);
  const navigateTool = event.capability === "nebula.navigate.open"
    || /^nebula_navigate_open_[0-9a-f]{10,}$/.test(event.capability);
  if (themeTool && value.kind === "device_theme"
      && ["light", "dark", "studio-dark", "zero-light", "zero-dark"].includes(String(value.theme))) {
    return { kind: "device_theme", theme: value.theme as ThemePreference };
  }
  if (openTool && value.kind === "open_setting" && typeof value.setting_id === "string") {
    const setting = settingCatalog.find((entry) => entry.id === `settings.${value.setting_id}`);
    if (setting) return { kind: "open_setting", setting };
  }
  if (navigateTool && value.kind === "navigate" && typeof value.destination_id === "string") {
    const destination = assistantDestination(value.destination_id);
    if (destination) return { kind: "navigate", destination };
  }
  if (modeTool && value.kind === "project_execution_mode"
      && (value.mode === "docker" || value.mode === "host")
      && (value.from === undefined || value.from === "docker" || value.from === "host")) {
    return { kind: "project_execution_mode", mode: value.mode, ...(value.from ? { from: value.from } : {}) };
  }
  return undefined;
}

function normalized(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

const NAVIGATION_WORDS = new Set([
  "a", "about", "all", "an", "and", "are", "bring", "can", "control", "controls",
  "do", "everything", "find", "for", "go", "i", "in", "is", "it", "list", "lists", "locate", "me", "my", "of",
  "open", "page", "place", "please", "screen", "see", "setting", "settings", "show",
  "take", "that", "the", "this", "to", "up", "where", "which", "with", "would", "you",
]);

export function isSettingNavigationRequest(prompt: string): boolean {
  const text = normalized(prompt);
  // A question about a failure needs an explanation grounded in Core state,
  // even when it also contains a word such as "where" or "find".
  if (/\b(?:why|how|cannot|cant|doesnt|failed|failure|error|troubleshoot|unable|permission|not working)\b/.test(text)) return false;
  return /^(?:(?:please|can you|could you|would you)\s+)?(?:open|show|bring up|take me to|go to|find|locate|where|which setting)\b/.test(text)
    || /\b(?:where|which setting|setting for|controls for|page for)\b/.test(text);
}

/** Rank the bounded settings catalog locally so a navigation request never waits on a model. */
export function settingNavigationCandidates(prompt: string): SettingCatalogEntry[] {
  if (!isSettingNavigationRequest(prompt)) return [];
  const tokens = [...new Set(normalized(prompt).split(" ").filter((word) => word.length > 2 && !NAVIGATION_WORDS.has(word)))];
  if (!tokens.length) return [];
  const scored = settingCatalog.map((entry) => {
    const label = normalized(entry.label);
    const aliases = entry.keywords.map(normalized);
    const description = normalized(entry.description);
    const score = tokens.reduce((total, token) => {
      if (label.split(" ").includes(token)) return total + 4;
      if (aliases.some((alias) => alias.split(" ").includes(token))) return total + 3;
      if (description.split(" ").includes(token)) return total + 1;
      return total;
    }, 0);
    return { entry, score };
  }).filter(({ score }) => score >= 3).sort((a, b) => b.score - a.score);
  return scored.slice(0, 3).map(({ entry }) => entry);
}

/** Only an explicit project-policy command may change the command runtime. */
export function projectModeChange(prompt: string): { mode: ProjectMode; from?: ProjectMode } | undefined {
  const text = normalized(prompt.replace(/\b(docker|host)mode\b/gi, "$1 mode"));
  if (!/^(?:(?:please|can you|could you|would you)\s+)?(?:open|change|switch|set|use)\b/.test(text)) return undefined;
  const from = text.match(/\bfrom\s+(docker|host)(?:\s+mode)?\b/)?.[1] as ProjectMode | undefined;
  const target = text.match(/\b(?:to|into|use)\s+(docker|host)(?:\s+mode)?\b/)?.[1]
    ?? text.match(/\b(?:set|switch)\s+(?:the\s+)?(?:project\s+)?(?:execution\s+mode|policy)\s+(docker|host)(?:\s+mode)?$/)?.[1];
  if (target !== "docker" && target !== "host") return undefined;
  if (!text.includes("project policy") && !text.includes("execution mode") && !from) return undefined;
  return { mode: target, ...(from ? { from } : {}) };
}

/** Device appearance is browser-owned, so an explicit change is handled in this window. */
export function themeChange(prompt: string): ThemePreference | undefined {
  const text = normalized(prompt);
  const command = text.match(/^(?:(?:please|can you|could you|would you)\s+)?(?:change|switch|set|use|make)\s+(.+)$/);
  if (!command) return undefined;
  const request = command[1];
  const target = request.match(/\b(?:to|into|as)\s+((?:zero(?: dark| light)?|studio dark|dark|light)(?: theme)?)$/)?.[1]
    ?? request.replace(/^(?:(?:the|my|this device)\s+)?(?:theme|appearance)\s+/, "")
      .replace(/^(?:it\s+)?(?:to|into|as)\s+/, "");
  if (!/\b(?:theme|appearance|zero|studio)\b/.test(text)) return undefined;
  const value = target.replace(/\s+theme$/, "");
  const themes: Record<string, ThemePreference> = {
    light: "light", dark: "dark", "studio dark": "studio-dark",
    zero: "zero-dark", "zero light": "zero-light", "zero dark": "zero-dark",
  };
  return themes[value];
}

/** Route an explicit navigation request through the same Settings lens as search. */
export function settingToOpen(prompt: string): SettingCatalogEntry | undefined {
  if (!isSettingNavigationRequest(prompt)) return undefined;
  const match = prompt.trim().match(/^(?:(?:please|can you|could you|would you)\s+)?(?:open|show|bring up|take me to|go to)\s+(.+?)[.!?]*$/i);
  const target = normalized(match?.[1] ?? prompt)
    .replace(/^the\s+/, "")
    .replace(/\s+(?:settings?|page|controls)$/, "")
    .trim();
  if (target.length < 2) return undefined;
  const exact = settingCatalog.filter((entry) =>
    [entry.label, ...entry.keywords].some((alias) => normalized(alias) === target));
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return undefined;
  const partial = settingCatalog.filter((entry) => normalized(entry.label).includes(target));
  if (partial.length === 1) return partial[0];
  const candidates = settingNavigationCandidates(prompt);
  if (candidates.length !== 1) {
    const tokens = normalized(prompt).split(" ").filter((word) => word.length > 2 && !NAVIGATION_WORDS.has(word));
    const top = candidates[0];
    const runnerUp = candidates[1];
    if (!top || !tokens.length) return undefined;
    const score = (entry: SettingCatalogEntry) => tokens.reduce((total, token) => {
      const label = normalized(entry.label).split(" ");
      const aliases = entry.keywords.map(normalized);
      return total + (label.includes(token) ? 4 : aliases.some((alias) => alias.split(" ").includes(token)) ? 3 : 0);
    }, 0);
    if (score(top) >= 3 && score(top) >= score(runnerUp ?? top) + 2) return top;
    return undefined;
  }
  return candidates[0];
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
