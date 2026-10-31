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
  | { kind: "project_create_linked"; sourceProjectId: string; name: string; description: string; clientName: string; requestId: string }
  | { kind: "project_relink"; sourceProjectId: string; projectId: string; expectedRevision: number; name: string }
  | { kind: "project_rename"; projectId: string; expectedRevision: number; oldName: string; name: string }
  | { kind: "project_archive" | "project_restore" | "project_delete"; projectId: string; expectedRevision: number; name: string }
  | { kind: "chat_export"; projectId: string; sessionId: string; expectedRevision: number }
  | { kind: "workspace_delete"; projectId: string; path: string; expectedSha256: string }
  | { kind: "workspace_download"; projectId: string; path: string; expectedSha256: string; size: number }
  | { kind: "workspace_upload"; projectId: string; directory: string; expectedDirectoryIdentity: string }
  | { kind: "workspace_delete_directory"; projectId: string; path: string; expectedEntryIdentity: string }
  | { kind: "notebook_index_connect" | "notebook_index_remove"; projectId: string; expectedProjectRevision: number }
  | { kind: "library_add"; projectId: string }
  | { kind: "library_download" | "library_reindex" | "library_remove"; projectId: string; itemId: string; expectedRevision: number; name: string }
  | { kind: "evidence_upload"; projectId: string }
  | { kind: "evidence_download"; projectId: string; evidenceId: string; expectedRevision: number; title: string; expectedSha256: string }
  | { kind: "report_create"; projectId: string; reportId: string; title: string; executiveSummary: string; findingIds: string[]; observationIds: string[] }
  | { kind: "report_update"; projectId: string; reportId: string; expectedRevision: number; title?: string; status?: "draft" | "review"; executiveSummary?: string; findingIds?: string[]; observationIds?: string[] }
  | { kind: "report_transform_note"; projectId: string; reportId: string; expectedRevision: number; observationId: string; expectedSourceRevision: number; instruction: string }
  | { kind: "report_signoff" | "report_export_pdf"; projectId: string; reportId: string; expectedRevision: number }
  | { kind: "report_delete"; projectId: string; reportId: string; expectedRevision: number; title: string }
  | { kind: "report_export_bundle"; projectId: string; expectedProjectRevision: number }
  | { kind: "terminal_stop"; projectId: string; sessionId: string; expectedCreatedAt: string };

export interface AssistantWorkReceipt { url: string; content: string }

function validWorkspacePath(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 4096
    && !value.startsWith("/") && !value.includes("\\") && !value.includes("\0")
    && value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

function validWorkspaceDirectory(value: unknown): value is string {
  return value === "" || validWorkspacePath(value);
}

const uuidPattern = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i;

function validReportReferences(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 500
    && value.every((item) => typeof item === "string" && uuidPattern.test(item))
    && new Set(value).size === value.length;
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
  if (projectAction && operation === "nebula.terminal.stop" && value.kind === "terminal_stop"
      && typeof projectId === "string" && value.project_id === projectId
      && typeof value.session_id === "string" && uuidPattern.test(value.session_id)
      && typeof value.expected_created_at === "string"
      && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value.expected_created_at)
      && Number.isFinite(Date.parse(value.expected_created_at))) {
    return {kind: "terminal_stop", projectId, sessionId: value.session_id,
      expectedCreatedAt: value.expected_created_at};
  }
  if (projectAction && typeof projectId === "string" && value.project_id === projectId
      && operation.startsWith("nebula.reports.")) {
    const reportId = typeof value.report_id === "string" && uuidPattern.test(value.report_id) ? value.report_id : undefined;
    const revision = Number.isInteger(value.expected_revision) && Number(value.expected_revision) >= 1
      ? Number(value.expected_revision) : undefined;
    if (operation === "nebula.reports.export_bundle" && value.kind === "report_export_bundle"
        && Number.isInteger(value.expected_project_revision) && Number(value.expected_project_revision) >= 1) {
      return {kind: "report_export_bundle", projectId, expectedProjectRevision: Number(value.expected_project_revision)};
    }
    if (!reportId) return undefined;
    if (operation === "nebula.reports.create" && value.kind === "report_create"
        && typeof value.title === "string" && value.title.trim().length > 0 && value.title.length <= 500
        && typeof value.executive_summary === "string" && value.executive_summary.length <= 100_000
        && validReportReferences(value.finding_ids) && validReportReferences(value.observation_ids)) {
      return {kind: "report_create", projectId, reportId, title: value.title,
        executiveSummary: value.executive_summary, findingIds: value.finding_ids, observationIds: value.observation_ids};
    }
    if (operation === "nebula.reports.update" && value.kind === "report_update" && revision) {
      const changes = ["title", "status", "executive_summary", "finding_ids", "observation_ids"]
        .filter((field) => Object.hasOwn(value, field));
      if (!changes.length || (value.title !== undefined && (typeof value.title !== "string" || !value.title.trim() || value.title.length > 500))
          || (value.status !== undefined && value.status !== "draft" && value.status !== "review")
          || (value.executive_summary !== undefined && (typeof value.executive_summary !== "string" || value.executive_summary.length > 100_000))
          || (value.finding_ids !== undefined && !validReportReferences(value.finding_ids))
          || (value.observation_ids !== undefined && !validReportReferences(value.observation_ids))) return undefined;
      return {kind: "report_update", projectId, reportId, expectedRevision: revision,
        ...(value.title !== undefined ? {title: value.title as string} : {}),
        ...(value.status !== undefined ? {status: value.status as "draft" | "review"} : {}),
        ...(value.executive_summary !== undefined ? {executiveSummary: value.executive_summary as string} : {}),
        ...(value.finding_ids !== undefined ? {findingIds: value.finding_ids as string[]} : {}),
        ...(value.observation_ids !== undefined ? {observationIds: value.observation_ids as string[]} : {})};
    }
    if (operation === "nebula.reports.transform_note" && value.kind === "report_transform_note" && revision
        && typeof value.observation_id === "string" && uuidPattern.test(value.observation_id)
        && Number.isInteger(value.expected_source_revision) && Number(value.expected_source_revision) >= 1
        && typeof value.instruction === "string" && value.instruction.trim().length > 0 && value.instruction.length <= 4000) {
      return {kind: "report_transform_note", projectId, reportId, expectedRevision: revision,
        observationId: value.observation_id, expectedSourceRevision: Number(value.expected_source_revision),
        instruction: value.instruction.trim()};
    }
    if (revision && operation === "nebula.reports.signoff" && value.kind === "report_signoff") {
      return {kind: "report_signoff", projectId, reportId, expectedRevision: revision};
    }
    if (revision && operation === "nebula.reports.delete" && value.kind === "report_delete"
        && typeof value.title === "string" && value.title.trim().length > 0 && value.title.length <= 500) {
      return {kind: "report_delete", projectId, reportId, expectedRevision: revision, title: value.title};
    }
    if (revision && operation === "nebula.reports.export_pdf" && value.kind === "report_export_pdf") {
      return {kind: "report_export_pdf", projectId, reportId, expectedRevision: revision};
    }
    return undefined;
  }
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
    const indexKind = operation === "nebula.notebook.connect_file_index" ? "notebook_index_connect"
      : operation === "nebula.notebook.remove_file_index" ? "notebook_index_remove" : undefined;
    if (indexKind && value.kind === indexKind && Number.isInteger(value.expected_project_revision)
        && Number(value.expected_project_revision) >= 1) {
      return {kind: indexKind, projectId, expectedProjectRevision: Number(value.expected_project_revision)};
    }
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
  if (projectAction && operation === "nebula.evidence.upload" && value.kind === "evidence_upload"
      && typeof projectId === "string" && value.project_id === projectId) {
    return {kind: "evidence_upload", projectId};
  }
  if (projectAction && operation === "nebula.evidence.download" && value.kind === "evidence_download"
      && typeof projectId === "string" && value.project_id === projectId
      && typeof value.evidence_id === "string" && uuidPattern.test(value.evidence_id)
      && Number.isInteger(value.expected_revision) && Number(value.expected_revision) >= 1
      && typeof value.title === "string" && value.title.trim().length > 0 && value.title.length <= 500
      && typeof value.expected_sha256 === "string" && /^[0-9a-f]{64}$/.test(value.expected_sha256)) {
    return {kind: "evidence_download", projectId, evidenceId: value.evidence_id,
      expectedRevision: Number(value.expected_revision), title: value.title,
      expectedSha256: value.expected_sha256};
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
    if (operation === "nebula.projects.create_linked" && value.kind === "project_create_linked" && validName
        && typeof projectId === "string"
        && typeof value.description === "string" && value.description.length <= 20_000
        && typeof value.client_name === "string" && value.client_name.length <= 300
        && typeof value.request_id === "string" && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(value.request_id)) {
      return { kind: "project_create_linked", sourceProjectId: projectId,
        name, description: value.description, clientName: value.client_name, requestId: value.request_id };
    }
    const target = typeof value.project_id === "string" && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(value.project_id) ? value.project_id : undefined;
    const revision = Number.isInteger(value.expected_revision) && Number(value.expected_revision) >= 1 ? Number(value.expected_revision) : undefined;
    if (target && revision && validName) {
      if (operation === "nebula.projects.relink" && value.kind === "project_relink" && typeof projectId === "string") {
        return {kind: "project_relink", sourceProjectId: projectId,
          projectId: target, expectedRevision: revision, name};
      }
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
  // An action on another resource may mention Butler incidentally. Let the
  // model handle it unless the operator explicitly asked for settings.
  if (!/\b(?:setting|settings|controls)\b/.test(text)
      && /\b(?:reports?|sign off|exports?|downloads?|archive|delete|create|edit|save|run)\b/.test(text)) return false;
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
