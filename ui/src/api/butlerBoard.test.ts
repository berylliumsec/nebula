import { describe, expect, it, vi } from "vitest";
import { boardElapsed, getButlerBoardIfChanged, groupButlerBoard, listButlerThreadsIfChanged, type ButlerBoardItem } from "./butlerBoard";
import type { ApiClient } from "./client";

function item(overrides: Partial<ButlerBoardItem>): ButlerBoardItem {
  return {
    sessionId: overrides.title ?? "chat",
    title: "chat",
    project: { id: "p1", name: "Research" },
    state: "working",
    backend: "provider",
    isSubagent: false,
    updatedAt: "2026-11-20T10:00:00Z",
    ...overrides,
  };
}

describe("Butler board grouping", () => {
  const items = [
    item({ title: "Fuzz", state: "working", project: { id: "p2", name: "Bounty" } }),
    item({ title: "Install", state: "waiting" }),
    item({ title: "Symbols", state: "failed", project: { id: "p2", name: "Bounty" } }),
    item({ title: "Child", state: "working", isSubagent: true }),
    item({ title: "Child asks", state: "waiting", isSubagent: true }),
  ];

  it("puts chats needing the operator first and folds working subagents", () => {
    const groups = groupButlerBoard(items, "attention", false);
    expect(groups.map((group) => [group.label, group.items.map((entry) => entry.title)])).toEqual([
      ["Needs you", ["Install", "Child asks"]],
      ["Working", ["Fuzz"]],
      ["Failed", ["Symbols"]],
    ]);
    expect(groupButlerBoard(items, "attention", true)[1].items.map((entry) => entry.title)).toEqual(["Fuzz", "Child"]);
  });

  it("groups by project with projects needing the operator first", () => {
    const groups = groupButlerBoard(items, "project", false);
    expect(groups.map((group) => group.label)).toEqual(["Research", "Bounty"]);
    expect(groups[0].counts).toEqual({ waiting: 2, working: 0, failed: 0, idle: 0 });
    expect(groups[1].items.map((entry) => entry.title)).toEqual(["Fuzz", "Symbols"]);
  });

  it("formats elapsed time compactly and ignores unknown times", () => {
    const now = Date.parse("2026-11-20T12:00:00Z");
    expect(boardElapsed("2026-11-20T11:59:15Z", now)).toBe("45s");
    expect(boardElapsed("2026-11-20T11:48:00Z", now)).toBe("12m");
    expect(boardElapsed("2026-11-20T09:00:00Z", now)).toBe("3h");
    expect(boardElapsed("2026-11-18T12:00:00Z", now)).toBe("2d");
    expect(boardElapsed(undefined, now)).toBeUndefined();
    expect(boardElapsed("not a date", now)).toBeUndefined();
  });
});

describe("Butler board reads", () => {
  it("maps Core's board and passes the poll's ETag", async () => {
    const requestIfChanged = vi.fn().mockResolvedValue({
      etag: '"b2"',
      value: {
        counts: { waiting: 1, working: 0, failed: 0, idle: 9 },
        truncated: false,
        next_offset: null,
        items: [{
          session_id: "s1", title: "Kernel fuzz", project: { id: "p1", name: "Research" }, state: "waiting",
          backend: "provider", provider_profile_id: "prov", model: "m", is_subagent: false, turn_id: "t1",
          turn_status: "waiting_approval", turn_started_at: "2026-11-20T10:00:00Z", updated_at: "2026-11-20T10:01:00Z",
          now: "Approve run_command: make install",
          attention: { kind: "approval", action_id: "a1", summary: "Approve run_command: make install", requested_at: "2026-11-20T10:01:00Z" },
          goal: { objective: "Find a crash", status: "running", current_step: 2, plan_length: 6 },
        }],
      },
    });
    const answer = await getButlerBoardIfChanged({ requestIfChanged } as unknown as ApiClient, { q: " fuzz " }, '"b1"');
    expect(requestIfChanged).toHaveBeenCalledWith("butler/board?q=fuzz", '"b1"', { signal: undefined });
    expect(answer?.etag).toBe('"b2"');
    expect(answer?.board.counts.idle).toBe(9);
    expect(answer?.board.items[0]).toMatchObject({
      sessionId: "s1", state: "waiting", turnId: "t1",
      attention: { kind: "approval", actionId: "a1" },
      goal: { currentStep: 2, planLength: 6 },
    });
  });

  it("pages idle chats and treats 304 as unchanged", async () => {
    const requestIfChanged = vi.fn().mockResolvedValue(undefined);
    const api = { requestIfChanged } as unknown as ApiClient;
    expect(await getButlerBoardIfChanged(api, { state: "idle", offset: 50, limit: 50 }, '"b"')).toBeUndefined();
    expect(requestIfChanged).toHaveBeenCalledWith("butler/board?state=idle&offset=50&limit=50", '"b"', { signal: undefined });
    expect(await listButlerThreadsIfChanged(api, '"t"')).toBeUndefined();
    expect(requestIfChanged).toHaveBeenLastCalledWith("butler/threads", '"t"', { signal: undefined });
  });
});
