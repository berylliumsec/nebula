import { describe, expect, it } from "vitest";
import {
  cancelActiveAssistantMessage,
  cancelStreamingAssistantMessage,
  reconcileAuthoritativeMessages,
  reconcileCompletedAssistantMessage,
  recoverHarnessHistory,
  savedAssistantState,
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
    expect(result[1]).toMatchObject({ content: "Final conclusion", state: "complete", durable: true, runtimeId: "assistant-temp" });
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

  it("keeps the saved progress boundary when the streamed turn completes", () => {
    const progress = "Checking 🔎 sources.";
    const result = reconcileCompletedAssistantMessage([user, { ...temporary, content: progress }], {
      ...completion,
      content: `${progress}\n\nFinal answer.`,
      progressPrefixUtf16Length: progress.length,
    });
    expect(result[1].metadata?.progress_prefix_utf16_length).toBe(progress.length);
    expect(result[1].content).toBe(`${progress}\n\nFinal answer.`);
  });
});

describe("reconcileAuthoritativeMessages", () => {
  it("keeps the completed row identity while authoritative content and metadata replace the stream", () => {
    const completed = reconcileCompletedAssistantMessage([user, temporary], completion);
    const authoritative: ReconciledConversationMessage = {
      ...completed[1], runtimeId: undefined, content: "Core's saved answer", metadata: {saved: true},
    };
    const result = reconcileAuthoritativeMessages(completed, [authoritative]);
    expect(result).toEqual([{...authoritative, runtimeId: "assistant-temp"}]);
    expect(reconcileAuthoritativeMessages(result, [authoritative])).toEqual(result);
    expect(authoritative.runtimeId).toBeUndefined();
  });

  it("does not transfer disclosure identity to a different native turn or message role", () => {
    const previous = {...temporary, id: "assistant-final", runtimeId: "assistant-temp", harnessTurnId: "turn-1"};
    const nextTurn = {...previous, runtimeId: undefined, harnessTurnId: "turn-2"};
    const nextRole = {...previous, runtimeId: undefined, role: "user" as const};
    expect(reconcileAuthoritativeMessages([previous], [nextTurn])[0]).toBe(nextTurn);
    expect(reconcileAuthoritativeMessages([previous], [nextRole])[0]).toBe(nextRole);
  });

  it("honors Core ordering and removal without transferring identity to a copied message", () => {
    const previous = {...temporary, id: "assistant-final", runtimeId: "assistant-temp", harnessTurnId: "turn-1"};
    const copied = {...previous, id: "assistant-copy", sourceMessageId: previous.id, runtimeId: undefined};
    const result = reconcileAuthoritativeMessages([user, previous], [copied, user]);
    expect(result).toEqual([copied, user]);
    expect(result[0]).toBe(copied);
    expect(result.some(message => message.id === previous.id)).toBe(false);
  });
});


describe("recoverHarnessHistory", () => {
  it("retains every stopped turn in order while a later turn is active, with stable reload identities", async () => {
    const history = ["old", "second", "active"].map(id => ({...user, id, harnessTurnId: id}));
    const lookup = async (id: string) => ({id, status: id === "active" ? "running" : "cancelled"});
    const recovered = await recoverHarnessHistory(history, lookup);
    expect(recovered.map(message => message.id)).toEqual(["old", "assistant-harness-recovery-old", "second", "assistant-harness-recovery-second", "active"]);
    expect(recovered[1]).toMatchObject({state: "cancelled", durable: false, recoveredHarnessTurn: true});
    expect(recovered[1].detail).toBeUndefined();
    expect(await recoverHarnessHistory(recovered, lookup)).toEqual(recovered);
    expect(await recoverHarnessHistory(history, lookup)).toEqual(recovered);
  });
  it("keeps saved assistant messages and preserves actual failure details", async () => {
    const result = await recoverHarnessHistory([{...user, harnessTurnId: "saved"}, {...temporary, harnessTurnId: "saved"}, {...user, id: "failed", harnessTurnId: "failed"}], async id => ({id, status: "failed", error: "Runtime unavailable"}));
    expect(result).toHaveLength(4);
    expect(result[1]).toEqual({...temporary, harnessTurnId: "saved"});
    expect(result[3]).toMatchObject({state: "error", detail: "Runtime unavailable"});
  });
});

describe("cancelStreamingAssistantMessage", () => {
  it("settles the live response as stopped with its elapsed time and partial content", () => {
    const result = cancelStreamingAssistantMessage([user, temporary], "assistant-temp", undefined, Date.parse("2026-01-01T00:00:31Z"));
    expect(result[1]).toMatchObject({
      id: "assistant-temp", content: "Partial", state: "cancelled", elapsedMs: 30_000, detail: "Response stopped by the operator.",
    });
    expect(result[0]).toBe(user);
  });

  it("settles the newest live bubble when the cancelled stream event loses the stop race", () => {
    const older = { ...temporary, id: "assistant-old", state: "complete" as const };
    const result = cancelActiveAssistantMessage(
      [older, user, temporary],
      undefined,
      Date.parse("2026-01-01T00:00:31Z"),
    );
    expect(result[0]).toBe(older);
    expect(result[2]).toMatchObject({ state: "cancelled", detail: "Response stopped by the operator." });
  });
});

describe("savedAssistantState", () => {
  it("keeps the saved partial answer of a stopped harness turn marked as stopped after reload", async () => {
    expect(savedAssistantState("interrupted")).toBe("cancelled");
    expect(savedAssistantState("stop")).toBe("complete");
    expect(savedAssistantState(undefined)).toBe("complete");
    const partial: ReconciledConversationMessage = {
      ...temporary, id: "assistant-partial", durable: true, harnessTurnId: "stopped",
      state: savedAssistantState("interrupted"),
    };
    const recovered = await recoverHarnessHistory(
      [{...user, harnessTurnId: "stopped"}, partial],
      async id => ({id, status: "cancelled"}),
    );
    // The saved answer represents the stopped turn: no empty recovery bubble is added.
    expect(recovered.map(message => message.id)).toEqual(["user-1", "assistant-partial"]);
    expect(recovered[1]).toMatchObject({content: "Partial", state: "cancelled"});
  });
});
