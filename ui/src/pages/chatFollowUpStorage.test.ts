import { describe, expect, it } from "vitest";
import {
  chatFollowUpStorageKey,
  clearChatFollowUps,
  maxChatFollowUps,
  readChatFollowUps,
  validateChatFollowUpText,
  writeChatFollowUps,
  type ChatFollowUp,
} from "./chatFollowUpStorage";

const item = (id: string, status: ChatFollowUp["status"] = "queued"): ChatFollowUp => ({
  id,
  text: `message ${id}`,
  createdAt: "2026-08-24T12:00:00Z",
  status,
});

describe("assistant follow-up queue recovery", () => {
  it("isolates ordered follow-ups by project and conversation", () => {
    const storage = window.sessionStorage;
    const first = chatFollowUpStorageKey("project/a", "session 1");
    const second = chatFollowUpStorageKey("project/a", "session 2");
    writeChatFollowUps(storage, first, [item("one"), item("two")]);
    writeChatFollowUps(storage, second, [item("other")]);

    expect(readChatFollowUps(storage, first).map((value) => value.id)).toEqual(["one", "two"]);
    expect(readChatFollowUps(storage, second).map((value) => value.id)).toEqual(["other"]);
    clearChatFollowUps(storage, first);
    expect(readChatFollowUps(storage, first)).toEqual([]);
    expect(readChatFollowUps(storage, second)).toHaveLength(1);
  });

  it("requires review after a reload during send", () => {
    const key = chatFollowUpStorageKey("project", "session");
    writeChatFollowUps(window.sessionStorage, key, [item("replayed", "sending")]);

    expect(readChatFollowUps(window.sessionStorage, key)).toEqual([expect.objectContaining({
      id: "replayed",
      status: "failed",
      detail: expect.stringContaining("reloaded"),
    })]);
  });

  it("validates text and bounds the persisted queue", () => {
    expect(validateChatFollowUpText("  ")).toContain("Write a message");
    expect(validateChatFollowUpText("ok")).toBeUndefined();
    expect(validateChatFollowUpText("x".repeat(32_001))).toContain("32,000");

    const key = chatFollowUpStorageKey("project", "session");
    writeChatFollowUps(window.sessionStorage, key, Array.from({ length: maxChatFollowUps() + 3 }, (_, index) => item(String(index))));
    expect(readChatFollowUps(window.sessionStorage, key)).toHaveLength(maxChatFollowUps());
  });
});
