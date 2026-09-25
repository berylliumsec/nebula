import { ApiError } from "../api/client";

/**
 * Tool families whose inputs and results a turn sends to its model. On a cloud
 * runtime, Core accepts such a turn only with the operator's consent for that
 * turn or standing consent on the runtime profile.
 */
export type ToolResultFamily =
  | "Command runtime"
  | "MCP servers"
  | "SSH host"
  | "Subagents"
  | "Agent messaging"
  | "Parent agent messages"
  | "Browser control"
  | "Project model context";

/** Families the operator can turn off from the composer for a turn without tools. */
const OPERATOR_OPTIONAL: ReadonlySet<ToolResultFamily> = new Set([
  "MCP servers",
  "SSH host",
  "Subagents",
  "Agent messaging",
  "Browser control",
  "Project model context",
]);

export interface ProviderTurnTools {
  /** The request's `toolsEnabled`: the project command runtime is ready. */
  commandRuntime: boolean;
  mcpServerIds: readonly string[];
  /** Explicit SSH targets; `undefined` lets the model pick only when commands run. */
  sshEnvironmentIds?: readonly string[];
  allowSubagents: boolean;
  allowAgentMessaging: boolean;
  /** A subagent's own conversation always routes messages to its parent. */
  subagentConversation: boolean;
  browserControl: boolean;
  contextSourceKinds: readonly string[];
}

/**
 * The tool families a provider turn carries, mirroring Core's `tools_enabled`
 * in chat.py for everything the composer can see. Core alone knows about
 * project web search and skill resources; for those it refuses with
 * `tool_result_consent_required` and {@link runWithToolResultConsent} asks.
 */
export function providerTurnToolFamilies(turn: ProviderTurnTools): ToolResultFamily[] {
  const families: ToolResultFamily[] = [];
  if (turn.commandRuntime) families.push("Command runtime");
  if (turn.mcpServerIds.length) families.push("MCP servers");
  if (turn.sshEnvironmentIds?.length) families.push("SSH host");
  // Core ignores both choices inside a subagent's own conversation.
  if (turn.allowSubagents && !turn.subagentConversation) families.push("Subagents");
  if (turn.allowAgentMessaging && !turn.subagentConversation) families.push("Agent messaging");
  if (turn.subagentConversation) families.push("Parent agent messages");
  if (turn.browserControl) families.push("Browser control");
  if (turn.contextSourceKinds.includes("application_model")) families.push("Project model context");
  return families;
}

function spokenList(items: readonly string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/**
 * Explain a declined share: what the turn would have shared and the next valid
 * action. Sending again asks again; when every family is one the operator can
 * turn off, a chat without tools is the other way forward.
 */
export function declinedToolSharingMessage(families: readonly ToolResultFamily[], runtimeName: string): string {
  const optional = families.length > 0 && families.every((family) => OPERATOR_OPTIONAL.has(family));
  const off = families.length === 1 ? "it" : "them";
  return `${spokenList(families)} would share tool results with ${runtimeName}. `
    + `Send again to allow sharing${optional ? `, or turn ${off} off` : ""}.`;
}

/** A text-only runtime cannot receive tool results, whatever the operator answers. */
export function textOnlyToolSharingMessage(families: readonly ToolResultFamily[], runtimeName: string): string {
  const optional = families.length > 0 && families.every((family) => OPERATOR_OPTIONAL.has(family));
  return `${runtimeName} is text-only. Permit project/document data in Settings`
    + `${optional ? `, or turn off ${spokenList(families)}` : ""}.`;
}

/**
 * Lead every refused send with its outcome. The recovery notice shows one
 * bounded line, so the outcome comes first and the whole line stays short.
 */
export function notSentMessage(detail: string, kept: boolean): string {
  return `${kept ? "Not sent; your message is kept." : "Not sent."} ${detail}`;
}

/** The operator declined a share Core asked for; Core accepted nothing. */
export class ToolResultSharingDeclinedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolResultSharingDeclinedError";
  }
}

export function isToolResultConsentRefusal(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === "tool_result_consent_required";
}

/**
 * Send once, and when Core refuses before accepting the turn because it
 * carries tools that need consent, ask the operator and send the same request
 * once more with consent. Core names the tools only it can see (project web
 * search, skill resources), so both sides agree on when to ask.
 */
export async function runWithToolResultConsent<T>(
  request: () => Promise<T>,
  options: {
    /** False when the request already carries consent or the runtime cannot be asked. */
    canAsk: () => boolean;
    ask: () => Promise<boolean>;
    grant: () => void;
    runtimeName: string;
  },
): Promise<T> {
  try {
    return await request();
  } catch (error) {
    if (!isToolResultConsentRefusal(error) || !options.canAsk()) throw error;
    if (!await options.ask()) {
      const cause = error.operatorDetail ?? `This turn's tools would share results with ${options.runtimeName}.`;
      throw new ToolResultSharingDeclinedError(`${cause} Send again to allow sharing.`);
    }
    options.grant();
    return request();
  }
}
