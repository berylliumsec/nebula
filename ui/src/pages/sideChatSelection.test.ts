import { describe, expect, it } from "vitest";
import type { ChatSessionSummary } from "../api/types";
import { sideChatForConversation } from "./sideChatSelection";

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
});
