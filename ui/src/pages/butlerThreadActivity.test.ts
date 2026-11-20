import { describe, expect, it } from "vitest";
import type { ChatStreamEvent, ChatTurn } from "../api/types";
import { activityLedgerFromNative } from "../components/activityLedgerModel";
import { activityFromEvent, activityFromPendingTurn, activityFromSavedResults, ledgerStatus } from "./butlerThreadActivity";

const event = (value: Record<string, unknown>) => value as unknown as ChatStreamEvent;

function turn(overrides: Partial<ChatTurn>): ChatTurn {
  return {
    id: "turn-1", sessionId: "thread-1", revision: 1, status: "routing", content: "", reasoning: "",
    toolCallIds: [], recoveryBlocked: false, unresolvedToolCallIds: [], unresolvedHookExecutionIds: [],
    subagentIds: [], recoverable: true, ...overrides,
  };
}

describe("Butler thread activity", () => {
  it("follows a streamed turn from the queue through its tool steps", () => {
    let activity = activityFromEvent(undefined, event({ type: "queued", turnId: "turn-1", queuedAt: "", queuePosition: 2, capacityLane: "direct", detail: "Waiting for Core capacity" }));
    expect(activity).toMatchObject({ state: "queued", detail: "Waiting for Core capacity · position 2", cards: [] });
    expect(ledgerStatus(activity!)).toBe("queued");
    activity = activityFromEvent(activity, event({ type: "admitted", turnId: "turn-1", admittedAt: "", capacityLane: "direct" }));
    expect(activity).toMatchObject({ state: "running", detail: undefined });
    activity = activityFromEvent(activity, event({ type: "tool_started", turnId: "turn-1", toolCallId: "call-1", capability: "chat.read", displayName: "Butler · read chat", arguments: { session_id: "ca89c91a" }, step: 1 }));
    activity = activityFromEvent(activity, event({ type: "tool_completed", turnId: "turn-1", toolCallId: "call-1", capability: "chat.read", displayName: "Butler · read chat", status: "succeeded", summary: "Read 40 messages" }));
    expect(activity!.cards).toEqual([expect.objectContaining({ toolCallId: "call-1", status: "succeeded", summary: "Read 40 messages", arguments: { session_id: "ca89c91a" } })]);
    // Text deltas leave the steps as they are.
    expect(activityFromEvent(activity, event({ type: "delta", delta: "It" }))).toBe(activity);
    const ledger = activityLedgerFromNative("Butler's steps", ledgerStatus(activity!), activity!.cards);
    expect(ledger.entries).toHaveLength(1);
  });

  it("restores a reloaded or resumed turn's steps and queue position from Core", () => {
    expect(activityFromPendingTurn(turn({ status: "queued", queuePosition: 1, admissionReason: "Resumed after Core restarted" })))
      .toMatchObject({ state: "queued", detail: "Resumed after Core restarted · position 1" });
    const running = activityFromPendingTurn(turn({
      toolCallIds: ["call-1", "call-2"],
      toolCalls: [{ toolCallId: "call-1", capability: "goal.read", displayName: "Butler · read goal", status: "succeeded", arguments: {} }],
    }));
    expect(running.state).toBe("running");
    expect(running.cards.map((card) => [card.toolCallId, card.status])).toEqual([["call-1", "succeeded"], ["call-2", "running"]]);
    expect(activityFromPendingTurn(turn({ status: "interrupted", error: "Core restarted" }))).toMatchObject({ state: "failed", detail: "Core restarted" });
    expect(activityFromPendingTurn(turn({ status: "waiting_approval" }))).toMatchObject({ state: "waiting", detail: "Waiting for your approval" });
  });

  it("shows a finished answer's saved steps", () => {
    expect(activityFromSavedResults("m1", undefined)).toEqual([]);
    expect(activityFromSavedResults("m1", [{ toolCallId: "call-1", capability: "goal.resume", status: "succeeded", evidenceIds: [], receipt: {} }]))
      .toEqual([expect.objectContaining({ assistantId: "m1", toolCallId: "call-1", status: "succeeded" })]);
  });
});
