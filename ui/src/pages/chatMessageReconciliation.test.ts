import { describe, expect, it } from "vitest";
import {
  reconcileCompletedAssistantMessage,
  type ReconciledConversationMessage,
} from "./chatMessageReconciliation";

const user: ReconciledConversationMessage = {
  id: "user-1", role: "user", content: "Question", createdAt: "2026-01-01T00:00:00Z",
  citations: [], state: "complete", durable: false,
};
const temporary: ReconciledConversationMessage = {
  id: "assistant-temp", role: "assistant", content: "Partial", createdAt: "2026-01-01T00:00:01Z",
  citations: [], state: "streaming", durable: false,
};
const completion = {
  temporaryAssistantId: "assistant-temp",
  durableAssistantId: "assistant-final",
  userId: "user-1",
  content: "Final conclusion",
  citations: [],
  harnessTurnId: "turn-1",
  createdAt: "2026-01-01T00:00:02Z",
};

describe("reconcileCompletedAssistantMessage", () => {
  it("replaces the temporary streaming response", () => {
    const result = reconcileCompletedAssistantMessage([user, temporary], completion);
    expect(result.map((message) => message.id)).toEqual(["user-1", "assistant-final"]);
    expect(result[0].durable).toBe(true);
    expect(result[1]).toMatchObject({ content: "Final conclusion", state: "complete", durable: true });
  });

  it("appends the durable response when chat switching removed the temporary message", () => {
    const result = reconcileCompletedAssistantMessage([user], completion);
    expect(result.map((message) => message.id)).toEqual(["user-1", "assistant-final"]);
    expect(result[1]).toMatchObject({ content: "Final conclusion", harnessTurnId: "turn-1" });
  });

  it("updates an already restored durable response without duplicating it", () => {
    const durable = { ...temporary, id: "assistant-final", content: "Final conclusion", durable: true };
    const result = reconcileCompletedAssistantMessage([user, durable], completion);
    expect(result.filter((message) => message.id === "assistant-final")).toHaveLength(1);
  });
});
