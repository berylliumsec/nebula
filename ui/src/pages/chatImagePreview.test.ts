import { afterEach, describe, expect, it, vi } from "vitest";
import { chatImageKey, ChatImagePreviewCache, renderChatImagePreview, type ChatImagePalette, type ChatImagePreview } from "./chatImagePreview";

const palette: ChatImagePalette = {
  background: "#101010", text: "#eeeeee", muted: "#aaaaaa", userBackground: "#282828", fontFamily: "sans-serif", theme: "dark",
};

const createObjectURLDescriptor = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const revokeObjectURLDescriptor = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
afterEach(() => {
  vi.restoreAllMocks();
  if (createObjectURLDescriptor) Object.defineProperty(URL, "createObjectURL", createObjectURLDescriptor);
  else delete (URL as {createObjectURL?: unknown}).createObjectURL;
  if (revokeObjectURLDescriptor) Object.defineProperty(URL, "revokeObjectURL", revokeObjectURLDescriptor);
  else delete (URL as {revokeObjectURL?: unknown}).revokeObjectURL;
});

describe("chat image previews", () => {
  it("invalidates an image when text, width or theme changes", () => {
    const messages = [{id: "one", role: "assistant" as const, content: "Saved answer"}];
    const key = chatImageKey(messages, 390, palette);
    expect(chatImageKey([{...messages[0], content: "Edited answer"}], 390, palette)).not.toBe(key);
    expect(chatImageKey(messages, 430, palette)).not.toBe(key);
    expect(chatImageKey(messages, 390, {...palette, theme: "light"})).not.toBe(key);
  });

  it("rasterizes the full loaded history into bounded tiles", async () => {
    const drawn: string[] = [];
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => ({
      measureText: (value: string) => ({width: value.length * 7}),
      scale: () => {}, fillRect: () => {}, fillText: (value: string) => drawn.push(value),
    }) as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => callback(new Blob(["tile"], {type: "image/webp"})));
    let nextUrl = 0;
    const create = vi.fn(() => `blob:chat-${nextUrl++}`);
    const revoke = vi.fn();
    Object.defineProperty(URL, "createObjectURL", {configurable: true, value: create});
    Object.defineProperty(URL, "revokeObjectURL", {configurable: true, value: revoke});
    const messages = Array.from({length: 30}, (_, index) => ({id: String(index), role: "assistant" as const, content: `Message ${index} ${"durable history ".repeat(8)}`}));
    const image = await renderChatImagePreview(messages, 390, palette);
    if (!image) throw new Error("Expected a rendered image preview");
    expect(image.tiles.length).toBeGreaterThan(1);
    expect(image.tiles.every((tile) => tile.height <= 1024)).toBe(true);
    expect(drawn).toContain("Assistant");
    expect(drawn.some((line) => line.includes("Message 29"))).toBe(true);
    image.dispose();
    expect(revoke).toHaveBeenCalledTimes(image.tiles.length);
  });

  it("releases evicted images and keeps the most recent chats within its byte limit", () => {
    const disposed: string[] = [];
    const image = (id: string): ChatImagePreview => ({
      key: id, width: 390, height: 100, bytes: 6, tiles: [], messageTops: [], dispose: () => disposed.push(id),
    });
    const cache = new ChatImagePreviewCache(3, 12);
    cache.set("a", image("a")); cache.set("b", image("b"));
    expect(cache.get("a")?.key).toBe("a");
    cache.set("c", image("c"));
    expect(cache.get("b")).toBeUndefined();
    expect(disposed).toEqual(["b"]);
    cache.clear();
    expect(disposed.sort()).toEqual(["a", "b", "c"]);
  });
});
