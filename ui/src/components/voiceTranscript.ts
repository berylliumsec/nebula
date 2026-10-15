/** Short-lived GPT-Live transcript parts. Butler's saved chat owns durable history. */
export type VoiceTranscriptPart = {
  speaker: "user" | "assistant";
  delta: string;
  startMs: number;
  endMs: number;
  sequence: number;
};

/** Split speech that arrived since the last Core turn into distinct user turns. */
export function selectDelegatedVoiceTurns(
  parts: VoiceTranscriptPart[],
  delegatedThroughMs: number,
  offsetMs: number,
): VoiceTranscriptPart[][] {
  const candidates = parts.filter((part) => part.speaker === "user"
    && part.endMs > delegatedThroughMs && part.startMs <= offsetMs + 700);
  if (!candidates.length) return [];
  const turns: VoiceTranscriptPart[][] = [[candidates[0]]];
  for (let index = 1; index < candidates.length; index += 1) {
    const previous = candidates[index - 1];
    const next = candidates[index];
    const assistantReplied = parts.some((part) => part.speaker === "assistant"
      && part.sequence > previous.sequence && part.sequence < next.sequence);
    if (assistantReplied || next.startMs - previous.endMs > 1_200) turns.push([]);
    turns[turns.length - 1].push(next);
  }
  return turns;
}
