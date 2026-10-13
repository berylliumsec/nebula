import type { ChatStreamEvent, HarnessProfile } from "../api/types";
import { THEME_OPTIONS, isThemePreference, type ThemePreference } from "../state/ThemeContext";
import { settingCatalog, type SettingCatalogEntry } from "../settingsCatalog";
import { assistantDestination, type AssistantDestination } from "../assistantDestinations";

type Runtime = { kind: "provider" | "harness"; id: string; model: string };
export type ProjectMode = "docker" | "host";
export type ProjectScopeMode = "bounded" | "all_destinations" | "allow_all";
export type SettingClientAction =
  | { kind: "open_setting"; setting: SettingCatalogEntry }
  | { kind: "navigate"; destination: AssistantDestination }
  | { kind: "device_theme"; theme: ThemePreference }
  | { kind: "project_execution_mode"; mode: ProjectMode; from?: ProjectMode }
  | { kind: "project_scope"; changes: Record<string, unknown> }
  | { kind: "headless_session"; action: "start" | "stop" };

const scopeFields = new Set([
  "mode", "allowed_cidrs", "allowed_domains", "allowed_urls", "allowed_ports",
  "not_before", "not_after", "prohibited_actions", "local_only", "tool_suggestions",
  "on_demand_tools", "web_search", "web_search_discloses_scope", "always_loaded_tools",
  "max_concurrency",
]);

export function validScopeChanges(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const entries = Object.entries(value);
  if (!entries.length || entries.some(([name]) => !scopeFields.has(name))) return false;
  return entries.every(([name, field]) => {
    if (name === "mode") return ["bounded", "all_destinations", "allow_all"].includes(String(field));
    if (["not_before", "not_after"].includes(name)) return field === null || typeof field === "string";
    if (name === "max_concurrency") return Number.isInteger(field) && Number(field) >= 1 && Number(field) <= 256;
    if (["local_only", "tool_suggestions", "on_demand_tools", "web_search", "web_search_discloses_scope"].includes(name)) return typeof field === "boolean";
    if (!Array.isArray(field)) return false;
    return field.every((item) => name === "allowed_ports" ? Number.isInteger(item) && item >= 1 && item <= 65535 : typeof item === "string");
  });
}

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
  const scopeTool = event.capability === "nebula.settings.set_project_scope"
    || /^nebula_settings_set_project_scope_[0-9a-f]{10,}$/.test(event.capability);
  const headlessTool = event.capability === "nebula.settings.headless_session"
    || /^nebula_settings_headless_session_[0-9a-f]{10,}$/.test(event.capability);
  const navigateTool = event.capability === "nebula.navigate.open"
    || /^nebula_navigate_open_[0-9a-f]{10,}$/.test(event.capability);
  if (themeTool && value.kind === "device_theme" && isThemePreference(value.theme)) {
    return { kind: "device_theme", theme: value.theme };
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
  if (scopeTool && value.kind === "project_scope" && validScopeChanges(value.changes)) {
    return { kind: "project_scope", changes: value.changes };
  }
  if (headlessTool && value.kind === "headless_session" && (value.action === "start" || value.action === "stop")) {
    return { kind: "headless_session", action: value.action };
  }
  return undefined;
}

function normalized(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function labelHasToken(label: string, token: string): boolean {
  return label.split(" ").some((word) => word === token || word === `${token}s` || word === `${token}es`);
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
      if (labelHasToken(label, token)) return total + 5;
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

/** Explicit scope commands use the existing policy API even with no model configured. */
export function projectScopeChange(prompt: string): { mode: ProjectScopeMode } | undefined {
  const text = normalized(prompt);
  if (!/^(?:(?:please|can you|could you|would you)\s+)?(?:set|change|switch|allow|enable|make)\b/.test(text)) return undefined;
  if (!/\b(?:project|policy|scope|urls?|network|targets?|ports?|permissions?)\b/.test(text)) return undefined;
  if (/\b(?:all urls?|all network targets?|all destinations?|every network target|every url)\b/.test(text)) {
    return { mode: "all_destinations" };
  }
  if (/\b(?:bypass (?:all )?permissions?|allow all mode|allow all permissions?)\b/.test(text)) {
    return { mode: "allow_all" };
  }
  if (/\b(?:bounded mode|bounded scope)\b/.test(text)) return { mode: "bounded" };
  return undefined;
}

export function headlessSessionChange(prompt: string): "start" | "stop" | undefined {
  const text = normalized(prompt);
  if (!/^(?:(?:please|can you|could you|would you)\s+)?(?:set|change|switch|allow|enable|make|start|stop|configure|disable)\b/.test(text)) return undefined;
  const command = text.match(/\b(start|enable|configure|stop|disable)\s+(?:(?:the|a)\s+)?headless\s+(?:security\s+)?(?:browser|session)\b/);
  if (!command) return undefined;
  return command[1] === "stop" || command[1] === "disable" ? "stop" : "start";
}

/** Keep explicit device-theme commands in the browser, including unknown themes. */
function themeCommandTarget(prompt: string): string | undefined {
  const text = normalized(prompt);
  const command = text.match(/^(?:(?:please|can you|could you|would you)\s+)?(?:change|switch|set|use|make)\s+(.+)$/);
  if (!command) return undefined;
  const request = command[1];
  const target = request.match(/\b(?:to|into|as)\s+(.+)$/)?.[1]
    ?? request.replace(/^(?:(?:the|my|this device)\s+)?(?:theme|appearance)\s+/, "")
      .replace(/^(?:it\s+)?(?:to|into|as)\s+/, "");
  const value = target.replace(/^(?:(?:the|my|this device)\s+)+/, "").replace(/\s+(?:theme|appearance)$/, "");
  if (!/\b(?:theme|appearance)\b/.test(text)
    && value !== "zero"
    && !THEME_OPTIONS.some((theme) => normalized(theme.label) === value)) return undefined;
  return value;
}

export function isThemeChangeCommand(prompt: string): boolean {
  return themeCommandTarget(prompt) !== undefined;
}

/** Device appearance is browser-owned, so a known theme is applied in this window. */
export function themeChange(prompt: string): ThemePreference | undefined {
  const value = themeCommandTarget(prompt);
  if (!value) return undefined;
  if (value === "zero") return "zero-dark";
  return THEME_OPTIONS.find((theme) => normalized(theme.label) === value)?.value;
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
  const exact = settingCatalog.filter((entry) => normalized(entry.label) === target);
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return undefined;
  const partial = settingCatalog.filter((entry) => normalized(entry.label).includes(target));
  if (partial.length === 1) return partial[0];
  const alias = settingCatalog.filter((entry) => entry.keywords.some((keyword) => normalized(keyword) === target));
  if (alias.length === 1) return alias[0];
  const configurationAlias = alias.filter((entry) => entry.id !== "settings.usage");
  if (configurationAlias.length === 1) return configurationAlias[0];
  const candidates = settingNavigationCandidates(prompt);
  if (candidates.length !== 1) {
    const tokens = normalized(prompt).split(" ").filter((word) => word.length > 2 && !NAVIGATION_WORDS.has(word));
    const top = candidates[0];
    const runnerUp = candidates[1];
    if (!top || !tokens.length) return undefined;
    const score = (entry: SettingCatalogEntry) => tokens.reduce((total, token) => {
      const label = normalized(entry.label);
      const aliases = entry.keywords.map(normalized);
      return total + (labelHasToken(label, token) ? 5 : aliases.some((alias) => alias.split(" ").includes(token)) ? 3 : 0);
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
