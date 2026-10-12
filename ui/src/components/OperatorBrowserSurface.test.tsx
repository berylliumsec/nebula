import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { framePoint, OperatorBrowserSurface } from "./OperatorBrowserSurface";

class ResizeObserverStub {
  observe() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

describe("OperatorBrowserSurface", () => {
  it("maps the displayed frame to the Chromium viewport and forwards direct input", () => {
    const send = vi.fn();
    render(<OperatorBrowserSurface frame="data:image/jpeg;base64,fixture" connected send={send} />);
    const image = screen.getByRole("img", { name: /Live Chromium page/ }) as HTMLImageElement;
    Object.defineProperty(image, "naturalWidth", { value: 800 });
    Object.defineProperty(image, "naturalHeight", { value: 600 });
    vi.spyOn(image, "getBoundingClientRect").mockReturnValue({ left: 10, top: 20, width: 400, height: 300, right: 410, bottom: 320, x: 10, y: 20, toJSON: () => ({}) });
    image.setPointerCapture = vi.fn();
    expect(framePoint(image, 110, 95)).toEqual({ x: 200, y: 150 });

    fireEvent.pointerDown(image, { pointerId: 1, pointerType: "mouse", button: 0, clientX: 110, clientY: 95 });
    fireEvent.pointerUp(image, { pointerId: 1, pointerType: "mouse", button: 0, clientX: 110, clientY: 95 });
    expect(send).toHaveBeenCalledWith({ kind: "mouse", type: "mousePressed", button: "left", clickCount: 1, x: 200, y: 150 });
    expect(send).toHaveBeenCalledWith({ kind: "mouse", type: "mouseReleased", button: "left", clickCount: 1, x: 200, y: 150 });

    fireEvent.keyDown(image, { key: "a", code: "KeyA" });
    expect(send).toHaveBeenCalledWith({ kind: "text", text: "a" });
    const count = send.mock.calls.length;
    fireEvent.keyDown(image, { key: "Tab", code: "Tab" });
    expect(send).toHaveBeenCalledTimes(count);
  });

  it("keeps a disconnected frame inert and named", () => {
    const send = vi.fn();
    render(<OperatorBrowserSurface frame="" connected={false} send={send} />);
    expect(screen.getByRole("region", { name: "Interactive browser page" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("Connect to show the page.")).toBeVisible();
    expect(send).not.toHaveBeenCalled();
  });
});
