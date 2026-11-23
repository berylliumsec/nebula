import { describe, expect, it } from "vitest";
import type { ChatSessionSummary } from "./api/types";
import { BUTLER_CASE_STATUSES, butlerCaseStage, butlerConversationStage, parseButlerCaseStatus } from "./butlerStage";

const chat = (overrides: Partial<ChatSessionSummary> = {}): ChatSessionSummary => ({
  id: "chat", engagementId: "project", title: "Chat", backend: "provider", toolsEnabled: false,
  mcpServerIds: [], hookIds: [], createdAt: "2026-11-23T10:00:00Z", updatedAt: "2026-11-23T10:00:00Z", revision: 1,
  ...overrides,
});

describe("Butler conversation stage", () => {
  it("maps every case status to one of five case stages", () => {
    expect(Object.fromEntries(BUTLER_CASE_STATUSES.map(status => [status, butlerCaseStage(status).label]))).toEqual({
      observed: "Investigating", investigating: "Investigating", verifying: "Investigating",
      needs_attention: "Needs you", uncertain: "Uncertain", resolved: "Resolved", cancelled: "Closed",
    });
    for (const status of BUTLER_CASE_STATUSES) expect(butlerCaseStage(status).description.length).toBeGreaterThan(10);
  });

  it("reads a case chat's stage from its case, whatever its turn is doing", () => {
    const caseChat = chat({ isControlAssistant: true, butlerRecoveryCaseId: "case-1", butlerCaseStatus: "uncertain" });
    expect(butlerConversationStage(caseChat, "working")?.key).toBe("uncertain");
    // Core has not reported the case: no guess.
    expect(butlerConversationStage({ ...caseChat, butlerCaseStatus: undefined }, "working")).toBeUndefined();
  });

  it("reads the Butler chat and threads from activity, and labels nothing else", () => {
    const butler = chat({ isControlAssistant: true });
    expect(butlerConversationStage(butler, "waiting")?.label).toBe("Needs you");
    expect(butlerConversationStage(butler, "working")?.label).toBe("Active");
    expect(butlerConversationStage({ ...butler, isButlerThread: true }, undefined)?.label).toBe("Idle");
    expect(butlerConversationStage(chat(), "working")).toBeUndefined();
    expect(butlerConversationStage(chat({ isButlerInitiated: true }), "working")).toBeUndefined();
    expect(butlerConversationStage(chat({ isControlAssistant: true, isSubagent: true }), "working")).toBeUndefined();
  });

  it("ignores statuses Core does not send", () => {
    expect(parseButlerCaseStatus("needs_attention")).toBe("needs_attention");
    expect(parseButlerCaseStatus("done")).toBeUndefined();
    expect(parseButlerCaseStatus(undefined)).toBeUndefined();
  });
});
