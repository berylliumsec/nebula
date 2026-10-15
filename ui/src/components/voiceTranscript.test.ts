import { describe, expect, it } from "vitest";
import { selectDelegatedVoiceTurns, type VoiceTranscriptPart } from "./voiceTranscript";

const part = (sequence: number, speaker: VoiceTranscriptPart["speaker"], delta: string, startMs: number, endMs: number): VoiceTranscriptPart =>
  ({ sequence, speaker, delta, startMs, endMs });

describe("GPT-Live transcript handoff", () => {
  it("splits earlier direct speech into its own durable Butler turn before a follow-up", () => {
    const parts = [
      part(1, "user", "My favorite color is blue.", 100, 700),
      part(2, "assistant", "Okay.", 750, 900),
      part(3, "user", "What did I say ", 2_000, 2_300),
      part(4, "user", "my favorite color is?", 2_300, 2_650),
    ];
    const turns = selectDelegatedVoiceTurns(parts, 0, 2_800);
    expect(turns.map((turn) => turn.map((item) => item.delta).join(""))).toEqual([
      "My favorite color is blue.", "What did I say my favorite color is?",
    ]);

    const followUp = [...parts, part(5, "assistant", "Blue.", 2_900, 3_150), part(6, "user", "Why?", 3_500, 3_800)];
    const next = selectDelegatedVoiceTurns(followUp, 2_650, 4_000);
    expect(next.map((turn) => turn.map((item) => item.delta).join(""))).toEqual(["Why?"]);
  });

  it("separates two user utterances at a pause without discarding either turn", () => {
    const parts = [part(1, "user", "Remember the second option.", 100, 500), part(2, "user", "Which option?", 2_000, 2_400)];
    expect(selectDelegatedVoiceTurns(parts, 0, 2_600).map((turn) => turn.map((item) => item.delta).join("")))
      .toEqual(["Remember the second option.", "Which option?"]);
  });
});
