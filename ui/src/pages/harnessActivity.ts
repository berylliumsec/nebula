import type { HarnessActivityEvent } from "../api/types";

const CHAT_CHROME_EVENT_TYPES = new Set([
  "message_delta",
  "started",
  "status",
  "turn_status",
  "completed",
]);

export function isTimelineActivity(event: HarnessActivityEvent): boolean {
  if (CHAT_CHROME_EVENT_TYPES.has(event.type)) return false;
  const vendorItemType = typeof event.payload.type === "string" ? event.payload.type : undefined;
  return vendorItemType !== "userMessage" && vendorItemType !== "agentMessage";
}

export function finalAssistantContent(streamed: string, durable: string): string {
  return durable.trim() ? durable : streamed;
}
