import { describe, expect, it } from "vitest";
import { providerTurnFollowAction, transcriptShowsTurn, type ProviderTurnFollowInput } from "./providerTurnFollow";

const snapshot = (turnId: string | null, execution: string, busy: boolean, pending: ProviderTurnFollowInput["snapshot"]["pending"] = []) => ({
  turn_id: turnId, execution, busy, pending,
});
const decide = (overrides: Partial<ProviderTurnFollowInput>) => providerTurnFollowAction({
  snapshot: snapshot("turn-2", "running", true),
  owned: false,
  finished: new Set(),
  shown: () => false,
  ...overrides,
});

describe("provider turn follow authority", () => {
  it("attaches to active work this page did not start", () => {
    // A goal's next turn, a schedule, a decision made elsewhere, restart recovery.
    for (const execution of ["running", "queued", "finalizing", "waiting_callback", "recovering", "needs_stop"]) {
      expect(decide({snapshot: snapshot("turn-2", execution, true)})).toBe("attach");
    }
  });

  it("leaves a turn to whatever on the page already presents it", () => {
    expect(decide({owned: true})).toBeUndefined();
    // The page saw it end; a snapshot that has not caught up yet is stale.
    expect(decide({finished: new Set(["turn-2"])})).toBeUndefined();
    // A pending approval restores its card through the approval authority.
    expect(decide({snapshot: snapshot("turn-2", "waiting_approval", true, [{id: "approval", turn_id: "turn-2", kind: "approval", text: "Review"}])})).toBeUndefined();
    // A recorded decision has nothing to follow until the turn continues.
    expect(decide({snapshot: snapshot("turn-2", "continuing", true)})).toBeUndefined();
    expect(decide({snapshot: snapshot(null, "idle", false)})).toBeUndefined();
  });

  it("reloads only a settled turn the transcript does not show", () => {
    for (const execution of ["complete", "failed", "cancelled"]) {
      expect(decide({snapshot: snapshot("turn-2", execution, false)})).toBe("reload");
      expect(decide({snapshot: snapshot("turn-2", execution, false), shown: turnId => turnId === "turn-2"})).toBeUndefined();
    }
    // An interrupted turn that no longer blocks has no answer or note to load.
    expect(decide({snapshot: snapshot("turn-2", "interrupted", false)})).toBeUndefined();
  });

  it("finds a turn by its saved answer or by Core's outcome note", () => {
    expect(transcriptShowsTurn([{metadata: {chat_turn_id: "turn-2"}}], "turn-2")).toBe(true);
    expect(transcriptShowsTurn([{outcomeTurnId: "turn-2"}], "turn-2")).toBe(true);
    expect(transcriptShowsTurn([{metadata: {chat_turn_id: "turn-1"}}, {}], "turn-2")).toBe(false);
  });
});
