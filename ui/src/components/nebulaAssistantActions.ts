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
  | { kind: "headless_session"; action: "start" | "stop" }
  | { kind: "project_create"; name: string; description: string; clientName: string; requestId: string }
  | { kind: "project_rename"; projectId: string; expectedRevision: number; oldName: string; name: string }
  | { kind: "project_archive" | "project_restore" | "project_delete"; projectId: string; expectedRevision: number; name: string }
  | { kind: "chat_export"; projectId: string; sessionId: string; expectedRevision: number }
  | { kind: "workspace_delete"; projectId: string; path: string; expectedSha256: string }
  | { kind: "workspace_download"; projectId: string; path: string; expectedSha256: string; size: number }
  | { kind: "workspace_upload"; projectId: string; directory: string; expectedDirectoryIdentity: string }
  | { kind: "workspace_delete_directory"; projectId: string; path: string; expectedEntryIdentity: string }
  | { kind: "library_add"; projectId: string }
  | { kind: "library_download" | "library_reindex" | "library_remove"; projectId: string; itemId: string; expectedRevision: number; name: string };

export interface AssistantWorkReceipt { url: string; content: string }

function validWorkspacePath(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 4096
    && !value.startsWith("/") && !value.includes("\\") && !value.includes("\0")
    && value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

function validWorkspaceDirectory(value: unknown): value is string {
  return value === "" || validWorkspacePath(value);
}

function butlerGatewayOperation(event: Extract<ChatStreamEvent, {type: "tool_completed"}>, projectId?: string): string | undefined {
  const gateway = event.capability === "nebula.capabilities.change"
    || event.capability === "nebula_capabilities_change"
    || /^nebula_capabilities_change_[0-9a-f]{10,}$/.test(event.capability);
  if (!gateway || !projectId || event.receipt?.butler_project_id !== projectId) return undefined;
  return typeof event.receipt.butler_operation === "string" ? event.receipt.butler_operation : undefined;
}

/** Derive a project-local destination from a successful Core tool receipt. */
export function assistantWorkReceipt(event: ChatStreamEvent, projectId: string): AssistantWorkReceipt | undefined {
  if (event.type !== "tool_completed" || !["complete", "completed", "succeeded"].includes(event.status)) return undefined;
  const operation = butlerGatewayOperation(event, projectId) ?? event.capability;
  const chat = ["nebula.work.start_chat", "nebula.work.create_goal_conversation", "nebula.work.create_goal_child", "nebula_work_start_chat"].includes(operation)
    || /^nebula_work_start_chat_[0-9a-f]{10,}$/.test(operation);
  const mission = ["nebula.work.start_mission", "nebula.work.retry_mission", "nebula_work_start_mission"].includes(operation)
    || /^nebula_work_start_mission_[0-9a-f]{10,}$/.test(operation);
  if (!chat && !mission) return undefined;
  const id = event.receipt?.[chat ? "session_id" : "run_id"];
  if (typeof id !== "string" || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(id)) return undefined;
  const url = `/projects/${encodeURIComponent(projectId)}/workbench?view=${chat ? "chat&session" : "missions&mission"}=${encodeURIComponent(id)}`;
  return { url, content: chat ? `Chat started. [Open chat](${url})` : `Mission saved. [Open mission](${url})` };
}

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
export function settingClientAction(event: ChatStreamEvent, projectId?: string): SettingClientAction | undefined {
  if (event.type !== "tool_completed" || !["complete", "completed", "succeeded"].includes(event.status)) return undefined;
  const receipt = event.receipt;
  if (receipt?.state !== "pending_browser_apply") return undefined;
  if ((event.capability === "nebula.capabilities.change" || event.capability === "nebula_capabilities_change"
    || /^nebula_capabilities_change_[0-9a-f]{10,}$/.test(event.capability))
    && !butlerGatewayOperation(event, projectId)) return undefined;
  const operation = butlerGatewayOperation(event, projectId) ?? event.capability;
  const action = receipt.client_action;
  if (!action || typeof action !== "object" || Array.isArray(action)) return undefined;
  const value = action as Record<string, unknown>;
  const projectAction = event.capability === "nebula.capabilities.change"
    || event.capability === "nebula_capabilities_change"
    || /^nebula_capabilities_change_[0-9a-f]{10,}$/.test(event.capability);
  if (projectAction && operation === "nebula.chats.export" && value.kind === "chat_export"
      && typeof projectId === "string" && value.project_id === projectId
      && typeof value.session_id === "string"
      && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(value.session_id)
      && Number.isInteger(value.expected_revision) && Number(value.expected_revision) >= 1) {
    return {kind: "chat_export", projectId, sessionId: value.session_id, expectedRevision: Number(value.expected_revision)};
  }
  if (projectAction && typeof projectId === "string" && value.project_id === projectId
      && validWorkspacePath(value.path) && typeof value.expected_sha256 === "string"
      && /^[0-9a-f]{64}$/.test(value.expected_sha256)) {
    if (operation === "nebula.workspace.delete_file" && value.kind === "workspace_delete") {
      return {kind: "workspace_delete", projectId, path: value.path, expectedSha256: value.expected_sha256};
    }
    if (operation === "nebula.workspace.download_file" && value.kind === "workspace_download"
        && Number.isSafeInteger(value.size) && Number(value.size) >= 0) {
      return {kind: "workspace_download", projectId, path: value.path,
        expectedSha256: value.expected_sha256, size: Number(value.size)};
    }
  }
  if (projectAction && typeof projectId === "string" && value.project_id === projectId) {
    if (operation === "nebula.workspace.upload_from_device" && value.kind === "workspace_upload"
        && validWorkspaceDirectory(value.directory)
        && typeof value.expected_directory_identity === "string"
        && /^[0-9a-f]{64}$/.test(value.expected_directory_identity)) {
      return {kind: "workspace_upload", projectId, directory: value.directory,
        expectedDirectoryIdentity: value.expected_directory_identity};
    }
    if (operation === "nebula.workspace.delete_directory" && value.kind === "workspace_delete_directory"
        && validWorkspacePath(value.path) && typeof value.expected_entry_identity === "string"
        && /^[0-9a-f]{64}$/.test(value.expected_entry_identity)) {
      return {kind: "workspace_delete_directory", projectId, path: value.path,
        expectedEntryIdentity: value.expected_entry_identity};
    }
  }
  if (projectAction && operation === "nebula.library.add" && value.kind === "library_add"
      && typeof projectId === "string" && value.project_id === projectId) {
    return {kind: "library_add", projectId};
  }
  const libraryKind = operation === "nebula.library.download" ? "library_download"
    : operation === "nebula.library.reindex" ? "library_reindex"
    : operation === "nebula.library.remove" ? "library_remove" : undefined;
  if (projectAction && libraryKind && value.kind === libraryKind
      && typeof projectId === "string" && value.project_id === projectId
      && typeof value.item_id === "string"
      && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(value.item_id)
      && Number.isInteger(value.expected_revision) && Number(value.expected_revision) >= 1
      && typeof value.name === "string" && value.name.trim().length > 0 && value.name.length <= 300) {
    return {kind: libraryKind, projectId, itemId: value.item_id, expectedRevision: Number(value.expected_revision), name: value.name};
  }
  if (projectAction && operation.startsWith("nebula.projects.")) {
    const name = value.name;
    const validName = typeof name === "string" && name.trim().length > 0 && name.length <= 300;
    if (operation === "nebula.projects.create" && value.kind === "project_create" && validName
        && typeof value.description === "string" && value.description.length <= 20_000
        && typeof value.client_name === "string" && value.client_name.length <= 300
        && typeof value.request_id === "string" && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(value.request_id)) {
      return { kind: "project_create", name, description: value.description, clientName: value.client_name, requestId: value.request_id };
    }
    const target = typeof value.project_id === "string" && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(value.project_id) ? value.project_id : undefined;
    const revision = Number.isInteger(value.expected_revision) && Number(value.expected_revision) >= 1 ? Number(value.expected_revision) : undefined;
    if (target && revision && validName) {
      if (operation === "nebula.projects.rename" && value.kind === "project_rename"
          && typeof value.old_name === "string" && value.old_name.length <= 300) {
        return { kind: "project_rename", projectId: target, expectedRevision: revision, oldName: value.old_name, name };
      }
      const kind = `project_${operation.slice("nebula.projects.".length)}`;
      if (kind === value.kind && (kind === "project_archive" || kind === "project_restore" || kind === "project_delete")) {
        return { kind, projectId: target, expectedRevision: revision, name };
      }
    }
    return undefined;
  }
  const themeTool = operation === "nebula.settings.set_device_theme"
    || /^nebula_settings_set_device_theme_[0-9a-f]{10,}$/.test(operation);
  const openTool = operation === "nebula.settings.open"
    || /^nebula_settings_open_[0-9a-f]{10,}$/.test(operation);
  const modeTool = operation === "nebula.settings.set_project_mode"
    || /^nebula_settings_set_project_mode_[0-9a-f]{10,}$/.test(operation);
  const scopeTool = operation === "nebula.settings.set_project_scope"
    || /^nebula_settings_set_project_scope_[0-9a-f]{10,}$/.test(operation);
  const headlessTool = operation === "nebula.settings.headless_session"
    || /^nebula_settings_headless_session_[0-9a-f]{10,}$/.test(operation);
  const navigateTool = operation === "nebula.navigate.open"
    || /^nebula_navigate_open_[0-9a-f]{10,}$/.test(operation);
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
