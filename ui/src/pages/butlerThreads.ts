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
