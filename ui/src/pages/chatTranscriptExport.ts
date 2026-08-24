import type { ChatSessionSummary, PersistedChatMessage } from "../api/types";

export interface ChatTranscriptExportInput {
  session: ChatSessionSummary;
  engagementName: string;
  messages: PersistedChatMessage[];
  exportedAt?: Date;
}

function safeFilename(value: string): string {
  const normalized = value.trim().replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return (normalized || "nebula-conversation").slice(0, 120);
}

function line(label: string, value: string | number | undefined): string {
  return `- ${label}: ${value === undefined || value === "" ? "not recorded" : value}`;
}

function markdownFence(value: string): string {
  const longestRun = Math.max(0, ...[...value.matchAll(/`+/g)].map((match) => match[0].length));
  return "`".repeat(Math.max(3, longestRun + 1));
}

export function formatChatTranscript(input: ChatTranscriptExportInput): string {
  const { session, engagementName, messages } = input;
  const exportedAt = (input.exportedAt ?? new Date()).toISOString();
  const output = [
    `# ${session.title}`,
    "",
    line("Nebula session", `\`${session.id}\``),
    line("Project", engagementName),
    line("Runtime", session.backend === "harness" ? "agent harness" : "provider"),
    line("Model", session.model),
    line("Created", session.createdAt),
    line("Updated", session.updatedAt),
    line("Exported", exportedAt),
  ];
  if (session.parentSessionId) output.push(line("Parent session", `\`${session.parentSessionId}\``));
  if (session.forkedFromMessageId) output.push(line("Forked from message", `\`${session.forkedFromMessageId}\``));

  for (const message of messages) {
    output.push(
      "",
      `## ${message.role === "assistant" ? "Assistant" : "Operator"} · message ${message.sequence}`,
      "",
      line("Message ID", `\`${message.id}\``),
      line("Recorded", message.createdAt),
    );
    if (message.harnessTurnId) output.push(line("Harness turn", `\`${message.harnessTurnId}\``));
    if (message.usage?.totalTokens) output.push(line("Tokens", message.usage.totalTokens));
    output.push("", message.content || "_No text content._");

    if (message.contextAttachments.length) {
      output.push("", "### Exact selected context");
      for (const attachment of message.contextAttachments) {
        const fence = markdownFence(attachment.text);
        output.push(
          "",
          `#### ${attachment.sourceLabel} (${attachment.sourceKind})`,
          "",
          line("SHA-256", `\`${attachment.sha256}\``),
          line("Characters", `${attachment.text.length}${attachment.truncated ? " · truncated" : ""}`),
          line("Source ID", attachment.sourceId ? `\`${attachment.sourceId}\`` : undefined),
          "",
          `${fence}text`,
          attachment.text,
          fence,
        );
      }
    }
    if (message.citations.length) {
      output.push("", "### Citations");
      for (const citation of message.citations) {
        output.push(`- ${citation.name}${citation.page ? ` · page ${citation.page}` : ""} · source \`${citation.sourceId}\` · chunk \`${citation.chunkId}\``);
      }
    }
  }
  output.push("", "---", "Exported from Nebula's authoritative saved transcript. Live tool artifacts and raw evidence remain in the project evidence bundle.", "");
  return output.join("\n");
}

export function chatTranscriptFilename(session: ChatSessionSummary): string {
  return `${safeFilename(session.title)}-${safeFilename(session.id).slice(-8) || "session"}.md`;
}
