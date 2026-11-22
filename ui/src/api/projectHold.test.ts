import { afterEach, describe, expect, it, vi } from "vitest";
import { HELD_POLL_MS, OPEN_POLL_MS, ProjectHoldWatch, chatHoldFor, holdState, type ProjectHoldView } from "./projectHold";

function view(status: "holding" | "held" | "released" | null, extra: Partial<ProjectHoldView> = {}): ProjectHoldView {
  return {
    project_id: "project-1",
    held: status === "holding" || status === "held",
    hold: status ? {
      id: "hold-1", status, reason: "Core supervisor restart", grace_seconds: 180,
      grace_until: "2026-11-20T12:03:00Z", created_at: "2026-11-20T12:00:00Z",
      paused_goals: [{ goal_id: "g1", session_id: "s1" }, { goal_id: "g2", session_id: "s2" }],
    } : null,
    active_turns: 1,
    running_goals: 0,
    ...extra,
  };
}

afterEach(() => vi.useRealTimers());

describe("project hold state", () => {
  it("reads a released or missing hold as an open project", () => {
    expect(holdState(view(null)).held).toBe(false);
    expect(holdState(view("released")).held).toBe(false);
    expect(holdState(undefined).held).toBe(false);
  });

  it("carries phase, grace and paused goal count while held", () => {
    expect(holdState(view("holding"))).toMatchObject({
      held: true, phase: "holding", holdId: "hold-1", pausedGoals: 2, activeTurns: 1,
      graceUntil: "2026-11-20T12:03:00Z",
    });
  });
});

describe("ProjectHoldWatch", () => {
  it("shares one read per project and polls faster while held", async () => {
    vi.useFakeTimers();
    const request = vi.fn().mockResolvedValue(view("held"));
    const watch = new ProjectHoldWatch(request as never);
    const first = vi.fn();
    const second = vi.fn();
    const stopFirst = watch.subscribe("project-1", first);
    const stopSecond = watch.subscribe("project-1", second);
    await vi.waitFor(() => expect(first).toHaveBeenCalledWith(expect.objectContaining({ held: true })));
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("engagements/project-1/hold", expect.anything());
    expect(second).toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(HELD_POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
    stopFirst();
    stopSecond();
    await vi.advanceTimersByTimeAsync(OPEN_POLL_MS);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("stops asking a Core that has no hold route", async () => {
    vi.useFakeTimers();
    const request = vi.fn().mockRejectedValue(Object.assign(new Error("Not Found"), { status: 404 }));
    const watch = new ProjectHoldWatch(request as never);
    const listener = vi.fn();
    watch.subscribe("project-1", listener);
    await vi.waitFor(() => expect(listener).toHaveBeenCalledWith(expect.objectContaining({ held: false })));
    await vi.advanceTimersByTimeAsync(OPEN_POLL_MS * 3);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("releases through Core and publishes the open project", async () => {
    const request = vi.fn()
      .mockResolvedValueOnce(view("held"))
      .mockResolvedValueOnce(view("released", { held: false }));
    const watch = new ProjectHoldWatch(request as never);
    const listener = vi.fn();
    watch.subscribe("project/1", listener);
    await vi.waitFor(() => expect(listener).toHaveBeenCalledWith(expect.objectContaining({ held: true })));
    const state = await watch.release("project/1", "hold-1");
    expect(state.held).toBe(false);
    expect(request).toHaveBeenLastCalledWith(
      "engagements/project%2F1/hold/hold-1/release",
      expect.objectContaining({ method: "POST" }),
    );
    expect(listener).toHaveBeenLastCalledWith(expect.objectContaining({ held: false }));
  });
});


describe("chat holds", () => {
  const chatHold = (session: string, batch = "batch-1") => ({
    id: `hold-${session}`, status: "held" as const, reason: "Priorities changed", grace_seconds: 180,
    created_at: "2026-11-20T12:00:00Z", paused_goals: [], session_id: session, batch_id: batch,
  });

  it("lists chat holds while the project itself stays open", () => {
    const state = holdState(view(null, { chat_holds: [chatHold("s1"), { ...chatHold("s2"), status: "released" }] }));
    expect(state.held).toBe(false);
    expect(state.chatHolds).toEqual([{ id: "hold-s1", sessionId: "s1", batchId: "batch-1", reason: "Priorities changed", createdAt: "2026-11-20T12:00:00Z" }]);
  });

  it("finds a chat's own hold or its parent's, without looping", () => {
    const holds = holdState(view(null, { chat_holds: [chatHold("main")] })).chatHolds;
    const parents: Record<string, string> = { subagent: "main", grandchild: "subagent", loop: "loop" };
    expect(chatHoldFor(holds, "grandchild", (id) => parents[id])?.sessionId).toBe("main");
    expect(chatHoldFor(holds, "other", (id) => parents[id])).toBeUndefined();
    expect(chatHoldFor(holds, "loop", (id) => parents[id])).toBeUndefined();
  });

  it("places and releases chat holds through Core and publishes the new state", async () => {
    const request = vi.fn(async (path: string) => {
      if (path.endsWith("/holds")) return { ...view(null, { chat_holds: [chatHold("s1")] }), batch_id: "batch-1", holds: [chatHold("s1")] };
      if (path.endsWith("/holds/release")) return view(null, { chat_holds: [] });
      return view(null);
    });
    const watch = new ProjectHoldWatch(request as never);
    const seen: number[] = [];
    const stop = watch.subscribe("project-1", (state) => seen.push(state.chatHolds.length));
    const placed = await watch.holdChats("project-1", { sessionIds: ["s1"] });
    expect(placed).toEqual({ batchId: "batch-1", sessionIds: ["s1"] });
    expect(request).toHaveBeenCalledWith("engagements/project-1/holds", { method: "POST", body: JSON.stringify({ session_ids: ["s1"], all_active: false }) });
    await watch.releaseChats("project-1", { batchId: "batch-1" });
    expect(JSON.parse((request.mock.calls.find(([path]) => path.endsWith("/release")) as unknown as [string, { body: string }])[1].body)).toMatchObject({ batch_id: "batch-1" });
    expect(seen.at(-1)).toBe(0);
    expect(seen).toContain(1);
    stop();
  });
});
