import { afterEach, describe, expect, it, vi } from "vitest";
import { copySelectionText } from "./selectionActions";

afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren(); });

describe("clipboard across browser origins", () => {
  it("copies exact text on LAN HTTP and restores focused input selection", async () => {
    vi.stubGlobal("navigator", {});
    const input = document.createElement("textarea");
    input.value = "unsent draft";
    document.body.append(input);
    input.focus();
    input.setSelectionRange(2, 7, "backward");
    const copy = vi.fn(() => {
      const setData = vi.fn();
      const event = new Event("copy", { cancelable: true });
      Object.defineProperty(event, "clipboardData", { value: { setData } });
      document.dispatchEvent(event);
      expect(setData).toHaveBeenCalledWith("text/plain", "\tλ\r\n");
      return true;
    });
    Object.defineProperty(document, "execCommand", { configurable: true, value: copy });
    await copySelectionText("\tλ\r\n");
    expect(copy).toHaveBeenCalledWith("copy");
    expect(document.activeElement).toBe(input);
    expect([input.selectionStart, input.selectionEnd, input.selectionDirection]).toEqual([2, 7, "backward"]);
    expect(document.querySelectorAll("textarea")).toHaveLength(1);
  });

  it("falls back after permission denial and restores document selection", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    document.body.textContent = "selected code";
    const range = document.createRange();
    range.selectNodeContents(document.body);
    document.getSelection()?.removeAllRanges();
    document.getSelection()?.addRange(range);
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => true) });
    await copySelectionText("code");
    expect(document.getSelection()?.toString()).toBe("selected code");
  });

  it.each([false, "throw"])("cleans up and reports failure when legacy copy returns %s", async (failure) => {
    vi.stubGlobal("navigator", {});
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => {
      if (failure === "throw") throw new Error("denied");
      return false;
    }) });
    await expect(copySelectionText("draft")).rejects.toThrow("Select the text and copy it manually");
    expect(document.querySelector("textarea")).toBeNull();
  });
});
