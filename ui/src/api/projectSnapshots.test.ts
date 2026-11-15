import { describe, expect, it } from "vitest";
import { buildProjectSnapshots } from "./projectSnapshots";
import type { ChatSessionSummary, EngagementSummary } from "./types";

const project = (id: string, status: EngagementSummary["status"] = "active") => ({ id, name: id, description: "", status, tags: [], workEnabled: true, createdAt: "2026-11-01T00:00:00Z", updatedAt: "2026-11-01T00:00:00Z", scopeAssetCount: 0 }) as EngagementSummary;
const chat = (id: string, engagementId: string, updatedAt: string, extra: Partial<ChatSessionSummary> = {}) => ({ id, engagementId, title: id, backend: "provider", toolsEnabled: true, mcpServerIds: [], hookIds: [], createdAt: updatedAt, updatedAt, revision: 1, ...extra }) as ChatSessionSummary;

describe("buildProjectSnapshots", () => {
  it("keeps only active projects that hold an ordinary chat", () => {
    const sessions = new Map([
      ["a", [chat("a1", "a", "2026-11-10T00:00:00Z")]],
      ["paused", [chat("p1", "paused", "2026-11-10T00:00:00Z")]],
      ["butler-only", [chat("b1", "butler-only", "2026-11-10T00:00:00Z", { isControlAssistant: true }), chat("b2", "butler-only", "2026-11-10T00:00:00Z", { isSubagent: true }), chat("b3", "butler-only", "2026-11-10T00:00:00Z", { archivedAt: "2026-11-11T00:00:00Z" })]],
      ["empty", []],
    ]);
    const result = buildProjectSnapshots([project("a"), project("paused", "paused"), project("butler-only"), project("empty")], sessions, new Map(), []);
    expect(result.map(item => item.id)).toEqual(["a"]);
  });

  it("counts live states and pending actions, ranking attention then running then recency", () => {
    const sessions = new Map([
      ["recent", [chat("r1", "recent", "2026-11-14T00:00:00Z")]],
      ["running", [chat("w1", "running", "2026-11-01T00:00:00Z"), chat("w2", "running", "2026-11-02T00:00:00Z")]],
      ["blocked", [chat("x1", "blocked", "2026-10-01T00:00:00Z")]],
    ]);
    const activity = new Map([["running", [{ sessionId: "w1", state: "working" as const }, { sessionId: "w2", state: "waiting" as const }]]]);
    const attention = [{ kind: "approval", actionId: "1", engagementId: "blocked", engagementName: "blocked", sessionId: "x1", sessionTitle: "x1", requestedAt: "2026-11-14T00:00:00Z" }] as never;
    const result = buildProjectSnapshots([project("recent"), project("running"), project("blocked")], sessions, activity, attention);
    expect(result.map(item => item.id)).toEqual(["blocked", "running", "recent"]);
    expect(result[1]).toMatchObject({ chatCount: 2, working: 1, waiting: 1, lastActivityAt: "2026-11-02T00:00:00Z" });
    expect(result[1].chats.map(item => item.id)).toEqual(["w2", "w1"]);
    expect(result[0].needsYou).toBe(1);
  });
});
