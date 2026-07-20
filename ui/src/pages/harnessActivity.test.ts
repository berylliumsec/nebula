import { describe, expect, it } from "vitest";
import type { HarnessActivityEvent, HarnessSessionActivity } from "../api/types";
import {
  finalAssistantContent,
  isSameHarnessSessionActivity,
  isTimelineActivity,
  reasoningSummaryState,
  reasoningSummaryText,
  reduceHarnessActivity,
  shouldShowActivityItem,
  shouldShowActivityKind,
} from "./harnessActivity";

function activity(
  type: HarnessActivityEvent["type"],
  payload: Record<string, unknown> = {},
): HarnessActivityEvent {
  return {
    schemaVersion: "nebula.harness-activity/v1",
    type,
    artifactIds: [],
    payload,
  };
}

describe("harness activity presentation", () => {
  it("recognizes unchanged authoritative activity polls", () => {
    const activityState: HarnessSessionActivity = {
      sessionId: "session-1",
      sessionStatus: "idle",
      busy: false,
      live: true,
      turnId: "turn-1",
      turnStatus: "complete",
      turnOrigin: "chat",
      startedAt: "2026-07-20T20:00:00Z",
      lastActivityAt: "2026-07-20T20:01:00Z",
      detail: "Harness is ready.",
    };

    expect(isSameHarnessSessionActivity(activityState, { ...activityState })).toBe(true);
    expect(isSameHarnessSessionActivity(activityState, { ...activityState, busy: true })).toBe(false);
    expect(isSameHarnessSessionActivity(undefined, activityState)).toBe(false);
  });

  it("keeps routine turn status out of the assistant timeline", () => {
    expect(isTimelineActivity(activity("turn_status"))).toBe(false);
    expect(isTimelineActivity(activity("status"))).toBe(false);
  });

  it("does not mirror vendor chat messages as tool activity", () => {
    expect(isTimelineActivity(activity("item_upsert", { type: "userMessage" }))).toBe(false);
    expect(isTimelineActivity(activity("item_upsert", { type: "agentMessage" }))).toBe(false);
  });

  it("preserves useful observable work", () => {
    expect(isTimelineActivity(activity("item_upsert", { type: "commandExecution" }))).toBe(true);
    expect(isTimelineActivity(activity("notice", { severity: "warning" }))).toBe(true);
  });

  it("does not erase streamed assistant text with an empty durable frame", () => {
    expect(finalAssistantContent("Visible answer", "")).toBe("Visible answer");
    expect(finalAssistantContent("Partial answer", "Final answer")).toBe("Final answer");
  });

  it("replaces streamed reasoning with the authoritative completed summary", () => {
    const streamed: HarnessActivityEvent = {
      ...activity("output_delta", {
        reasoning_summary_state: "available",
        reasoning_summary_source: "stream",
      }),
      id: "event-1",
      sequence: 1,
      vendor: "codex_app_server",
      harnessTurnId: "turn-1",
      itemId: "reasoning-1",
      itemKind: "reasoning",
      itemStatus: "streaming",
      title: "Reasoning",
      stream: "reasoning_summary",
      delta: "Partial summary",
    };
    const completed: HarnessActivityEvent = {
      ...activity("item_upsert", {
        reasoning_summary_state: "available",
        reasoning_summary_source: "completed_item",
        reasoning_summary_text: "Authoritative summary",
      }),
      id: "event-2",
      sequence: 2,
      vendor: "codex_app_server",
      harnessTurnId: "turn-1",
      itemId: "reasoning-1",
      itemKind: "reasoning",
      itemStatus: "completed",
      title: "Reasoning",
    };

    const live = reduceHarnessActivity(
      reduceHarnessActivity([], streamed, "assistant-1"),
      completed,
      "assistant-1",
    );
    const replayed = [streamed, {
      ...completed,
      summary: "chat · Harness item upsert",
    }].reduce(
      (items, event) => reduceHarnessActivity(items, event, "assistant-1"),
      [] as ReturnType<typeof reduceHarnessActivity>,
    );

    expect(live).toEqual(replayed);
    expect(reasoningSummaryState(live[0])).toBe("available");
    expect(reasoningSummaryText(live[0])).toBe("Authoritative summary");
    expect(live[0].streams.reasoning_summary).toBe("Authoritative summary");
    expect(shouldShowActivityKind(live[0])).toBe(false);

    const duplicate = reduceHarnessActivity(live, completed, "assistant-1");
    expect(duplicate).toBe(live);
    expect(reasoningSummaryText(duplicate[0])).toBe("Authoritative summary");
  });

  it("maps historical private-trace fallbacks to an honest unavailable state", () => {
    const [item] = reduceHarnessActivity([], {
      ...activity("item_upsert", { type: "reasoning" }),
      sequence: 4,
      vendor: "codex_app_server",
      harnessTurnId: "turn-old",
      itemId: "reasoning-old",
      itemKind: "reasoning",
      itemStatus: "completed",
      title: "Reasoning",
      summary: "Codex is reasoning; hidden trace content is not retained.",
    }, "assistant-old");

    expect(item.summary).toBeUndefined();
    expect(reasoningSummaryState(item)).toBe("not_provided");
    expect(reasoningSummaryText(item)).toBeUndefined();
    expect(shouldShowActivityItem(item)).toBe(false);
  });

  it("keeps active, summarized, and malformed reasoning items visible", () => {
    const base = {
      assistantId: "assistant-1",
      key: "turn-1:reasoning-1",
      kind: "reasoning" as const,
      type: "item_upsert",
      vendor: "codex_app_server" as const,
      title: "Reasoning",
      sequence: 1,
      streams: {},
      artifactIds: [],
    };

    expect(shouldShowActivityItem({
      ...base,
      status: "streaming",
      payload: { reasoning_summary_state: "pending" },
    })).toBe(true);
    expect(shouldShowActivityItem({
      ...base,
      status: "completed",
      streams: { reasoning_summary: "Safe summary" },
      payload: { reasoning_summary_state: "available" },
    })).toBe(true);
    expect(shouldShowActivityItem({
      ...base,
      status: "completed",
      payload: {
        reasoning_summary_state: "not_provided",
        reasoning_summary_malformed: true,
      },
    })).toBe(true);
  });

  it("preserves streamed summaries when the completed item has no snapshot", () => {
    const [item] = [
      {
        ...activity("output_delta", {
          reasoning_summary_state: "available",
          reasoning_summary_source: "stream",
        }),
        sequence: 1,
        vendor: "codex_app_server" as const,
        harnessTurnId: "turn-stream",
        itemId: "reasoning-stream",
        itemKind: "reasoning" as const,
        itemStatus: "streaming",
        title: "Reasoning",
        stream: "reasoning_summary",
        delta: "Safe stream",
      },
      {
        ...activity("item_upsert", { reasoning_summary_state: "not_provided" }),
        sequence: 2,
        vendor: "codex_app_server" as const,
        harnessTurnId: "turn-stream",
        itemId: "reasoning-stream",
        itemKind: "reasoning" as const,
        itemStatus: "completed",
        title: "Reasoning",
      },
    ].reduce(
      (items, event) => reduceHarnessActivity(items, event, "assistant-stream"),
      [] as ReturnType<typeof reduceHarnessActivity>,
    );

    expect(reasoningSummaryState(item)).toBe("available");
    expect(reasoningSummaryText(item)).toBe("Safe stream");
  });
});
