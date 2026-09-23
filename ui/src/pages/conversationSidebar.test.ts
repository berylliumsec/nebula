import { describe, expect, it } from "vitest";
import type { ChatSessionSummary } from "../api/types";
import { groupSidebarConversations } from "./conversationSidebar";

const now = Date.parse("2026-09-22T15:00:00Z");

function session(id: string, overrides: Partial<ChatSessionSummary> = {}): ChatSessionSummary {
  return {
    id,
    engagementId: "project",
    title: id,
    backend: "provider",
    toolsEnabled: false,
    mcpServerIds: [],
    hookIds: [],
    createdAt: "2026-09-22T10:00:00Z",
    updatedAt: "2026-09-22T11:00:00Z",
    revision: 1,
    ...overrides,
  };
}

describe("groupSidebarConversations", () => {
  it("nests only marked subagents and starts their parent collapsed", () => {
    const parent = session("parent", { title: "Main agent" });
    const child = session("child", { title: "Subagent", parentSessionId: parent.id, isSubagent: true });
    const branch = session("branch", { title: "Ordinary branch", parentSessionId: parent.id });
    const result = groupSidebarConversations([child, branch, parent], {}, "", new Set(), now);

    expect(result.groups[0].rows.map(row => row.session.id)).toEqual(["branch", "parent"]);
    expect(result.groups[0].rows.at(-1)).toMatchObject({ childCount: 1, depth: 0, expanded: false });
    expect(result.matchCount).toBe(3);
  });

  it("reveals children directly below their parent when expanded", () => {
    const parent = session("parent");
    const child = session("child", { parentSessionId: parent.id, isSubagent: true });
    const result = groupSidebarConversations([child, parent], {}, "", new Set([parent.id]), now);

    expect(result.groups[0].rows.map(row => [row.session.id, row.depth])).toEqual([["parent", 0], ["child", 1]]);
  });

  it("keeps child waits under supervisor control", () => {
    const parent = session("parent");
    const waiting = session("waiting", { parentSessionId: parent.id, isSubagent: true });
    const working = session("working", { parentSessionId: parent.id, isSubagent: true });
    const result = groupSidebarConversations([parent, waiting, working], { waiting: "waiting", working: "working" }, "", new Set(), now);

    expect(result.groups[0].label).toBe("Working");
    expect(result.groups[0].rows).toHaveLength(1);
    expect(result.groups[0].rows[0]).toMatchObject({ childCount: 2, waitingChildren: 1, workingChildren: 1 });
  });

  it("shows a matching child with its parent while hiding unrelated children", () => {
    const parent = session("parent", { title: "Main agent" });
    const match = session("match", { title: "Audit tokens", parentSessionId: parent.id, isSubagent: true });
    const other = session("other", { title: "Review UI", parentSessionId: parent.id, isSubagent: true });
    const result = groupSidebarConversations([parent, match, other], {}, "audit", new Set(), now);

    expect(result.matchCount).toBe(1);
    expect(result.groups[0].rows.map(row => row.session.id)).toEqual(["parent", "match"]);
    expect(result.groups[0].rows[0].expanded).toBe(true);
    expect(result.groups[0].rows[0].searchRevealed).toBe(true);
  });

  it("keeps an orphaned subagent reachable and updates counts after deletion", () => {
    const parent = session("parent");
    const child = session("child", { parentSessionId: parent.id, isSubagent: true });
    const orphan = session("orphan", { parentSessionId: "missing", isSubagent: true });
    const before = groupSidebarConversations([parent, child, orphan], {}, "", new Set(), now);
    const after = groupSidebarConversations([parent, orphan], {}, "", new Set(), now);

    expect(before.groups[0].rows.map(row => row.session.id)).toEqual(["parent", "orphan"]);
    expect(before.groups[0].rows[0].childCount).toBe(1);
    expect(after.groups[0].rows[0].childCount).toBe(0);
  });
});
