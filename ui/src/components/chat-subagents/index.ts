/**
 * Delegated subagents in a conversation: children on the chat's own model in
 * a provider chat, or on a chosen provider model in a harness chat.
 *
 * Core starts, runs and finishes them; these surfaces only show what it holds
 * and carry the two decisions an operator has over one — approve the command
 * it is waiting on, or stop it.
 */

export { ChatSubagentPane } from "./ChatSubagentPane";
export { ChatSubagentAttention } from "./ChatSubagentAttention";
export { ChatSubagentRail } from "./ChatSubagentRail";
export { ChatSubagentResultCard } from "./ChatSubagentResultCard";
export {
  defaultSubagentChoice,
  HarnessSubagentSettings,
  subagentProviders,
  type HarnessSubagentChoice,
} from "./HarnessSubagentSettings";
export { SubagentLimitField, subagentLimitLabel } from "./SubagentLimitField";
export {
  ACTIVE_STATUSES,
  compactTokens,
  elapsedLabel,
  statusLabel,
  subagentSummary,
  useChatSubagents,
  type ChatSubagentState,
} from "./useChatSubagents";
