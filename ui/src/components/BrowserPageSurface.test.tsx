import { createEvent, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BrowserPageSurface } from "./BrowserPageSurface";

function fixture(mode: "browse" | "element" | "region" = "browse") {
  const send = vi.fn(); const onCapture = vi.fn();
  render(<BrowserPageSurface frame="data:image/jpeg;base64,AA==" mode={mode} connected send={send} onCapture={onCapture} />);
  const page = screen.getByRole("img");
  Object.defineProperties(page, { naturalWidth: { value: 1280 }, naturalHeight: { value: 800 }, setPointerCapture: { value: vi.fn() } });
  vi.spyOn(page, "getBoundingClientRect").mockReturnValue({ x: 10, y: 20, left: 10, top: 20, right: 650, bottom: 420, width: 640, height: 400, toJSON: () => ({}) });
  const pointer = (kind: "pointerDown" | "pointerMove" | "pointerUp" | "pointerCancel", x: number, y: number, touch = false) => {
    const event = createEvent[kind](page);
    Object.defineProperties(event, { clientX: { value: x }, clientY: { value: y }, pointerId: { value: 1 }, pointerType: { value: touch ? "touch" : "mouse" }, button: { value: 0 } });
    fireEvent(page, event);
  };
  return { send, onCapture, page, pointer };
}

describe("BrowserPageSurface", () => {
  it("scales touch coordinates and releases a cancelled gesture", () => {
    const { send, pointer } = fixture();
    pointer("pointerDown", 20, 40, true);
    pointer("pointerMove", 30, 30, true);
    pointer("pointerCancel", 30, 30, true);
    expect(send.mock.calls.map(([event]) => event)).toEqual([
      { kind: "touch", type: "touchStart", touchPoints: [{ x: 20, y: 40, id: 0 }] },
      { kind: "touch", type: "touchMove", touchPoints: [{ x: 40, y: 20, id: 0 }] },
      { kind: "touch", type: "touchCancel", touchPoints: [] },
    ]);
  });
  it("previews a rectangle and captures its host coordinates without mutating the page", () => {
    const { send, onCapture, pointer } = fixture("region");
    pointer("pointerDown", 20, 40);
    pointer("pointerMove", 120, 140);
    expect(document.querySelector(".managed-browser-region")).toBeInTheDocument();
    pointer("pointerUp", 120, 140);
    expect(onCapture).toHaveBeenCalledWith("region", { x: 20, y: 40, width: 200, height: 200 });
    expect(send).not.toHaveBeenCalled();
    expect(document.querySelector(".managed-browser-region")).not.toBeInTheDocument();
  });
  it("keeps keyboard focus escapable and sends both halves of modified keys", () => {
    const { send, page } = fixture();
    fireEvent.keyDown(page, { key: "Tab", code: "Tab" });
    expect(send).not.toHaveBeenCalled();
    fireEvent.keyDown(page, { key: "a", code: "KeyA", ctrlKey: true });
    expect(send.mock.calls.map(([event]) => event)).toEqual([
      { kind: "key", type: "keyDown", key: "a", code: "KeyA", modifiers: 2 },
      { kind: "key", type: "keyUp", key: "a", code: "KeyA", modifiers: 2 },
    ]);
  });
  it("accepts composed text through a real editable field and clears it after sending", () => {
    const { send } = fixture();
    fireEvent.click(screen.getByText("Page keyboard"));
    fireEvent.change(screen.getByLabelText("Text to type on page"), { target: { value: "Hello 世界" } });
    expect(send).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Type on page" }));
    expect(send).toHaveBeenCalledWith({ kind: "text", text: "Hello 世界" });
    expect(screen.getByLabelText("Text to type on page")).toHaveValue("");
  });
});
