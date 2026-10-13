import { describe, expect, it } from "vitest";
import { groupSubagentTranscriptMessages } from "./subagentTranscriptGroups";

describe("subagent transcript groups", () => {
  it("groups consecutive saved results while retaining every message's navigation position", () => {
    const message = (id: string, kind?: string) => ({id, role: "assistant", metadata: kind ? {kind} : {}});
    const {items, indexByMessageId} = groupSubagentTranscriptMessages([
      message("answer"), message("first", "subagent_result"), message("second", "subagent_result"),
      message("follow-up"), message("third"),
    ], new Set(["third"]));
    expect(items.map(item => item.kind)).toEqual(["message", "subagent_results", "message", "subagent_results"]);
    expect(items[1]).toMatchObject({key: "subagent:first", messages: [{id: "first"}, {id: "second"}]});
    expect([...indexByMessageId.entries()]).toEqual([
      ["answer", 0], ["first", 1], ["second", 1], ["follow-up", 2], ["third", 3],
    ]);
  });

  it("leaves action-bearing system messages outside result groups", () => {
    const {items} = groupSubagentTranscriptMessages([
      {id: "question", role: "system", metadata: {kind: "subagent_result"}},
      {id: "result", role: "assistant", metadata: {kind: "subagent_result"}},
    ], new Set<string>());
    expect(items.map(item => item.kind)).toEqual(["message", "subagent_results"]);
  });
});
