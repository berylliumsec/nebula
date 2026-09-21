import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import {
  savedAssistantState,
  withoutTurnOutcome,
  type ReconciledConversationMessage,
} from "./chatMessageReconciliation";

const entity = { created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z", revision: 1 };

function wireMessage(overrides: Record<string, unknown>) {
  return {
    ...entity,
    engagement_id: "engagement-1",
    session_id: "session-1",
    citations: [],
    metadata: {},
    ...overrides,
  };
}

describe("turn outcome notes", () => {
  it("maps Core's note for a failed or stopped turn and shows it as stopped", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response(JSON.stringify([
      wireMessage({ id: "user-1", sequence: 1, role: "user", content: "Scan the host." }),
      wireMessage({
        id: "note-1",
        sequence: 2,
        role: "assistant",
        content: "Response failed: upstream 500.",
        finish_reason: "interrupted",
        metadata: { kind: "turn_outcome", chat_turn_id: "turn-1", turn_status: "failed" },
      }),
      wireMessage({
        id: "answer-2",
        sequence: 3,
        role: "assistant",
        content: "A complete answer.",
        finish_reason: "stop",
        metadata: { chat_turn_id: "turn-2" },
      }),
    ]), { status: 200 }));
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

    const messages = await client.listChatMessages("session-1");

    expect(messages.map(message => message.outcomeTurnId)).toEqual([undefined, "turn-1", undefined]);
    expect(savedAssistantState(messages[1].finishReason)).toBe("cancelled");
  });

  it("leaves out only the note of the turn the page shows as unfinished", () => {
    const base = { createdAt: "2026-09-21T10:00:00Z", citations: [], durable: true };
    const messages: ReconciledConversationMessage[] = [
      { ...base, id: "user-1", role: "user", content: "Scan", state: "complete" },
      { ...base, id: "note-1", role: "assistant", content: "Response failed.", state: "cancelled", outcomeTurnId: "turn-1" },
      { ...base, id: "user-2", role: "user", content: "Summarise", state: "complete" },
      { ...base, id: "note-2", role: "assistant", content: "Response failed.", state: "cancelled", outcomeTurnId: "turn-2" },
    ];

    expect(withoutTurnOutcome(messages, "turn-2").map(message => message.id)).toEqual(["user-1", "note-1", "user-2"]);
    expect(withoutTurnOutcome(messages, "turn-9")).toEqual(messages);
  });
});
