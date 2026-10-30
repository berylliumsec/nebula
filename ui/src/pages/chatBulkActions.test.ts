import { describe, expect, it, vi } from "vitest";
import type { ChatSessionSummary } from "../api/types";
import { applyChatBulkAction, conversationMatchesSearch } from "./chatBulkActions";

const session = (id: string, changes: Partial<ChatSessionSummary> = {}): ChatSessionSummary => ({
  id, engagementId: "project", title: `Chat ${id}`, backend: "provider", model: "local-model",
  toolsEnabled: false, mcpServerIds: [], hookIds: [], createdAt: "2026-10-30T00:00:00Z",
  updatedAt: "2026-10-30T00:00:00Z", revision: 3, ...changes,
});

function fakeCore(initial: ChatSessionSummary[]) {
  const saved = new Map(initial.map(item => [item.id, item]));
  const api = {
    listChatSessions: vi.fn(async () => ({ items: [...saved.values()] })),
    setChatSessionArchived: vi.fn(async (id: string, archived: boolean, revision: number) => {
      const current = saved.get(id)!;
      expect(revision).toBe(current.revision);
      const updated = session(id, { ...current, archivedAt: archived ? "2026-10-30T01:00:00Z" : undefined, revision: revision + 1 });
      saved.set(id, updated);
      return updated;
    }),
    setChatSessionPriority: vi.fn(async (id: string, changes: { pinned?: boolean; flagColor?: ChatSessionSummary["flagColor"] | null }, revision: number) => {
      const current = saved.get(id)!;
      expect(revision).toBe(current.revision);
      const updated = session(id, { ...current, ...changes, flagColor: changes.flagColor === null ? undefined : changes.flagColor ?? current.flagColor, revision: revision + 1 });
      saved.set(id, updated);
      return updated;
    }),
    deleteChatSession: vi.fn(async (id: string) => { saved.delete(id); }),
  };
  return { api: api as unknown as Parameters<typeof applyChatBulkAction>[0], saved, calls: api };
}

describe("bulk chat actions", () => {
  it("uses the same direct matches as sidebar search", () => {
    expect(conversationMatchesSearch(session("one", { title: "Review evidence" }), "EVIDENCE")).toBe(true);
    expect(conversationMatchesSearch(session("one"), "local-model")).toBe(true);
    expect(conversationMatchesSearch(session("one"), "unrelated")).toBe(false);
  });

  it("re-reads current revisions and accepts a saved archive when its response was lost", async () => {
    const { api, saved, calls } = fakeCore([session("one"), session("two")]);
    calls.setChatSessionArchived.mockImplementationOnce(async (id, archived, revision) => {
      saved.set(id, session(id, { archivedAt: archived ? "2026-10-30T01:00:00Z" : undefined, revision: revision + 1 }));
      throw new Error("response lost");
    }).mockImplementationOnce(async () => { throw new Error("active turn"); });
    const result = await applyChatBulkAction(api, "project", ["one", "two"], "archive");
    expect(result.succeededIds).toEqual(["one"]);
    expect(result.failures).toMatchObject([{ id: "two", error: new Error("active turn") }]);
    expect(calls.listChatSessions).toHaveBeenCalledTimes(2);
    expect(calls.setChatSessionArchived).toHaveBeenCalledWith("one", true, 3);
  });

  it("deletes a chat already absent and leaves Core-rejected chats for retry", async () => {
    const { api, calls } = fakeCore([session("active")]);
    calls.deleteChatSession.mockRejectedValueOnce(new Error("active harness turn"));
    const result = await applyChatBulkAction(api, "project", ["already-gone", "active"], "delete");
    expect(result.succeededIds).toEqual(["already-gone"]);
    expect(result.failures).toMatchObject([{ id: "active" }]);
    expect(calls.deleteChatSession).toHaveBeenCalledTimes(1);
  });

  it("applies pin, flag, and restore through the saved revision", async () => {
    const { api, calls } = fakeCore([session("one", { archivedAt: "2026-10-29T00:00:00Z" })]);
    expect((await applyChatBulkAction(api, "project", ["one"], "restore")).failures).toEqual([]);
    expect((await applyChatBulkAction(api, "project", ["one"], "pin")).failures).toEqual([]);
    expect((await applyChatBulkAction(api, "project", ["one"], "flag:blue")).failures).toEqual([]);
    expect(calls.setChatSessionArchived).toHaveBeenCalledWith("one", false, 3);
    expect(calls.setChatSessionPriority).toHaveBeenCalledWith("one", { pinned: true }, 4);
    expect(calls.setChatSessionPriority).toHaveBeenCalledWith("one", { flagColor: "blue" }, 5);
  });
});
