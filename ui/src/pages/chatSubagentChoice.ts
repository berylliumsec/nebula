import type { ChatCompletionRequest } from "../api/types";
import type { ReasoningEffort } from "../api/types";

/** The composer's Subagents choice. */
export interface ComposerSubagentChoice {
  enabled: boolean;
  /** Harness chats: the chosen provider model passed the tool check, so this turn can use it. */
  ready: boolean;
  providerId: string;
  model: string;
  /** How many subagents may run at once; absent means no limit. */
  limit?: number;
  reasoningEffort?: ReasoningEffort;
}

type SubagentRequestFields = Pick<
  ChatCompletionRequest,
  "allowSubagents" | "subagentProviderId" | "subagentModel" | "maxActiveSubagents" | "pendingProviderSubagent" | "subagentReasoningEffort"
>;

/**
 * The subagent fields a message (or a new goal conversation) carries.
 *
 * A provider chat's subagents share its own model. A harness chat's run on a
 * chosen provider model, which Core accepts only once it is verified; until
 * then the turn runs without them and carries the choice for Core to remember,
 * so a chat this message creates keeps the box checked.
 */
export function subagentRequestFields(runtimeKind: "provider" | "harness", choice: ComposerSubagentChoice): SubagentRequestFields {
  if (runtimeKind === "provider") {
    return { allowSubagents: choice.enabled, maxActiveSubagents: choice.enabled ? choice.limit : undefined, subagentReasoningEffort: choice.enabled ? choice.reasoningEffort : undefined };
  }
  if (choice.enabled && choice.ready) {
    return { allowSubagents: true, subagentProviderId: choice.providerId, subagentModel: choice.model, maxActiveSubagents: choice.limit, subagentReasoningEffort: choice.reasoningEffort };
  }
  if (!choice.enabled) return { allowSubagents: false };
  return {
    allowSubagents: false,
    pendingProviderSubagent: { providerId: choice.providerId, model: choice.model, maxActive: choice.limit, reasoningEffort: choice.reasoningEffort },
  };
}
