import { describe, expect, it } from "vitest";
import type { ChatSessionSummary } from "../api/types";
import { nextSideChatNumber, sideChatForConversation, sideChatsForConversation, sideChatTabLabel } from "./sideChatSelection";

function side(id: string, parentId: string, isSideChat = true): ChatSessionSummary {
  return {id, parentSessionId: parentId, isSideChat} as ChatSessionSummary;
}

describe("side chat selection", () => {
  const sideChats = [side("a-new", "a"), side("b", "b"), side("a-old", "a"), side("ordinary", "a", false)];

  it("never displays a side chat beside another conversation", () => {
    expect(sideChatForConversation(sideChats, "b", "a-new")?.id).toBe("b");
    expect(sideChatForConversation(sideChats, "c", "a-new")).toBeUndefined();
  });

  it("restores the most recent side chat when returning to its parent", () => {
    expect(sideChatForConversation(sideChats, "a")?.id).toBe("a-new");
  });

  it("honors a deep link to an older side chat only within its parent", () => {
    expect(sideChatForConversation(sideChats, "a", "a-old")?.id).toBe("a-old");
    expect(sideChatForConversation(sideChats, "a", "ordinary")?.id).toBe("a-new");
  });

  it("keeps tabs in creation order when replies reorder the Core list", () => {
    const first = {...side("first", "a"), createdAt: "2026-10-14T10:00:00Z"};
    const second = {...side("second", "a"), createdAt: "2026-10-14T10:01:00Z"};
    expect(sideChatsForConversation([second, side("other", "b"), first], "a").map(item => item.id)).toEqual(["first", "second"]);
  });

  it("keeps a saved tab number when an older sibling closes", () => {
    const older = {...side("first", "a"), title: "Side chat #1 · Research", createdAt: "2026-10-14T10:00:00Z"};
    const newer = {...side("second", "a"), title: "Side chat #2 · Research", createdAt: "2026-10-14T10:01:00Z"};
    expect(nextSideChatNumber([newer, older], "a")).toBe(3);
    expect(sideChatTabLabel(newer, 0)).toBe("Side 2");
    expect(sideChatTabLabel(newer, 0)).toBe(sideChatTabLabel(newer, 1));
  });
});
