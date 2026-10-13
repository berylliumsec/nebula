/** A consecutive batch of posted subagent results occupies one transcript row. */
export type SubagentTranscriptItem<T> =
  | {kind: "message"; message: T; key: string}
  | {kind: "subagent_results"; messages: T[]; key: string};

export function groupSubagentTranscriptMessages<T extends {id: string; role: string; metadata?: Record<string, unknown>}>(
  messages: T[],
  resultMessageIds: ReadonlySet<string>,
): {items: SubagentTranscriptItem<T>[]; indexByMessageId: Map<string, number>} {
  const items: SubagentTranscriptItem<T>[] = [];
  const indexByMessageId = new Map<string, number>();
  for (const message of messages) {
    const isResult = message.role === "assistant"
      && (message.metadata?.kind === "subagent_result" || resultMessageIds.has(message.id));
    const previous = items.at(-1);
    if (isResult && previous?.kind === "subagent_results") {
      previous.messages.push(message);
    } else if (isResult) {
      items.push({kind: "subagent_results", messages: [message], key: `subagent:${message.id}`});
    } else {
      items.push({kind: "message", message, key: message.id});
    }
    indexByMessageId.set(message.id, items.length - 1);
  }
  return {items, indexByMessageId};
}
