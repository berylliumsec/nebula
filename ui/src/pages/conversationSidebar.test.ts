import { describe, expect, it } from "vitest";
import type { ChatSessionSummary } from "../api/types";
import { groupSidebarConversations, sidebarActivity } from "./conversationSidebar";

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
  it("groups running chats above pinned, blocked, paused and recent chats", () => {
    const sessions = [session("pinned", { pinned: true }), session("blocked"), session("paused"),
      session("running-old", { updatedAt: "2026-09-01T10:00:00Z" }), session("running-new"),
      session("archived", { archivedAt: "2026-09-22T12:00:00Z" })];
    const result = groupSidebarConversations(sessions, {
      blocked: "waiting", paused: "idle", "running-old": "working", "running-new": "working", archived: "working",
    }, "", new Set(), now);
    expect(result.groups.map(group => group.label)).toEqual(["Working", "Pinned", "Needs you", "Today", "Archived"]);
    expect(result.groups[0].rows.map(row => row.session.id)).toEqual(["running-new", "running-old"]);
  });

  it("moves a pinned chat into Working only while it runs", () => {
    const pinned = session("pinned", { pinned: true });
    for (const state of ["working", "waiting", "idle"] as const) {
      const result = groupSidebarConversations([pinned], { pinned: state }, "", new Set(), now);
      expect(result.groups[0].label).toBe(state === "working" ? "Working" : "Pinned");
      expect(result.groups[0].rows[0].session.pinned).toBe(true);
    }
  });

  it("keeps running Butler work ahead of blocked siblings and promotes their parent", () => {
    const butler = session("butler", { isControlAssistant: true });
    const blocked = session("blocked", { isButlerInitiated: true });
    const running = session("running", { isButlerInitiated: true, updatedAt: "2026-09-01T10:00:00Z" });
    const result = groupSidebarConversations([butler, blocked, running], { blocked: "waiting", running: "working" }, "", new Set([butler.id]), now);
    expect(result.groups[0].label).toBe("Working");
    expect(result.groups[0].rows.map(row => row.session.id)).toEqual(["butler", "running", "blocked"]);
    expect(result.groups[0].rows[0].waitingChildren).toBe(1);
  });

  it("sorts running subagents first without detaching them or hiding search matches", () => {
    const parent = session("parent");
    const idle = session("idle", { isSubagent: true, parentSessionId: parent.id });
    const running = session("running", { isSubagent: true, parentSessionId: parent.id, updatedAt: "2026-09-01T10:00:00Z" });
    const result = groupSidebarConversations([parent, idle, running], { running: "working" }, "", new Set([parent.id]), now);
    expect(result.groups[0].rows.map(row => [row.session.id, row.depth])).toEqual([["parent", 0], ["running", 1], ["idle", 1]]);
    const searched = groupSidebarConversations([parent, idle, running], { running: "working" }, "idle", new Set(), now);
    expect(searched.groups[0].rows.map(row => row.session.id)).toEqual(["parent", "idle"]);
  });

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

  it("groups Butler-created chats under Butler while preserving their subagents and ordinary branches", () => {
    const butler = session("butler", { title: "Butler", isControlAssistant: true });
    const recovery = session("recovery", { title: "Recover paused goal", isButlerInitiated: true });
    const subagent = session("subagent", { title: "Proof", parentSessionId: recovery.id, isSubagent: true });
    const ordinary = session("ordinary", { title: "Operator chat" });
    const branch = session("branch", { title: "Ordinary branch", parentSessionId: recovery.id });
    const collapsed = groupSidebarConversations([recovery, subagent, ordinary, branch, butler], {}, "", new Set(), now);
    expect(collapsed.groups[0].rows.map(row => row.session.id)).toEqual(["ordinary", "branch", "butler"]);
    expect(collapsed.groups[0].rows.at(-1)).toMatchObject({ childCount: 1, childKind: "chats", expanded: false });

    const expanded = groupSidebarConversations([recovery, subagent, ordinary, branch, butler], {}, "", new Set([butler.id, recovery.id]), now);
    expect(expanded.groups[0].rows.slice(-3).map(row => [row.session.id, row.depth])).toEqual([
      ["butler", 0], ["recovery", 1], ["subagent", 2],
    ]);
  });

  it("reveals only the matching Butler chat and subagent through search", () => {
    const butler = session("butler", { title: "Butler", isControlAssistant: true });
    const recovery = session("recovery", { title: "Recover paused goal", isButlerInitiated: true });
    const subagent = session("subagent", { title: "Inspect libwebrtc", parentSessionId: recovery.id, isSubagent: true });
    const other = session("other", { title: "Recover Apple Music", isButlerInitiated: true });
    const result = groupSidebarConversations([butler, recovery, subagent, other], {}, "libwebrtc", new Set(), now);
    expect(result.matchCount).toBe(1);
    expect(result.groups[0].rows.map(row => row.session.id)).toEqual(["butler", "recovery", "subagent"]);
    expect(result.groups[0].rows.slice(0, 2).every(row => row.expanded && row.searchRevealed)).toBe(true);
  });

  it("keeps Butler-created chats visible if Butler is absent from the loaded page", () => {
    const recovery = session("recovery", { isButlerInitiated: true });
    const result = groupSidebarConversations([recovery], {}, "", new Set(), now);
    expect(result.groups[0].rows.map(row => [row.session.id, row.depth])).toEqual([["recovery", 0]]);
  });

  it("keeps Butler's own subagents beside its initiated chats", () => {
    const butler = session("butler", { isControlAssistant: true });
    const chat = session("chat", { isButlerInitiated: true });
    const agent = session("agent", { parentSessionId: butler.id, isSubagent: true });
    const result = groupSidebarConversations([butler, chat, agent], {}, "", new Set([butler.id]), now);
    expect(result.groups[0].rows.map(row => [row.session.id, row.depth])).toEqual([
      ["butler", 0], ["chat", 1], ["agent", 1],
    ]);
    expect(result.groups[0].rows[0].childCount).toBe(2);
  });

  it("keeps Butler as the owner when a newer Butler-mode subagent exists", () => {
    const butler = session("butler", { isControlAssistant: true, updatedAt: "2026-09-22T10:00:00Z" });
    const chat = session("chat", { isButlerInitiated: true });
    const agent = session("agent", { parentSessionId: butler.id, isSubagent: true, isControlAssistant: true, updatedAt: "2026-09-22T14:00:00Z" });
    const result = groupSidebarConversations([agent, chat, butler], {}, "", new Set([butler.id]), now);
    const rows = result.groups[0].rows.map(row => [row.session.id, row.depth]);
    expect(rows[0]).toEqual(["butler", 0]);
    expect(rows.slice(1).sort()).toEqual([["agent", 1], ["chat", 1]]);
  });

  it("surfaces recent and waiting Butler work at the parent row", () => {
    const butler = session("butler", { isControlAssistant: true, updatedAt: "2026-09-01T10:00:00Z" });
    const chat = session("chat", { isButlerInitiated: true, updatedAt: "2026-09-22T14:00:00Z" });
    const recent = groupSidebarConversations([butler, chat], {}, "", new Set(), now);
    expect(recent.groups[0].label).toBe("Today");
    const waiting = groupSidebarConversations([butler, chat], { chat: "waiting" }, "", new Set(), now);
    expect(waiting.groups[0].label).toBe("Needs you");
    expect(waiting.groups[0].rows[0].waitingChildren).toBe(1);
  });

  it("keeps archived Butler chats in Butler's own collapsed archive", () => {
    const butler = session("butler", { isControlAssistant: true });
    const active = session("active", { isButlerInitiated: true });
    const archived = session("archived", { isButlerInitiated: true, archivedAt: "2026-09-22T12:00:00Z" });
    const collapsed = groupSidebarConversations([butler, active, archived], {}, "", new Set([butler.id]), now);
    expect(collapsed.groups[0].rows.map(row => row.session.id)).toEqual(["butler", "active", "butler"]);
    expect(collapsed.groups[0].rows.at(-1)).toMatchObject({ archiveToggle: true, archivedChildren: 1, expanded: false });
    expect(collapsed.groups.some(group => group.label === "Archived")).toBe(false);

    const open = groupSidebarConversations([butler, active, archived], {}, "", new Set([butler.id]), now, new Set([butler.id]));
    expect(open.groups[0].rows.map(row => row.session.id)).toEqual(["butler", "active", "butler", "archived"]);
    const searched = groupSidebarConversations([butler, active, archived], {}, "archived", new Set(), now);
    expect(searched.groups[0].rows.map(row => row.session.id)).toEqual(["butler", "butler", "archived"]);
    const onlyArchived = groupSidebarConversations([butler, archived], {}, "", new Set(), now);
    expect(onlyArchived.groups[0].rows[0]).toMatchObject({ childCount: 0, archivedChildren: 1 });
  });

  it("promotes pinned chats above urgent and recent chats, including Butler children", () => {
    const butler = session("butler", { isControlAssistant: true });
    const pinned = session("pinned", { isButlerInitiated: true, pinned: true, updatedAt: "2026-09-01T10:00:00Z" });
    const waiting = session("waiting", { updatedAt: "2026-09-22T14:00:00Z" });
    const result = groupSidebarConversations([butler, pinned, waiting], { waiting: "waiting" }, "", new Set([butler.id]), now);
    expect(result.groups.map(group => group.label)).toEqual(["Pinned", "Needs you", "Today"]);
    expect(result.groups[0].rows[0].session.id).toBe("pinned");
    expect(result.groups[2].rows.map(row => row.session.id)).toEqual(["butler"]);
  });
});

describe("Butler case chats in the list", () => {
  const butler = () => session("butler", { title: "Butler", isControlAssistant: true, updatedAt: "2026-09-22T09:00:00Z" });
  const caseChat = (id: string, status: ChatSessionSummary["butlerCaseStatus"], updatedAt = "2026-09-22T12:00:00Z") => session(id, {
    title: `Butler check · ${id}`, isControlAssistant: true, isButlerInitiated: true, parentSessionId: "butler",
    butlerRecoveryCaseId: `case-${id}`, butlerCaseStatus: status, updatedAt,
  });

  it("nests case chats under the project's Butler chat, never making one the parent", () => {
    const sessions = [butler(), caseChat("blocked", "needs_attention"), caseChat("stalled", "investigating"),
      session("started", { isButlerInitiated: true }), session("thread", { isControlAssistant: true, isButlerThread: true, updatedAt: "2026-09-22T13:00:00Z" })];
    const result = groupSidebarConversations(sessions, {}, "", new Set(["butler"]), now);
    const rows = result.groups.flatMap(group => group.rows);
    const parent = rows.find(row => row.session.id === "butler");
    expect(parent).toMatchObject({ depth: 0, childKind: "chats", childCount: 3 });
    expect(rows.filter(row => row.depth === 1).map(row => row.session.id).sort()).toEqual(["blocked", "stalled", "started"]);
    // A Butler thread stays its own conversation and never owns case chats.
    expect(rows.find(row => row.session.id === "thread")).toMatchObject({ depth: 0, childCount: 0 });
  });

  it("keeps a case chat under the Butler chat that opened it", () => {
    const older = session("older-butler", { isControlAssistant: true, archivedAt: "2026-09-21T00:00:00Z" });
    const opened = caseChat("check", "investigating");
    const result = groupSidebarConversations([butler(), older, { ...opened, parentSessionId: "older-butler" }], {}, "", new Set(), now);
    const archivedParent = result.groups.flatMap(group => group.rows).find(row => row.session.id === "older-butler");
    expect(archivedParent?.childCount).toBe(1);
  });

  it("lists a case that needs the operator as waiting, and the Butler chat under Needs you", () => {
    const sessions = [butler(), caseChat("blocked", "needs_attention"), caseChat("done", "resolved")];
    const activity = sidebarActivity(sessions, { done: "idle" });
    expect(activity).toMatchObject({ blocked: "waiting", done: "idle" });
    // A running investigation keeps its turn state.
    expect(sidebarActivity(sessions, { blocked: "working" }).blocked).toBe("working");
    const result = groupSidebarConversations(sessions, activity, "", new Set(), now);
    expect(result.groups[0]).toMatchObject({ label: "Needs you" });
    expect(result.groups[0].rows[0]).toMatchObject({ waitingChildren: 1, childCount: 2 });
  });
});
