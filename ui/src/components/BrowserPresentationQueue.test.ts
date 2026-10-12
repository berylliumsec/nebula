import { describe, expect, it, vi } from "vitest";
import { BrowserPresentationQueue, type BrowserPresentation } from "./BrowserPresentationQueue";

const visible = (y: number): BrowserPresentation => ({
  projectId: "project-1",
  tabId: "tab-1",
  bounds: { x: 10, y, width: 600, height: 400 },
});
const hidden: BrowserPresentation = { projectId: "project-1", tabId: null, bounds: null };

describe("BrowserPresentationQueue", () => {
  it("keeps the newest measured slot while a native placement is in flight", async () => {
    let finishFirst!: () => void;
    const apply = vi.fn().mockImplementationOnce(() => new Promise<void>((resolve) => { finishFirst = resolve; }))
      .mockResolvedValue(undefined);
    const queue = new BrowserPresentationQueue(apply);
    queue.request(visible(100));
    queue.request(visible(120));
    queue.request(visible(140));
    finishFirst();
    await queue.whenIdle();
    expect(apply.mock.calls.map(([request]) => request.bounds?.y)).toEqual([100, 140]);
  });

  it("applies a hide after an in-flight show before any later selection", async () => {
    let finishShow!: () => void;
    const apply = vi.fn().mockImplementationOnce(() => new Promise<void>((resolve) => { finishShow = resolve; }))
      .mockResolvedValue(undefined);
    const queue = new BrowserPresentationQueue(apply);
    queue.request(visible(100));
    queue.request(hidden);
    finishShow();
    await queue.whenIdle();
    expect(apply.mock.calls.map(([request]) => request.tabId)).toEqual(["tab-1", null]);
  });

  it("preserves a pending hide when the page is shown again before the native call finishes", async () => {
    let finishShow!: () => void;
    const apply = vi.fn().mockImplementationOnce(() => new Promise<void>((resolve) => { finishShow = resolve; }))
      .mockResolvedValue(undefined);
    const queue = new BrowserPresentationQueue(apply);
    queue.request(visible(100));
    queue.request(hidden);
    queue.request(visible(140));
    finishShow();
    await queue.whenIdle();
    expect(apply.mock.calls.map(([request]) => request.tabId)).toEqual(["tab-1", null, "tab-1"]);
  });

  it("reports a failed native placement and still applies the next request", async () => {
    const error = new Error("Native frame unavailable");
    const failed = vi.fn();
    const apply = vi.fn().mockRejectedValueOnce(error).mockResolvedValue(undefined);
    const queue = new BrowserPresentationQueue(apply);
    queue.request(visible(100), { onError: failed });
    queue.request(visible(140));
    await queue.whenIdle();
    expect(failed).toHaveBeenCalledWith(error);
    expect(apply).toHaveBeenCalledTimes(2);
    expect(apply).toHaveBeenLastCalledWith(visible(140));
  });
});
