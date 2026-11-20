import { assistantTurnOptions } from "../components/nebulaAssistantActions";
import type { ButlerBoardItem } from "../api/butlerBoard";
import type { ChatAssistantConfig, ChatCompletionRequest, HarnessProfile, ProviderHealth } from "../api/types";

/**
 * Butler mode threads reuse the project's Butler settings: the runtime, model,
 * tools and subagent choices saved for its current Butler conversation.
 */
export type ButlerThreadRuntime =
  | { ok: true; config: ChatAssistantConfig; allowCloudToolResults: boolean }
  | { ok: false; error: string };

export function butlerThreadRuntime(config: ChatAssistantConfig | undefined, providers: readonly ProviderHealth[], harnesses: readonly HarnessProfile[]): ButlerThreadRuntime {
  if (!config) return { ok: false, error: "Butler isn't set up in this project yet. Open the Butler panel once to choose its model, then retry." };
  const profile = config.backend === "provider"
    ? providers.find((item) => item.id === config.profileId)
    : harnesses.find((item) => item.id === config.profileId);
  if (!profile) return { ok: false, error: "Butler's saved model is no longer available. Choose another in Butler settings, then retry." };
  const local = config.backend === "provider" ? (profile as ProviderHealth).local : (profile as HarnessProfile).localOnly;
  if (!local && !profile.permitsSensitiveData) {
    return { ok: false, error: `${profile.name} is configured for text only. Permit project data for it in Settings to use Butler's tools.` };
  }
  // The profile's saved sensitive-data permission is the standing consent for Butler.
  return { ok: true, config, allowCloudToolResults: !local };
}

export function butlerThreadReservation(projectId: string, runtime: Extract<ButlerThreadRuntime, { ok: true }>, title: string) {
  const { config } = runtime;
  return {
    engagementId: projectId,
    backend: config.backend,
    providerId: config.backend === "provider" ? config.profileId : undefined,
    harnessProfileId: config.backend === "harness" ? config.profileId : undefined,
    model: config.model,
    mcpServerIds: config.mcpServerIds,
    harnessReasoningEffort: config.backend === "harness" ? config.harnessReasoningEffort : undefined,
    harnessServiceTier: config.backend === "harness" ? config.harnessServiceTier : undefined,
    harnessCyberAccessProgram: config.backend === "harness" ? config.harnessCyberAccessProgram : undefined,
    allowCloudToolResults: runtime.allowCloudToolResults,
    thread: true,
    title,
  };
}

export function butlerThreadRequest(
  projectId: string,
  sessionId: string,
  runtime: Extract<ButlerThreadRuntime, { ok: true }>,
  harnesses: readonly HarnessProfile[],
  content: string,
): ChatCompletionRequest {
  const { config } = runtime;
  const turnOptions = assistantTurnOptions({ kind: config.backend, id: config.profileId, model: config.model }, harnesses);
  const harness = config.backend === "harness";
  return {
    controlAssistant: true,
    engagementId: projectId,
    sessionId,
    backend: config.backend,
    providerId: harness ? undefined : config.profileId,
    harnessProfileId: harness ? config.profileId : undefined,
    model: turnOptions.model,
    reasoningEffort: harness ? undefined : config.reasoningEffort ?? turnOptions.reasoningEffort,
    harnessReasoningEffort: harness ? config.harnessReasoningEffort ?? turnOptions.harnessReasoningEffort : undefined,
    harnessServiceTier: harness ? config.harnessServiceTier ?? turnOptions.harnessServiceTier : undefined,
    harnessMode: harness ? config.harnessMode : undefined,
    harnessCyberAccessProgram: harness ? config.harnessCyberAccessProgram : undefined,
    mcpServerIds: config.mcpServerIds,
    hookIds: config.hookIds,
    includeKnowledge: config.includeKnowledge,
    allowCloudKnowledge: config.allowCloudKnowledge,
    allowAgentMessaging: config.allowAgentMessaging,
    allowSubagents: config.allowSubagents,
    maxActiveSubagents: config.maxActiveSubagents,
    subagentProviderId: config.subagentProviderId,
    subagentHarnessId: config.subagentHarnessProfileId,
    subagentModel: config.subagentModel,
    subagentReasoningEffort: config.subagentReasoningEffort,
    messages: [{ role: "user", content }],
    toolsEnabled: config.toolsEnabled,
    allowCloudToolResults: runtime.allowCloudToolResults,
  };
}

/** A short thread title from the operator's question. */
export function butlerThreadTitle(prompt: string): string {
  const line = prompt.replace(/\s+/g, " ").trim();
  return line.length <= 80 ? line || "Butler thread" : `${line.slice(0, 79).trimEnd()}…`;
}

/**
 * What Butler receives when the operator asks about a board card: the chat it
 * can open with its tools, followed by the question.
 */
export function butlerQuestionAbout(item: ButlerBoardItem | undefined, prompt: string): string {
  if (!item) return prompt;
  const status = item.now ? ` Current status: ${item.now}.` : "";
  return `About the chat "${item.title}" (conversation ${item.sessionId} in this project).${status}\n\n${prompt}`;
}

/**
 * The project thread picker's groups: projects with chats that need the
 * operator or are working come first, labelled with their counts; the rest
 * are quiet. Archived projects are left out.
 */
export function projectPickerGroups<T extends { id: string; status?: string }>(
  projects: readonly T[],
  items: readonly Pick<ButlerBoardItem, "state" | "project">[],
): { active: { project: T; label: string }[]; quiet: T[] } {
  const counts = new Map<string, { waiting: number; working: number }>();
  for (const item of items) {
    const entry = counts.get(item.project.id) ?? { waiting: 0, working: 0 };
    if (item.state === "waiting") entry.waiting += 1;
    if (item.state === "working") entry.working += 1;
    counts.set(item.project.id, entry);
  }
  const active: { project: T; label: string }[] = [];
  const quiet: T[] = [];
  for (const project of projects) {
    if (project.status === "archived") continue;
    const entry = counts.get(project.id);
    if (entry?.waiting) active.push({ project, label: `${entry.waiting} need you` });
    else if (entry?.working) active.push({ project, label: `${entry.working} working` });
    else quiet.push(project);
  }
  return { active, quiet };
}

/** Lower-cased words of a search; every word must match. */
function searchTerms(query: string): string[] {
  return query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
}

function matchesTerms(text: string, terms: readonly string[]): boolean {
  const haystack = text.toLocaleLowerCase();
  return terms.every((term) => haystack.includes(term));
}

export interface ProjectPickerOption<T> {
  project: T;
  group: "active" | "quiet";
  /** "2 need you" or "14 working" for an active project. */
  label?: string;
}

/**
 * The project picker's rows for a search: Active before Quiet, each keeping
 * its order, matched on every word of the project's name. Only the first
 * `limit` rows render; `total` says how many matched.
 */
export function filterProjectPicker<T extends { name: string }>(
  groups: { active: readonly { project: T; label: string }[]; quiet: readonly T[] },
  query: string,
  limit = 80,
): { options: ProjectPickerOption<T>[]; total: number } {
  const terms = searchTerms(query);
  const all: ProjectPickerOption<T>[] = [
    ...groups.active.map(({ project, label }) => ({ project, group: "active" as const, label })),
    ...groups.quiet.map((project) => ({ project, group: "quiet" as const })),
  ];
  const matches = terms.length ? all.filter((option) => matchesTerms(option.project.name, terms)) : all;
  return { options: matches.slice(0, limit), total: matches.length };
}

/**
 * The board's chats that match the search box as the operator types: every
 * word must appear in the chat title, its project's name or its model. Core's
 * search then fills in chats the board had not loaded.
 */
export function filterBoardItems<T extends Pick<ButlerBoardItem, "title" | "project" | "model">>(items: readonly T[], query: string): T[] {
  const terms = searchTerms(query);
  if (!terms.length) return [...items];
  return items.filter((item) => matchesTerms(`${item.title} ${item.project.name} ${item.model ?? ""}`, terms));
}
