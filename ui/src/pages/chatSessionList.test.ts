import { describe, expect, it } from "vitest";
import type { ChatSessionSummary } from "../api/types";
import { reconcileListedSessions } from "./chatSessionList";

function session(id: string, revision: number, overrides: Partial<ChatSessionSummary> = {}): ChatSessionSummary {
  return {
    id,
    engagementId: "engagement-1",
    title: id,
    backend: "provider",
    providerId: "provider-1",
    model: "model-1",
    toolsEnabled: false,
    mcpServerIds: [],
    hookIds: [],
    createdAt: "2026-09-21T10:00:00Z",
    updatedAt: "2026-09-21T10:00:00Z",
    revision,
    ...overrides,
  };
}

describe("reconcileListedSessions", () => {
  it("keeps a conversation saved after the list was read", () => {
    // Subagents was checked (revision 4) while an older list read (revision
    // 2) was still on its way.
    const saved = session("chat", 4, { allowSubagents: true });
    const stale = session("chat", 2, { allowSubagents: false });

    expect(reconcileListedSessions([saved], [stale])).toEqual([saved]);
  });

  it("takes every conversation the list has newer or equal", () => {
    const current = [session("a", 2), session("b", 3, { title: "Renamed here" })];
    const listed = [session("a", 5, { allowSubagents: true }), session("b", 3, { title: "Listed" })];

    expect(reconcileListedSessions(current, listed)).toEqual(listed);
  });

  it("follows the list for which conversations exist, newest activity first", () => {
    const current = [session("deleted", 9), session("kept", 1)];
    const listed = [
      session("kept", 1, { updatedAt: "2026-09-21T10:00:00Z" }),
      session("created", 1, { updatedAt: "2026-09-21T12:00:00Z" }),
    ];

    expect(reconcileListedSessions(current, listed).map((item) => item.id)).toEqual(["created", "kept"]);
  });
});
