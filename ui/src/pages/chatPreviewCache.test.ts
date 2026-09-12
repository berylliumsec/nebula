import { describe, expect, it } from "vitest";
import { ChatPreviewCache } from "./chatPreviewCache";

describe("chat previews", () => {
  it("retains the most recently visited chats within its capacity", () => {
    const cache = new ChatPreviewCache<string>(2);
    cache.set("a", "A"); cache.set("b", "B");
    expect(cache.get("a")).toBe("A");
    cache.set("c", "C");
    expect(cache.get("b")).toBeUndefined();
    expect(cache.get("a")).toBe("A");
    expect(cache.get("c")).toBe("C");
  });

  it("replaces refreshed previews and forgets deleted chats", () => {
    const cache = new ChatPreviewCache<string>();
    cache.set("a", "old"); cache.set("a", "fresh");
    expect(cache.get("a")).toBe("fresh");
    cache.delete("a");
    expect(cache.get("a")).toBeUndefined();
  });

  it("isolates previews between project or connection instances", () => {
    const first = new ChatPreviewCache<string>();
    const second = new ChatPreviewCache<string>();
    first.set("a", "private transcript");
    expect(second.get("a")).toBeUndefined();
  });
});
