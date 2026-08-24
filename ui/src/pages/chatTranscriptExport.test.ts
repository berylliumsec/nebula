import { describe, expect, it } from "vitest";
import type { ChatSessionSummary, PersistedChatMessage } from "../api/types";
import { chatTranscriptFilename, formatChatTranscript } from "./chatTranscriptExport";

const session: ChatSessionSummary = {
  id: "session-123456789",
  engagementId: "project-1",
  title: "TLS / scope review",
  backend: "harness",
  harnessProfileId: "harness-1",
  parentSessionId: "session-parent",
  forkedFromMessageId: "message-parent",
  model: "gpt-test",
  toolsEnabled: true,
  createdAt: "2026-08-24T10:00:00Z",
  updatedAt: "2026-08-24T10:01:00Z",
  revision: 1,
};

const messages: PersistedChatMessage[] = [{
  id: "message-1",
  engagementId: "project-1",
  sessionId: session.id,
  sequence: 1,
  role: "user",
  content: "Review 443/tcp.",
  citations: [],
  contextAttachments: [{
    sourceKind: "terminal",
    sourceId: "terminal-1",
    sourceLabel: "Nmap result",
    text: "443/tcp open https",
    sha256: "a".repeat(64),
    truncated: false,
  }],
  createdAt: "2026-08-24T10:00:30Z",
  updatedAt: "2026-08-24T10:00:30Z",
}];

describe("assistant transcript export", () => {
  it("records durable lineage and selected-context integrity metadata", () => {
    const output = formatChatTranscript({
      session,
      engagementName: "External assessment",
      messages,
      exportedAt: new Date("2026-08-24T11:00:00Z"),
    });
    expect(output).toContain("Nebula session: `session-123456789`");
    expect(output).toContain("Parent session: `session-parent`");
    expect(output).toContain("Forked from message: `message-parent`");
    expect(output).toContain("SHA-256: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`");
    expect(output).toContain("```text\n443/tcp open https\n```");
    expect(output).toContain("Review 443/tcp.");
    expect(output).toContain("authoritative saved transcript");
  });

  it("builds a bounded portable filename", () => {
    expect(chatTranscriptFilename(session)).toBe("TLS-scope-review-23456789.md");
  });
});
