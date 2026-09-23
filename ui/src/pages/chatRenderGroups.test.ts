import { describe, expect, it } from "vitest";
import { groupByAssistantId } from "./chatRenderGroups";

describe("groupByAssistantId", () => {
  it("groups render records once without changing their order", () => {
    const first = { assistantId: "one", id: "a" };
    const second = { assistantId: "two", id: "b" };
    const third = { assistantId: "one", id: "c" };

    const grouped = groupByAssistantId([first, second, third]);

    expect(grouped.get("one")).toEqual([first, third]);
    expect(grouped.get("two")).toEqual([second]);
    expect(grouped.get("missing")).toBeUndefined();
  });
});
