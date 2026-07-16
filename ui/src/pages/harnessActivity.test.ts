import { describe, expect, it } from "vitest";
import type { HarnessActivityEvent } from "../api/types";
import { finalAssistantContent, isTimelineActivity } from "./harnessActivity";

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
});
