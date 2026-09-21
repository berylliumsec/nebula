/**
 * Delegated subagents in a provider conversation.
 *
 * Core starts, runs and finishes them; these surfaces only show what it holds
 * and carry the two decisions an operator has over one — approve the command
 * it is waiting on, or stop it.
 */

export { ChatSubagentPane } from "./ChatSubagentPane";
export { ChatSubagentRail } from "./ChatSubagentRail";
export { ChatSubagentResultCard } from "./ChatSubagentResultCard";
export {
  ACTIVE_STATUSES,
  compactTokens,
  elapsedLabel,
  statusLabel,
  SUBAGENT_SLOTS,
  subagentSummary,
  useChatSubagents,
  type ChatSubagentState,
} from "./useChatSubagents";
