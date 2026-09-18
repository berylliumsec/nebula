import { describe, expect, it } from "vitest";
import { UI_ZOOM_DEFAULT, UI_ZOOM_STEPS, nextZoom, normalizeZoom, zoomShortcut } from "./uiZoom";

const key = (value: string, modifiers: Partial<Record<"metaKey" | "ctrlKey" | "altKey", boolean>> = {}) => ({
  key: value,
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  ...modifiers,
});

describe("ui zoom", () => {
  it("maps ⌘ shortcuts on macOS and Ctrl elsewhere", () => {
    expect(zoomShortcut(key("=", { metaKey: true }), true)).toBe("in");
    expect(zoomShortcut(key("+", { metaKey: true }), true)).toBe("in");
    expect(zoomShortcut(key("-", { metaKey: true }), true)).toBe("out");
    expect(zoomShortcut(key("0", { metaKey: true }), true)).toBe("reset");
    expect(zoomShortcut(key("-", { ctrlKey: true }), true)).toBeNull();
    expect(zoomShortcut(key("-", { ctrlKey: true }), false)).toBe("out");
    expect(zoomShortcut(key("=", { metaKey: true }), false)).toBeNull();
    expect(zoomShortcut(key("=", { metaKey: true, altKey: true }), true)).toBeNull();
    expect(zoomShortcut(key("k", { metaKey: true }), true)).toBeNull();
    expect(zoomShortcut(key("="), true)).toBeNull();
  });

  it("steps within bounds and resets", () => {
    expect(nextZoom(1, "in")).toBe(1.1);
    expect(nextZoom(1, "out")).toBe(0.9);
    expect(nextZoom(1.5, "reset")).toBe(UI_ZOOM_DEFAULT);
    expect(nextZoom(UI_ZOOM_STEPS[UI_ZOOM_STEPS.length - 1], "in")).toBe(UI_ZOOM_STEPS[UI_ZOOM_STEPS.length - 1]);
    expect(nextZoom(UI_ZOOM_STEPS[0], "out")).toBe(UI_ZOOM_STEPS[0]);
  });

  it("rejects stored values that are not a supported step", () => {
    expect(normalizeZoom("1.25")).toBe(1.25);
    expect(normalizeZoom("3")).toBe(UI_ZOOM_DEFAULT);
    expect(normalizeZoom(null)).toBe(UI_ZOOM_DEFAULT);
    expect(normalizeZoom("abc")).toBe(UI_ZOOM_DEFAULT);
  });
});
