import { describe, expect, it } from "vitest";
import type { HarnessActivityEvent, HarnessSessionActivity } from "../api/types";
import {
  finalAssistantContent,
  groupHarnessActivityItems,
  harnessActivityItemTier,
  harnessActivityTier,
  harnessCostLabel,
  isSameHarnessSessionActivity,
  isTimelineActivity,
  reasoningSummaryState,
  reasoningSummaryText,
  reduceHarnessActivity,
  shouldShowActivityItem,
  shouldShowActivityKind,
  summarizeHarnessActivity,
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

  it("shows commentary, tools, and operator-action states", () => {
    expect(isTimelineActivity({
      ...activity("output_delta"),
      itemKind: "reasoning",
      title: "Commentary",
    })).toBe(true);
    expect(isTimelineActivity({
      ...activity("tool_started"),
      itemKind: "tool",
    })).toBe(true);
    expect(isTimelineActivity(activity("approval_required"))).toBe(true);
    expect(isTimelineActivity(activity("interaction"))).toBe(true);
    expect(isTimelineActivity(activity("checkpoint"))).toBe(true);
    expect(isTimelineActivity(activity("error"))).toBe(true);
    expect(isTimelineActivity({
      ...activity("item_upsert"),
      itemKind: "plan",
      itemStatus: "failed",
    })).toBe(true);
  });

  it("routes lifecycle context to details and unknown protocol traffic to diagnostics", () => {
    expect(isTimelineActivity(activity("notice", {
      method: "_x.ai/session_notification",
    }))).toBe(false);
    expect(harnessActivityTier(activity("notice", { method: "_x.ai/session_notification" }))).toBe("diagnostics");
    expect(harnessActivityTier({ ...activity("item_upsert"), itemKind: "plan" })).toBe("details");
    expect(harnessActivityTier({ ...activity("item_upsert"), itemKind: "mode" })).toBe("details");
    expect(harnessActivityTier({ ...activity("item_upsert"), itemKind: "goal" })).toBe("details");
    expect(harnessActivityTier({ ...activity("item_upsert"), itemKind: "hook" })).toBe("details");
    expect(harnessActivityTier(activity("usage"))).toBe("details");
    expect(isTimelineActivity(activity("usage"))).toBe(true);
  });

  it("applies the sparse timeline consistently to Codex", () => {
    expect(isTimelineActivity({
      ...activity("notice", { method: "thread/status/changed" }),
      vendor: "codex_app_server",
    })).toBe(false);
    expect(harnessActivityTier({
      ...activity("item_upsert"),
      vendor: "codex_app_server",
      itemKind: "plan",
    })).toBe("details");
    expect(isTimelineActivity({
      ...activity("output_delta"),
      vendor: "codex_app_server",
      itemKind: "reasoning",
      title: "Commentary",
    })).toBe(true);
    expect(isTimelineActivity({
      ...activity("tool_started"),
      vendor: "codex_app_server",
      itemKind: "command",
    })).toBe(true);
    expect(isTimelineActivity({
      ...activity("approval_required"),
      vendor: "codex_app_server",
    })).toBe(true);
  });

  it("creates the same calm turn summary for either vendor", () => {
    const items: Parameters<typeof summarizeHarnessActivity>[0] = [
      {
        assistantId: "assistant-1",
        key: "commentary",
        kind: "reasoning" as const,
        type: "output_delta",
        vendor: "grok_acp" as const,
        status: "completed",
        title: "Commentary",
        sequence: 1,
        streams: { commentary: "Checking the workspace." },
        payload: {},
        artifactIds: [],
      },
      {
        assistantId: "assistant-1",
        key: "command",
        kind: "command" as const,
        type: "tool_completed",
        vendor: "codex_app_server" as const,
        status: "completed",
        title: "Run tests",
        sequence: 2,
        streams: {},
        payload: {},
        artifactIds: [],
      },
      {
        assistantId: "assistant-1",
        key: "usage",
        type: "usage",
        vendor: "codex_app_server" as const,
        title: "Usage",
        sequence: 3,
        streams: {},
        payload: {},
        artifactIds: [],
        usage: {
          inputTokens: 10,
          outputTokens: 5,
          totalTokens: 15,
          cachedInputTokens: 0,
          cacheCreationTokens: 0,
          cacheReadTokens: 0,
          reasoningTokens: 0,
          costUsd: 0,
          durationMs: 3200,
          turnCount: 1,
          modelUsage: {},
          rateLimit: {},
        },
      },
    ];

    expect(items.map(harnessActivityItemTier)).toEqual(["transcript", "transcript", "details"]);
    expect(summarizeHarnessActivity(items)).toBe("1 action · commentary · 3.2s");
  });

  it("keeps negotiated mode, plan, and goal state on normalized activity items", () => {
    const [item] = reduceHarnessActivity([], {
      ...activity("item_upsert"),
      sequence: 3,
      vendor: "codex_app_server",
      harnessTurnId: "turn-controls",
      itemId: "turn-plan",
      itemKind: "plan",
      mode: "plan",
      plan: [{ id: "step-1", title: "Inspect", status: "in_progress" }],
      goal: {
        objective: "Ship the adapter",
        status: "running",
        progress: 0.5,
        childAgents: 1,
      },
      title: "Plan",
    }, "assistant-controls");

    expect(item.mode).toBe("plan");
    expect(item.plan?.[0].status).toBe("in_progress");
    expect(item.goal?.objective).toBe("Ship the adapter");
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

  it("groups reasoning and commentary by parent without grouping tool cards", () => {
    const base = {
      assistantId: "assistant-1",
      type: "item_upsert",
      vendor: "codex_app_server" as const,
      status: "completed",
      sequence: 1,
      streams: {},
      payload: {},
      artifactIds: [],
    };
    const reasoning = {
      ...base,
      key: "reasoning-1",
      itemId: "reasoning-1",
      kind: "reasoning" as const,
      title: "Reasoning",
    };
    const tool = {
      ...base,
      key: "tool-1",
      itemId: "tool-1",
      kind: "tool" as const,
      title: "Knowledge list",
      sequence: 2,
    };
    const commentary = {
      ...base,
      key: "commentary-1",
      itemId: "commentary-1",
      kind: "reasoning" as const,
      title: "Commentary",
      sequence: 3,
      streams: { commentary: "I’ll list the available sources." },
    };
    const nestedReasoning = {
      ...base,
      key: "reasoning-nested",
      itemId: "reasoning-nested",
      parentItemId: "subagent-1",
      kind: "reasoning" as const,
      title: "Reasoning",
      sequence: 4,
    };

    const groups = groupHarnessActivityItems([
      reasoning,
      tool,
      commentary,
      nestedReasoning,
    ]);

    expect(groups.map((group) => group.type)).toEqual([
      "narrative",
      "item",
      "narrative",
    ]);
    expect(groups[0].items.map((item) => item.key)).toEqual([
      "reasoning-1",
      "commentary-1",
    ]);
    expect(groups[1].items).toEqual([tool]);
    expect(groups[2].items).toEqual([nestedReasoning]);
  });

  it("labels catalog-derived Codex cost as an API-equivalent estimate", () => {
    const usage = {
      inputTokens: 3,
      outputTokens: 2,
      totalTokens: 5,
      cachedInputTokens: 0,
      cacheCreationTokens: 0,
      cacheReadTokens: 0,
      reasoningTokens: 0,
      costUsd: 0.0000375,
      turnCount: 0,
      modelUsage: {},
      rateLimit: {},
    };
    const base = {
      assistantId: "assistant-1",
      key: "turn-1:usage",
      type: "usage",
      title: "usage",
      sequence: 1,
      streams: {},
      payload: {},
      artifactIds: [],
      usage,
    };

    expect(harnessCostLabel({ ...base, vendor: "codex_app_server" })).toBe(
      "≈$0.000038 API equivalent",
    );
    expect(harnessCostLabel({ ...base, vendor: "claude_agent_sdk" })).toBe(
      "$0.000038",
    );
    expect(harnessCostLabel({ ...base, usage: { ...usage, costUsd: 0 } })).toBeUndefined();
  });
});
